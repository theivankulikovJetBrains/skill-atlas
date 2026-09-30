from __future__ import annotations

import io
import sys
import webbrowser
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import nfc, skill_md

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
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record what the CLI hands to the browser instead of launching one."""
    urls: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url, *_a, **_kw: urls.append(url) or True)
    return urls


def at_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pose as an interactive session on a machine with a graphical browser.

    Call this inside the test body, not from a fixture: pytest reinstates its
    own ``sys.stdout`` when the call phase begins, which would undo a patch
    applied during fixture setup.
    """
    monkeypatch.setattr(sys, "stdout", TtyStream(io.BytesIO(), encoding="utf-8"))
    monkeypatch.setenv("DISPLAY", ":0")


class TestOpensTheReport:
    def test_report_is_opened_after_a_scan(self, fake_clone, in_tmp_cwd, opened, monkeypatch):
        at_a_terminal(monkeypatch)
        fake_clone()
        assert cli.main(["scan", URL]) == 0
        assert opened == [(in_tmp_cwd / "report.html").resolve().as_uri()]

    def test_the_opened_url_is_absolute(self, fake_clone, in_tmp_cwd, opened, monkeypatch):
        """``--out`` is relative by default, and a relative path has no file URI."""
        at_a_terminal(monkeypatch)
        fake_clone()
        cli.main(["scan", URL])
        assert opened[0].startswith("file:///")

    def test_custom_destination_is_the_one_opened(self, fake_clone, in_tmp_cwd, opened, monkeypatch):
        at_a_terminal(monkeypatch)
        fake_clone()
        cli.main(["scan", URL, "--out", "custom/atlas.html"])
        assert opened == [(in_tmp_cwd / "custom" / "atlas.html").resolve().as_uri()]

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

    def test_a_failing_browser_does_not_fail_the_scan(self, fake_clone, in_tmp_cwd, monkeypatch):
        at_a_terminal(monkeypatch)

        def _boom(*_args, **_kwargs):
            raise webbrowser.Error("no runnable browser")

        monkeypatch.setattr(cli.webbrowser, "open", _boom)
        fake_clone()
        assert cli.main(["scan", URL]) == 0
        assert (in_tmp_cwd / "report.html").is_file()


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
