from __future__ import annotations

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


def skill_md(name: str = "demo", description: str = "Does a thing.", body: str = "Steps here.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n"
