from __future__ import annotations

import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest


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
