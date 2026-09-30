from __future__ import annotations

import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from skill_atlas import cli

#: The real serving loop and the shipped port, captured before ``no_server_loop``
#: replaces them for every test in the suite.
REAL_SERVE_FOREVER = cli._ReportServer.serve_forever
SHIPPED_DEFAULT_PORT = cli.DEFAULT_PORT


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
