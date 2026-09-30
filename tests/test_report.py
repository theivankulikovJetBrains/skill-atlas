from __future__ import annotations

from pathlib import Path

import pytest

from skill_atlas.models import ScanResult, Skill
from skill_atlas.report import format_console, write_html


def result(*skills: Skill, **overrides) -> ScanResult:
    defaults = {
        "source": "https://github.com/acme/widgets.git",
        "scanned_at": "2026-09-30 08:00 UTC",
        "skills": list(skills),
        "commit": "a" * 40,
        "web_base_url": "https://github.com/acme/widgets",
    }
    return ScanResult(**{**defaults, **overrides})


ALPHA = Skill("alpha", "Formats things.", ".claude/skills/alpha/SKILL.md")
BRAVO = Skill("bravo", "", "skills/bravo/SKILL.md", ("frontmatter is missing 'description'",))

#: The same skill vendored under both agent directories, with the copies drifted apart.
AGENTS_COPY = Skill("deploy", "From .agents.", ".agents/skills/deploy/SKILL.md")
CLAUDE_COPY = Skill("deploy", "", ".claude/skills/deploy/SKILL.md", ("frontmatter is missing 'description'",))


class TestConsoleOutput:
    def test_lists_name_path_and_description(self):
        text = format_console(result(ALPHA))
        assert "alpha" in text
        assert ".claude/skills/alpha/SKILL.md" in text
        assert "Formats things." in text

    def test_reports_the_source_and_commit(self):
        text = format_console(result(ALPHA))
        assert "https://github.com/acme/widgets.git" in text
        assert "a" * 12 in text

    def test_counts_skills_with_correct_plural(self):
        assert "Found 1 skill:" in format_console(result(ALPHA))
        assert "Found 2 skills:" in format_console(result(ALPHA, BRAVO))

    def test_empty_scan_says_so(self):
        text = format_console(result())
        assert "No skills found" in text
        assert "SKILL.md" in text

    def test_issues_are_surfaced(self):
        assert "! frontmatter is missing 'description'" in format_console(result(BRAVO))

    def test_long_descriptions_are_wrapped(self):
        long = Skill("long", "word " * 80, "SKILL.md")
        lines = format_console(result(long)).splitlines()
        assert max(len(line) for line in lines) <= 100


class TestHtmlReport:
    def test_writes_the_requested_file(self, tmp_path: Path):
        path = write_html(result(ALPHA), tmp_path / "report.html")
        assert path.is_file()
        assert path.name == "report.html"

    def test_creates_missing_parent_directories(self, tmp_path: Path):
        path = write_html(result(ALPHA), tmp_path / "nested" / "out" / "report.html")
        assert path.is_file()

    def test_overwrites_an_existing_report(self, tmp_path: Path):
        target = tmp_path / "report.html"
        target.write_text("stale", encoding="utf-8")
        write_html(result(ALPHA), target)
        assert "stale" not in target.read_text(encoding="utf-8")

    def test_includes_every_skill(self, tmp_path: Path):
        html = write_html(result(ALPHA, BRAVO), tmp_path / "r.html").read_text(encoding="utf-8")
        assert "alpha" in html
        assert "bravo" in html
        assert "Formats things." in html
        assert "Skills found" in html

    def test_links_skill_files_to_the_host(self, tmp_path: Path):
        html = write_html(result(ALPHA), tmp_path / "r.html").read_text(encoding="utf-8")
        expected = f'href="https://github.com/acme/widgets/blob/{"a" * 40}/.claude/skills/alpha/SKILL.md"'
        assert expected in html

    def test_omits_links_for_unknown_hosts(self, tmp_path: Path):
        scan = result(ALPHA, web_base_url=None)
        html = write_html(scan, tmp_path / "r.html").read_text(encoding="utf-8")
        assert "/blob/" not in html
        assert ".claude/skills/alpha/SKILL.md" in html

    @pytest.mark.parametrize(
        ("field", "payload"),
        [
            ("name", "<script>alert(1)</script>"),
            ("description", "Uses <b>bold</b> & \"quotes\" 'here'"),
        ],
    )
    def test_skill_metadata_is_html_escaped(self, tmp_path: Path, field: str, payload: str):
        skill = Skill(
            name=payload if field == "name" else "safe",
            description=payload if field == "description" else "safe",
            path="SKILL.md",
        )
        html = write_html(result(skill), tmp_path / "r.html").read_text(encoding="utf-8")
        assert payload not in html
        assert "&lt;" in html or "&amp;" in html

    def test_source_url_is_escaped(self, tmp_path: Path):
        scan = result(ALPHA, source='https://evil/"><script>x</script>', web_base_url=None)
        html = write_html(scan, tmp_path / "r.html").read_text(encoding="utf-8")
        assert "<script>x</script>" not in html

    def test_empty_scan_renders_a_placeholder(self, tmp_path: Path):
        html = write_html(result(), tmp_path / "r.html").read_text(encoding="utf-8")
        assert "No skills found" in html
        assert "<table" not in html

    def test_is_a_standalone_document(self, tmp_path: Path):
        html = write_html(result(ALPHA), tmp_path / "r.html").read_text(encoding="utf-8")
        assert html.startswith("<!DOCTYPE html>")
        assert "<style>" in html
        assert "src=" not in html  # no external assets to fetch


class TestDuplicateSkills:
    """Copies of one skill must stay individually visible -- the drift is the finding."""

    def test_console_lists_every_copy_with_its_own_path(self):
        text = format_console(result(AGENTS_COPY, CLAUDE_COPY))
        assert "Found 2 skills:" in text
        assert text.count("  deploy") == 2
        assert ".agents/skills/deploy/SKILL.md" in text
        assert ".claude/skills/deploy/SKILL.md" in text

    def test_console_attaches_each_issue_to_the_copy_that_has_it(self):
        lines = format_console(result(AGENTS_COPY, CLAUDE_COPY)).splitlines()
        flagged = lines.index("    ! frontmatter is missing 'description'")
        # The warning belongs to the second copy, so it must follow that copy's path.
        assert lines.index("    .claude/skills/deploy/SKILL.md") < flagged
        assert lines.index("    .agents/skills/deploy/SKILL.md") < lines.index("    From .agents.")

    def test_html_renders_one_row_per_copy(self, tmp_path: Path):
        html = write_html(result(AGENTS_COPY, CLAUDE_COPY), tmp_path / "r.html").read_text(encoding="utf-8")
        assert html.count('class="name"') == 2
        assert "Skills found <b>2</b>" in html

    def test_each_copy_links_to_its_own_file(self, tmp_path: Path):
        html = write_html(result(AGENTS_COPY, CLAUDE_COPY), tmp_path / "r.html").read_text(encoding="utf-8")
        for skill in (AGENTS_COPY, CLAUDE_COPY):
            assert f'href="https://github.com/acme/widgets/blob/{"a" * 40}/{skill.path}"' in html


class TestUnusualPaths:
    def test_console_prints_an_odd_path_verbatim(self):
        skill = Skill("odd", "Strange home.", "my skills/café (v2)/SKILL.md")
        assert "my skills/café (v2)/SKILL.md" in format_console(result(skill))

    def test_an_odd_path_is_still_linked(self, tmp_path: Path):
        skill = Skill("odd", "Strange home.", "my skills/café/SKILL.md")
        html = write_html(result(skill), tmp_path / "r.html").read_text(encoding="utf-8")
        assert f'/blob/{"a" * 40}/my skills/café/SKILL.md"' in html

    def test_a_path_cannot_break_out_of_the_href_attribute(self, tmp_path: Path):
        """Directory names may legally contain quotes and ampersands on POSIX."""
        skill = Skill("odd", "d", 'weird "dir" & co/SKILL.md')
        html = write_html(result(skill), tmp_path / "r.html").read_text(encoding="utf-8")
        assert 'weird "dir"' not in html
        assert "&amp; co" in html
        assert "&#34;" in html or "&quot;" in html

    def test_an_issue_names_the_file_even_for_an_odd_path(self, tmp_path: Path):
        skill = Skill("odd", "", "my skills/café/Skill.md", ("has no YAML frontmatter",))
        html = write_html(result(skill), tmp_path / "r.html").read_text(encoding="utf-8")
        assert "Skill.md has no YAML frontmatter" in html
