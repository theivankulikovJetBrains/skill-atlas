from __future__ import annotations

import io
import socket
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from conftest import REAL_SERVE_FOREVER, SHIPPED_DEFAULT_PORT, nfc, skill_md

from skill_atlas import cli
from skill_atlas.repo import Checkout, RepoError

URL = "https://github.com/acme/widgets.git"


@pytest.fixture
def fake_clone(monkeypatch: pytest.MonkeyPatch, make_repo):
    """Replace the real clone with a local fixture tree, so tests stay offline."""

    def _install(files: dict[str, str] | None = None, commit: str = "c" * 40):
        root = make_repo(files if files is not None else {".claude/skills/alpha/SKILL.md": skill_md("alpha")})

        @contextmanager
        def _clone(url: str, ref: str | None = None, timeout: float | None = None):
            yield Checkout(path=root, commit=commit, ref=ref)

        monkeypatch.setattr(cli, "clone", _clone)
        return root

    return _install


@pytest.fixture(autouse=True)
def in_tmp_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run each CLI test in a scratch directory so ./report.html lands there."""
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


class TestScanCommand:
    def test_reports_skills_and_exits_zero(self, fake_clone, capsys):
        fake_clone()
        assert cli.main(["scan", URL]) == 0
        out = capsys.readouterr().out
        assert "alpha" in out
        assert ".claude/skills/alpha/SKILL.md" in out

    def test_writes_report_html_to_the_working_directory(self, fake_clone, in_tmp_cwd, capsys):
        fake_clone()
        cli.main(["scan", URL])
        report = in_tmp_cwd / "report.html"
        assert report.is_file()
        assert "alpha" in report.read_text(encoding="utf-8")
        assert str(report.resolve()) in capsys.readouterr().out

    def test_out_flag_overrides_the_destination(self, fake_clone, in_tmp_cwd):
        fake_clone()
        cli.main(["scan", URL, "--out", "custom/atlas.html"])
        assert (in_tmp_cwd / "custom" / "atlas.html").is_file()
        assert not (in_tmp_cwd / "report.html").exists()

    def test_no_html_skips_the_file(self, fake_clone, in_tmp_cwd):
        fake_clone()
        assert cli.main(["scan", URL, "--no-html"]) == 0
        assert not (in_tmp_cwd / "report.html").exists()

    def test_ref_is_passed_through_and_reported(self, fake_clone, in_tmp_cwd):
        fake_clone()
        cli.main(["scan", URL, "--ref", "release-1.2"])
        assert "release-1.2" in (in_tmp_cwd / "report.html").read_text(encoding="utf-8")

    def test_repository_without_skills_still_succeeds(self, fake_clone, in_tmp_cwd, capsys):
        fake_clone({"README.md": "# no skills\n"})
        assert cli.main(["scan", URL]) == 0
        assert "No skills found" in capsys.readouterr().out
        assert (in_tmp_cwd / "report.html").is_file()

    def test_duplicated_skills_reach_both_outputs(self, fake_clone, in_tmp_cwd, capsys):
        """A repo keeping one skill under .agents/ and .claude/ reports both copies."""
        fake_clone(
            {
                ".agents/skills/deploy/SKILL.md": skill_md("deploy", "From .agents."),
                ".claude/skills/deploy/SKILL.md": skill_md("deploy", "From .claude."),
            }
        )
        assert cli.main(["scan", URL]) == 0

        out = capsys.readouterr().out
        assert "Found 2 skills:" in out
        html = (in_tmp_cwd / "report.html").read_text(encoding="utf-8")
        for path in (".agents/skills/deploy/SKILL.md", ".claude/skills/deploy/SKILL.md"):
            assert path in out
            assert path in html

    def test_a_skill_in_an_odd_directory_survives_the_whole_pipeline(self, fake_clone, in_tmp_cwd, capsys):
        fake_clone({"my skills/café (v2)/SKILL.md": skill_md("odd", "Lives somewhere strange.")})
        assert cli.main(["scan", URL]) == 0

        assert "my skills/café (v2)/SKILL.md" in nfc(capsys.readouterr().out)
        html = nfc((in_tmp_cwd / "report.html").read_text(encoding="utf-8"))
        assert "my skills/café (v2)/SKILL.md" in html

    def test_report_can_be_written_into_an_odd_directory(self, fake_clone, in_tmp_cwd):
        fake_clone()
        assert cli.main(["scan", URL, "--out", "out dir (new)/atlas report.html"]) == 0
        assert (in_tmp_cwd / "out dir (new)" / "atlas report.html").is_file()

    def test_clone_failure_exits_one_with_stderr_message(self, monkeypatch, capsys, in_tmp_cwd):
        @contextmanager
        def _fail(*_args, **_kwargs):
            raise RepoError("could not clone https://example.com/nope.git")
            yield  # pragma: no cover

        monkeypatch.setattr(cli, "clone", _fail)
        assert cli.main(["scan", "https://example.com/nope.git"]) == 1
        captured = capsys.readouterr()
        assert "skill-atlas: could not clone" in captured.err
        assert not (in_tmp_cwd / "report.html").exists()

    def test_unwritable_report_exits_one(self, fake_clone, monkeypatch, capsys):
        fake_clone()

        def _boom(*_args, **_kwargs):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(cli, "write_html", _boom)
        assert cli.main(["scan", URL]) == 1
        assert "could not write" in capsys.readouterr().err


class TtyStream(io.TextIOWrapper):
    """A stdout lookalike that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch, no_server_loop: list[cli._ReportServer]) -> list[str]:
    """Record what the CLI hands to the browser instead of launching one."""
    urls: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url, *_a, **_kw: urls.append(url) or True)
    return urls


def free_port() -> int:
    """A port nothing is listening on -- as close to a guarantee as sockets allow."""
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def taken_port():
    """Hold a port open so the next bind to it fails the way a busy 8888 would."""
    with closing(socket.socket()) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        yield holder.getsockname()[1]


@contextmanager
def serving(document: bytes):
    """Run the real server in a thread and yield its base URL.

    Deliberately the pre-fixture loop rather than the patched one: ``shutdown()``
    waits on an event that only ``serve_forever`` sets, so stopping a server whose
    loop never ran would block here instead of at the scan.
    """
    server = cli._ReportServer(("127.0.0.1", 0), document)
    thread = threading.Thread(target=REAL_SERVE_FOREVER, args=(server,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def status_of(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def at_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pose as an interactive session on a machine with a graphical browser.

    Call this inside the test body, not from a fixture: pytest reinstates its
    own ``sys.stdout`` when the call phase begins, which would undo a patch
    applied during fixture setup.
    """
    monkeypatch.setattr(sys, "stdout", TtyStream(io.BytesIO(), encoding="utf-8"))
    monkeypatch.setenv("DISPLAY", ":0")


class TestOpensTheReport:
    def test_the_served_url_is_opened_after_a_scan(self, fake_clone, in_tmp_cwd, opened, no_server_loop, monkeypatch):
        at_a_terminal(monkeypatch)
        fake_clone()
        assert cli.main(["scan", URL]) == 0
        port = no_server_loop[0].server_address[1]
        assert opened == [f"http://localhost:{port}/"]

    def test_the_opened_url_names_the_port_actually_bound(self, fake_clone, opened, monkeypatch):
        """A URL built from the requested port would say ``:0`` under this fixture."""
        at_a_terminal(monkeypatch)
        fake_clone()
        cli.main(["scan", URL])
        assert opened[0].startswith("http://localhost:")
        assert not opened[0].startswith("http://localhost:0/")

    def test_the_port_scanned_on_is_the_default(self, fake_clone, opened, monkeypatch):
        port = free_port()
        monkeypatch.setattr(cli, "DEFAULT_PORT", port)  # over the fixture's 0
        at_a_terminal(monkeypatch)
        fake_clone()
        cli.main(["scan", URL])
        assert opened == [f"http://localhost:{port}/"]

    def test_custom_destination_is_the_document_served(self, fake_clone, in_tmp_cwd, opened, no_server_loop, monkeypatch):
        at_a_terminal(monkeypatch)
        fake_clone()
        cli.main(["scan", URL, "--out", "custom/atlas.html"])
        assert no_server_loop[0].document == (in_tmp_cwd / "custom" / "atlas.html").read_bytes()

    def test_no_open_suppresses_it_but_still_writes(self, fake_clone, in_tmp_cwd, opened, monkeypatch):
        at_a_terminal(monkeypatch)
        fake_clone()
        assert cli.main(["scan", URL, "--no-open"]) == 0
        assert opened == []
        assert (in_tmp_cwd / "report.html").is_file()

    def test_nothing_is_opened_when_there_is_no_report(self, fake_clone, opened, monkeypatch):
        at_a_terminal(monkeypatch)
        fake_clone()
        cli.main(["scan", URL, "--no-html"])
        assert opened == []

    def test_nothing_is_opened_when_the_scan_fails(self, monkeypatch, opened):
        at_a_terminal(monkeypatch)

        @contextmanager
        def _fail(*_args, **_kwargs):
            raise RepoError("could not clone")
            yield  # pragma: no cover

        monkeypatch.setattr(cli, "clone", _fail)
        assert cli.main(["scan", URL]) == 1
        assert opened == []

    def test_redirected_output_is_left_alone(self, fake_clone, in_tmp_cwd, opened, monkeypatch):
        """A pipe means a script or CI is driving us; a browser window would be noise."""
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
        assert not sys.stdout.isatty()
        fake_clone()
        assert cli.main(["scan", URL]) == 0
        assert opened == []
        assert (in_tmp_cwd / "report.html").is_file()

    def test_a_failing_browser_does_not_fail_the_scan(self, fake_clone, in_tmp_cwd, no_server_loop, monkeypatch):
        at_a_terminal(monkeypatch)

        def _boom(*_args, **_kwargs):
            raise webbrowser.Error("no runnable browser")

        monkeypatch.setattr(cli.webbrowser, "open", _boom)
        fake_clone()
        assert cli.main(["scan", URL]) == 0
        assert (in_tmp_cwd / "report.html").is_file()


class TestPortFlag:
    def test_the_chosen_port_is_the_one_bound_and_opened(self, fake_clone, opened, monkeypatch):
        port = free_port()
        at_a_terminal(monkeypatch)
        fake_clone()
        assert cli.main(["scan", URL, "--port", str(port)]) == 0
        assert opened == [f"http://localhost:{port}/"]

    def test_port_zero_takes_any_free_one_and_reports_it(self, fake_clone, opened, no_server_loop, monkeypatch):
        """0 is the OS asking-for-anything port; the URL must name what it got."""
        at_a_terminal(monkeypatch)
        fake_clone()
        assert cli.main(["scan", URL, "--port", "0"]) == 0
        bound = no_server_loop[0].server_address[1]
        assert bound != 0
        assert opened == [f"http://localhost:{bound}/"]

    def test_omitting_the_flag_falls_back_to_the_module_default(self):
        """Pinned to the constant, not to 8888: the autouse fixture moves it to 0, and
        the shipped value is asserted separately."""
        args = cli.build_parser().parse_args(["scan", URL])
        assert args.port == cli.DEFAULT_PORT

    @pytest.mark.parametrize("bad", ["-1", "65536", "99999", "http", "8.5", "", " "])
    def test_a_port_that_could_never_bind_is_a_usage_error(self, bad: str):
        """Exit 2 before the clone, rather than a stderr line after the whole scan."""
        with pytest.raises(SystemExit) as exc:
            cli.main(["scan", URL, "--port", bad])
        assert exc.value.code == 2

    @pytest.mark.parametrize("edge", ["0", "1", "65535"])
    def test_the_range_boundaries_are_accepted(self, edge: str):
        assert cli.build_parser().parse_args(["scan", URL, "--port", edge]).port == int(edge)

    def test_a_chosen_port_already_taken_is_reported_not_fatal(self, fake_clone, in_tmp_cwd, opened, monkeypatch, capsys):
        """Asking for a busy port explicitly is still not a failed scan -- the report
        is on disk either way, and the reason goes to stderr."""
        at_a_terminal(monkeypatch)
        fake_clone()
        with taken_port() as port:
            assert cli.main(["scan", URL, "--port", str(port)]) == 0
        assert opened == []
        assert f"not serving the report on port {port}" in capsys.readouterr().err
        assert (in_tmp_cwd / "report.html").is_file()

    def test_no_open_wins_over_an_explicit_port(self, fake_clone, in_tmp_cwd, opened, monkeypatch):
        at_a_terminal(monkeypatch)
        fake_clone()
        assert cli.main(["scan", URL, "--port", str(free_port()), "--no-open"]) == 0
        assert opened == []
        assert (in_tmp_cwd / "report.html").is_file()


class TestServesTheReport:
    def test_the_default_port_is_8888(self):
        """Read from the import-time capture: the autouse fixture moves it to 0."""
        assert SHIPPED_DEFAULT_PORT == 8888

    def test_the_report_is_what_the_server_returns(self, in_tmp_cwd):
        document = "<h1>café</h1>".encode()
        with serving(document) as base:
            with urllib.request.urlopen(f"{base}/", timeout=5) as response:
                assert response.status == 200
                assert response.read() == document
                assert response.headers["Content-Type"] == "text/html; charset=utf-8"

    def test_head_answers_without_a_body(self, in_tmp_cwd):
        document = b"<h1>head</h1>"
        with serving(document) as base:
            request = urllib.request.Request(f"{base}/", method="HEAD")
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 200
                assert response.headers["Content-Length"] == str(len(document))
                assert response.read() == b""

    def test_a_sibling_file_is_not_exposed(self, in_tmp_cwd):
        """Rooting a directory handler at the report would publish the whole cwd."""
        (in_tmp_cwd / "secret.txt").write_text("private", encoding="utf-8")
        with serving(b"<h1>report</h1>") as base:
            assert status_of(f"{base}/secret.txt") == 404

    def test_an_unknown_path_does_not_take_the_server_down(self, in_tmp_cwd):
        with serving(b"<h1>report</h1>") as base:
            assert status_of(f"{base}/favicon.ico") == 404
            assert status_of(f"{base}/") == 200

    def test_a_query_string_still_reaches_the_report(self, in_tmp_cwd):
        """Browsers and reloads append things like ``?`` on their own."""
        with serving(b"<h1>report</h1>") as base:
            assert status_of(f"{base}/?reload=1") == 200

    def test_the_socket_is_closed_when_serving_ends(self, tmp_path, no_server_loop):
        report = tmp_path / "report.html"
        report.write_text("<h1>done</h1>", encoding="utf-8")
        cli._serve_report(report, 0)
        assert no_server_loop[0].socket.fileno() == -1

    def test_a_busy_port_is_reported_not_raised(self, tmp_path, capsys):
        report = tmp_path / "report.html"
        report.write_text("<h1>busy</h1>", encoding="utf-8")
        with taken_port() as port:
            cli._serve_report(report, port)
        assert f"not serving the report on port {port}" in capsys.readouterr().err

    def test_a_busy_port_does_not_fail_the_scan(self, fake_clone, in_tmp_cwd, opened, monkeypatch):
        at_a_terminal(monkeypatch)
        fake_clone()
        with taken_port() as port:
            monkeypatch.setattr(cli, "DEFAULT_PORT", port)
            assert cli.main(["scan", URL]) == 0
        assert opened == []
        assert (in_tmp_cwd / "report.html").is_file()

    def test_an_unreadable_report_is_reported_not_raised(self, tmp_path, capsys):
        cli._serve_report(tmp_path / "never-written.html", 0)
        assert "not serving the report" in capsys.readouterr().err

    def test_ctrl_c_stops_serving_without_an_error(self, fake_clone, in_tmp_cwd, opened, monkeypatch):
        """Ctrl+C is how the user ends a served report, not a failed scan."""

        def _interrupted(_self):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli._ReportServer, "serve_forever", _interrupted)
        at_a_terminal(monkeypatch)
        fake_clone()
        assert cli.main(["scan", URL]) == 0
        assert (in_tmp_cwd / "report.html").is_file()

    def test_a_dropped_connection_prints_nothing(self, in_tmp_cwd, capsys):
        """Closing the tab mid-response would otherwise look like a crash."""
        server = cli._ReportServer(("127.0.0.1", 0), b"<h1>report</h1>")
        with server:
            server.handle_error(None, ("127.0.0.1", 0))
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "")

    def test_the_server_only_listens_on_loopback(self, tmp_path, no_server_loop):
        """A report of a private repository is not for the rest of the network."""
        report = tmp_path / "report.html"
        report.write_text("<h1>local</h1>", encoding="utf-8")
        cli._serve_report(report, 0)
        assert no_server_loop[0].server_address[0] == "127.0.0.1"


class TestBrowserAvailability:
    def test_headless_linux_is_left_alone(self, monkeypatch):
        """Without a display webbrowser would reach for lynx and seize the terminal."""
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert cli._has_display() is False

    @pytest.mark.parametrize("variable", ["DISPLAY", "WAYLAND_DISPLAY"])
    def test_a_linux_session_with_a_display_is_fine(self, monkeypatch, variable):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setenv(variable, ":0")
        assert cli._has_display() is True

    @pytest.mark.parametrize("platform", ["win32", "darwin"])
    def test_windows_and_macos_never_need_a_display(self, monkeypatch, platform):
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.delenv("DISPLAY", raising=False)
        assert cli._has_display() is True

    def test_stdout_without_isatty_is_tolerated(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", object())
        args = cli.build_parser().parse_args(["scan", URL])
        assert cli._should_open(args) is False


AWKWARD = "Maps a → b for 你好 \U0001f600"


def cp1252_stream() -> io.TextIOWrapper:
    """A stdout lookalike that rejects anything outside the legacy code page."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


class TestStdioHardening:
    def test_legacy_stream_stops_rejecting_unencodable_text(self, monkeypatch):
        stream = cp1252_stream()
        monkeypatch.setattr(sys, "stdout", stream)
        cli._harden_stdio()
        assert stream.errors == "replace"
        stream.write(AWKWARD)  # would raise UnicodeEncodeError before hardening

    def test_legacy_stderr_is_hardened_too(self, monkeypatch):
        stream = cp1252_stream()
        monkeypatch.setattr(sys, "stderr", stream)
        cli._harden_stdio()
        assert stream.errors == "replace"

    def test_utf8_stream_is_left_alone(self, monkeypatch):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
        monkeypatch.setattr(sys, "stdout", stream)
        cli._harden_stdio()
        assert stream.errors == "strict"
        assert stream.encoding == "utf-8"

    def test_stream_without_reconfigure_is_tolerated(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", object())
        cli._harden_stdio()  # must not raise

    def test_unencodable_description_does_not_lose_the_report(self, fake_clone, in_tmp_cwd, monkeypatch):
        fake_clone({"skills/awkward/SKILL.md": f"---\nname: awkward\ndescription: {AWKWARD}\n---\n"})
        monkeypatch.setattr(sys, "stdout", cp1252_stream())

        assert cli.main(["scan", URL]) == 0

        report = in_tmp_cwd / "report.html"
        assert report.is_file()
        assert AWKWARD in report.read_text(encoding="utf-8")


class TestArgumentParsing:
    def test_no_command_is_a_usage_error(self):
        with pytest.raises(SystemExit) as exc:
            cli.main([])
        assert exc.value.code == 2

    def test_scan_requires_a_url(self):
        with pytest.raises(SystemExit) as exc:
            cli.main(["scan"])
        assert exc.value.code == 2

    def test_unknown_command_is_a_usage_error(self):
        with pytest.raises(SystemExit) as exc:
            cli.main(["explode", URL])
        assert exc.value.code == 2

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["--version"])
        assert exc.value.code == 0
        assert "skill-atlas" in capsys.readouterr().out
