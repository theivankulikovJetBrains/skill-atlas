"""Render a scan result as terminal text and as a standalone HTML report."""

from __future__ import annotations

import shutil
import textwrap
from collections.abc import Iterator
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from . import __version__
from .models import ScanResult

_MIN_WIDTH = 40
_MAX_WIDTH = 100


def _terminal_width() -> int:
    return max(_MIN_WIDTH, min(_MAX_WIDTH, shutil.get_terminal_size((80, 24)).columns))


def format_console(result: ScanResult) -> str:
    """Human-readable summary of the scan for stdout."""
    return "\n".join(_console_lines(result))


def _console_lines(result: ScanResult) -> Iterator[str]:
    width = _terminal_width()
    yield f"Scanned {result.source}"
    if result.commit:
        ref = f"{result.ref} @ " if result.ref else ""
        yield f"  at {ref}{result.commit[:12]}"
    yield ""

    if not result.skills:
        yield "No skills found (looked for SKILL.md files with YAML frontmatter)."
        return

    noun = "skill" if result.skill_count == 1 else "skills"
    yield f"Found {result.skill_count} {noun}:"
    for skill in result.skills:
        yield ""
        yield f"  {skill.name}"
        yield f"    {skill.path}"
        if skill.description:
            for line in textwrap.wrap(skill.description, width=width - 4) or [""]:
                yield f"    {line}"
        for issue in skill.issues:
            yield f"    ! {issue}"


def write_html(result: ScanResult, destination: str | Path) -> Path:
    """Write ``report.html`` for ``result`` and return the path written."""
    env = Environment(
        loader=PackageLoader("skill_atlas", "templates"),
        autoescape=select_autoescape(default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    html = env.get_template("report.html.j2").render(result=result, version=__version__)

    path = Path(destination)
    if path.parent != Path():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
