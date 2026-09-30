from __future__ import annotations

import sys
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest
# By name, not as a module: the ``shots`` fixture below would shadow a module bound to
# the same global, and ``pytest_configure`` would then look its class up on a function.
from shots import Album, Collector

from skill_atlas import cli

#: The real serving loop and the shipped port, captured before ``no_server_loop``
#: replaces them for every test in the suite.
REAL_SERVE_FOREVER = cli._ReportServer.serve_forever
SHIPPED_DEFAULT_PORT = cli.DEFAULT_PORT

#: The session's screenshot collector. On the config rather than in a module global so
#: ``pytest_sessionfinish`` can reach the same object the fixtures handed out.
COLLECTOR = pytest.StashKey[Collector]()


def pytest_addoption(parser: pytest.Parser) -> None:
    """Flags for screenshot collection. See ``tests/shots.py`` for what they buy.

    Each one has an environment variable behind it so a runner can switch collection on
    without owning the pytest command line -- which is how the CI job does it for a
    suite it otherwise invokes exactly as a developer does.
    """
    group = parser.getgroup("screenshots", "photograph the report at every check")
    group.addoption(
        "--shots",
        action="store_true",
        default=False,
        help="collect a screenshot of the report at each check made against it, into "
             "<rootdir>/test-shots (env: SKILL_ATLAS_SHOTS). Off by default: it needs a "
             "browser and adds about a minute to a suite that otherwise runs in seconds.",
    )
    group.addoption(
        "--shots-dir",
        default=None,
        metavar="PATH",
        help="collect into somewhere else instead (env: SKILL_ATLAS_SHOTS_DIR). Spell it "
             "--shots-dir=PATH, with the equals sign: pytest cannot know this option takes "
             "a value until a conftest has loaded, and a separate argument that happens to "
             "be an existing directory is taken for a test path instead -- which moves "
             "rootdir and then loads no conftest at all. The environment variable has no "
             "such problem, which is what the CI job uses it for.",
    )
    group.addoption(
        "--shots-strict",
        action="store_true",
        default=False,
        help="treat a browser that cannot be found or cannot render, or a run that "
             "photographed nothing at all, as an error rather than as a skipped extra "
             "(env: SKILL_ATLAS_SHOTS_STRICT). Implies --shots. What CI uses, so a green "
             "run cannot ship an empty artifact.",
    )
    group.addoption(
        "--shots-browser",
        default=None,
        metavar="PATH",
        help="Chromium-family binary to render with (env: SKILL_ATLAS_SHOTS_BROWSER). "
             "Microsoft Edge is found automatically; Chrome is not a substitute.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.stash[COLLECTOR] = Collector.from_config(config)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    collector = session.config.stash.get(COLLECTOR, None)
    if collector is None:
        return
    collector.finish()
    # A strict run that photographed nothing has passed its tests and produced no
    # evidence, which is the one failure an artifact cannot show you: the upload
    # succeeds and the directory is empty. Only ever turns a green run red, so it
    # cannot mask the real failure in a run that was already red.
    if collector.strict and not collector.shots and exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter, exitstatus: int, config: pytest.Config) -> None:
    collector = config.stash.get(COLLECTOR, None)
    if collector is None or collector.directory is None:
        return
    if collector.shots:
        red = sum(1 for shot in collector.shots if not shot.passed)
        terminalreporter.write_line(
            f"screenshots: {len(collector.shots)} check moments photographed"
            f"{f', {red} red' if red else ''} -> {collector.directory}"
        )
    else:
        terminalreporter.write_line(
            f"screenshots: nothing photographed -> {collector.directory}", yellow=True
        )
    if collector.broken:
        terminalreporter.write_line(
            f"screenshots: collection stopped early: {collector.broken}", yellow=True
        )


@pytest.fixture
def shots(request: pytest.FixtureRequest) -> Album:
    """Photograph the report at each check this test makes against it.

    ``shots.page(report).check(label, condition)`` asserts the condition and leaves a
    frame behind. With no ``--shots-dir`` it is the assert alone and no browser starts,
    so a test written this way costs a default run nothing.
    """
    return request.config.stash[COLLECTOR].album(request.node.nodeid)


@pytest.fixture(autouse=True)
def intact_stdio() -> Iterator[None]:
    """Hand the next test a usable ``sys.stdout``, whoever swapped it out.

    A test that takes ``capsys`` *and* replaces ``sys.stdout`` itself leaves a closed
    stream behind: ``capsys`` installs its ``CaptureIO`` during setup, so that object
    is what ``monkeypatch`` saves as the original, and the two tear down in the order
    that puts it back after ``capsys`` has closed it. The next ``print`` outside
    ``capsys`` then raises ``ValueError: I/O operation on closed file``.

    Normally invisible, because the capture plugin reinstalls ``sys.stdout`` before
    every test. Run with ``-s`` and nothing does -- which is how a suite that is green
    on CI goes red the moment a developer debugs it.

    Declared first and autouse so it tears down last, after both of them.
    """
    streams = (sys.stdout, sys.stderr)
    yield
    sys.stdout, sys.stderr = streams


@pytest.fixture(autouse=True)
def no_server_loop(monkeypatch: pytest.MonkeyPatch) -> list[cli._ReportServer]:
    """Bind a port for real, but record the server instead of serving until Ctrl+C.

    Autouse, and here rather than beside the CLI tests, because forgetting it does not
    fail a test -- it hangs the whole run. ``_serve_report`` waits for a Ctrl+C that no
    suite will ever send, pytest prints nothing while it waits, and CI burns to its job
    timeout. A module-local fixture would only cover the module it lives in, and the
    next test to reach a browser-opening scan may not be written there.

    Tests that want the real loop drive it through ``serving()`` in test_cli.py.

    The default port also moves to 0 (any free one): 8888 is a popular choice for
    whatever else a developer already has running, and a suite that goes red when it
    is taken is testing the machine. The 8888 default is asserted on its own.
    """
    servers: list[cli._ReportServer] = []
    monkeypatch.setattr(cli, "DEFAULT_PORT", 0)
    monkeypatch.setattr(cli._ReportServer, "serve_forever", lambda self: servers.append(self))
    return servers


@pytest.fixture(autouse=True)
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record what the CLI hands to the browser instead of launching one.

    Autouse for the same reason as ``no_server_loop``, and with a worse failure mode:
    forgetting it does not go red, it opens a tab on the developer's own machine. A
    scan only skips the browser when ``sys.stdout`` is not a terminal, which under
    pytest is true by accident -- the capture plugin's doing. Run the suite with ``-s``,
    or from a runner that leaves stdout attached, and every plain ``cli.main(["scan",
    ...])`` reaches ``webbrowser.open`` for real; on Windows and macOS ``_has_display()``
    never says otherwise.

    Patched on the :mod:`webbrowser` module rather than on ``cli``, so the sibling
    entry points (``open_new``, ``open_new_tab``) that call it through the module
    global are stopped too.

    Tests that need the call to fail replace it again in their own body; the last
    ``setattr`` wins, and monkeypatch unwinds both.
    """
    urls: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url, *_a, **_kw: urls.append(url) or True)
    return urls


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[[Mapping[str, str]], Path]:
    """Build a fake repository on disk from a ``{relative path: contents}`` mapping."""

    def _make(files: Mapping[str, str], name: str = "repo") -> Path:
        root = tmp_path / name
        for rel, contents in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        root.mkdir(parents=True, exist_ok=True)
        return root

    return _make


@pytest.fixture
def case_sensitive_fs(tmp_path: Path) -> bool:
    """Whether this checkout can hold ``SKILL.md`` and ``skill.md`` side by side.

    Linux runners can, Windows and stock macOS cannot -- so the tests that need
    two spellings of one filename have to ask rather than assume.
    """
    (tmp_path / "CaseProbe").mkdir()
    return not (tmp_path / "caseprobe").exists()


def skill_md(name: str = "demo", description: str = "Does a thing.", body: str = "Steps here.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n"


def nfc(text: str) -> str:
    """Normalise a path before comparing it to a literal.

    macOS can return directory names decomposed (NFD), so ``"café"`` as typed in a
    test is not byte-equal to the same name as ``os.walk`` reports it.
    """
    return unicodedata.normalize("NFC", text)
