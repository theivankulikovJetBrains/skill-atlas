"""Find Claude Agent Skills on disk.

A skill is a ``SKILL.md`` file. Its ``name`` and ``description`` live in the YAML
frontmatter at the top of the file, delimited by ``---`` lines.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from .models import Skill

SKILL_FILENAME = "SKILL.md"

#: Directories never worth walking into: VCS metadata, build output, vendored deps.
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".idea",
        ".vscode",
        "site-packages",
        "dist",
        "build",
        ".next",
        ".gradle",
        "target",
    }
)

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<body>.*?)^(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)",
    re.DOTALL | re.MULTILINE,
)
_WHITESPACE_RE = re.compile(r"\s+")


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Split ``text`` into its raw YAML frontmatter block and the remaining body.

    Returns ``(None, text)`` when the file does not open with a ``---`` delimiter.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None, text
    return match.group("body"), text[match.end() :]


def _normalize(value: object) -> str:
    """Render a frontmatter scalar as a single-line string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return _WHITESPACE_RE.sub(" ", value).strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def parse_skill_file(path: Path, repo_root: Path) -> Skill:
    """Read one ``SKILL.md`` into a :class:`Skill`, recording any problems found."""
    rel_path = path.relative_to(repo_root).as_posix()
    fallback_name = path.parent.name or rel_path
    issues: list[str] = []

    try:
        # utf-8-sig so a leading BOM does not hide the frontmatter delimiter.
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return Skill(fallback_name, "", rel_path, (f"could not be read: {exc.strerror}",))
    except UnicodeDecodeError:
        return Skill(fallback_name, "", rel_path, ("is not valid UTF-8",))

    raw_frontmatter, _ = split_frontmatter(text)
    fields: dict[object, object] = {}
    usable = False

    if raw_frontmatter is None:
        issues.append("has no YAML frontmatter")
    else:
        try:
            metadata = yaml.safe_load(raw_frontmatter)
        except yaml.YAMLError as exc:
            issues.append(f"has invalid YAML frontmatter: {_first_line(exc)}")
        else:
            if metadata is None:  # an empty frontmatter block
                usable = True
            elif isinstance(metadata, dict):
                fields, usable = metadata, True
            else:
                issues.append("frontmatter is not a YAML mapping")

    name = _normalize(fields.get("name"))
    description = _normalize(fields.get("description"))

    # Only ask for individual fields once we know the frontmatter itself parsed;
    # otherwise every unreadable block reports the same three problems.
    if not name:
        if usable:
            issues.append(_missing_field_issue("name", fields))
        name = fallback_name
    if not description and usable:
        issues.append(_missing_field_issue("description", fields))

    return Skill(name=name, description=description, path=rel_path, issues=tuple(issues))


def _missing_field_issue(field: str, fields: dict[object, object]) -> str:
    if field in fields:
        return f"frontmatter '{field}' is empty or not text"
    return f"frontmatter is missing '{field}'"


def _first_line(exc: Exception) -> str:
    return str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__


def find_skills(repo_root: str | os.PathLike[str]) -> list[Skill]:
    """Walk ``repo_root`` and return every skill found, ordered by file path."""
    root = Path(repo_root).resolve()
    skills: list[Skill] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        for filename in filenames:
            if filename.casefold() == SKILL_FILENAME.casefold():
                skills.append(parse_skill_file(Path(dirpath) / filename, root))

    skills.sort(key=lambda s: s.path)
    return skills
