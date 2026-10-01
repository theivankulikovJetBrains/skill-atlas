from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from skill_atlas.models import ScanResult, SimilarGroup, Skill
from skill_atlas.repo import web_base_url
from skill_atlas.report import format_console, write_html
from skill_atlas.similarity import pairwise


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

    def test_includes_every_skill(self, tmp_path: Path, shots):
        page = shots.page(write_html(result(ALPHA, BRAVO), tmp_path / "r.html"))
        page.check("alpha is listed", "alpha" in page.html)
        page.check("bravo is listed too", "bravo" in page.html)
        page.check("a description is shown beside it", "Formats things." in page.html)
        page.check("the table is headed", "Skills found" in page.html)

    def test_links_skill_files_to_the_host(self, tmp_path: Path, shots):
        page = shots.page(write_html(result(ALPHA), tmp_path / "r.html"))
        expected = f'href="https://github.com/acme/widgets/blob/{"a" * 40}/.claude/skills/alpha/SKILL.md"'
        page.check("the path is a link to the file on the host", expected in page.html)

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
    def test_skill_metadata_is_html_escaped(self, tmp_path: Path, field: str, payload: str, shots):
        skill = Skill(
            name=payload if field == "name" else "safe",
            description=payload if field == "description" else "safe",
            path="SKILL.md",
        )
        page = shots.page(write_html(result(skill), tmp_path / "r.html"))
        # The frame is worth more than usual here: it shows the payload sitting in the
        # table as text. A page that executed it instead looks identical in the markup
        # these two checks read, and nothing like it once drawn.
        page.check(f"the {field} is not in the document verbatim", payload not in page.html)
        page.check("it was escaped on the way in", "&lt;" in page.html or "&amp;" in page.html)

    def test_source_url_is_escaped(self, tmp_path: Path):
        scan = result(ALPHA, source='https://evil/"><script>x</script>', web_base_url=None)
        html = write_html(scan, tmp_path / "r.html").read_text(encoding="utf-8")
        assert "<script>x</script>" not in html

    def test_empty_scan_renders_a_placeholder(self, tmp_path: Path, shots):
        page = shots.page(write_html(result(), tmp_path / "r.html"))
        page.check("it says nothing was found", "No skills found" in page.html)
        page.check("and offers no empty table", "<table" not in page.html)

    def test_is_a_standalone_document(self, tmp_path: Path):
        html = write_html(result(ALPHA), tmp_path / "r.html").read_text(encoding="utf-8")
        assert html.startswith("<!DOCTYPE html>")
        assert "<style>" in html
        assert "src=" not in html  # no external assets to fetch


class TestHtmlSearch:
    """The report's search box, which narrows the table to names matching what is typed.

    The narrowing itself is the browser's job and no browser runs in this suite, so what
    is pinned here is the contract the script depends on: the box, the per-row name it
    matches against, and the empty state it reveals.
    """

    def test_a_populated_report_offers_a_search_box(self, tmp_path: Path):
        html = write_html(result(ALPHA, BRAVO), tmp_path / "r.html").read_text(encoding="utf-8")
        assert 'type="search"' in html
        assert 'aria-label="Search skills by name"' in html

    def test_the_box_ships_hidden_for_readers_without_javascript(self, tmp_path: Path, shots):
        report = write_html(result(ALPHA), tmp_path / "r.html")
        # Two pages, on purpose. The first check is about the document a reader without
        # JavaScript gets, so its frame is taken from a copy with the script stripped --
        # rendered as shipped, the report's own script reveals the box before the shutter
        # opens and the picture would contradict the check beside it. The second check is
        # about that script, so its frame is the shipped document, box and all.
        bare = shots.page(report, no_script=True)
        bare.check("the box ships hidden", '<div class="search" role="search" hidden>' in bare.html)
        shipped = shots.page(report)
        shipped.check("and the script is what reveals it", "box.hidden = false" in shipped.html)

    def test_the_attribute_is_honoured_against_the_flex_layout(self, tmp_path: Path, shots):
        """`.search { display: flex }` outranks the browser's own rule for `[hidden]`.

        Without this the box ships with an attribute that does nothing, and a reader with
        no JavaScript gets the one control on the page that cannot work. The frame is the
        point of this one: the bug it guards against is invisible in the markup and
        obvious in the picture.
        """
        page = shots.page(write_html(result(ALPHA), tmp_path / "r.html"), no_script=True)
        page.check("the rule that makes hidden stick is there",
                   ".search[hidden] { display: none; }" in page.html)

    def test_the_filter_script_is_inline(self, tmp_path: Path):
        html = write_html(result(ALPHA), tmp_path / "r.html").read_text(encoding="utf-8")
        assert "<script>" in html
        assert "addEventListener('input'" in html

    def test_every_row_carries_the_name_it_is_matched_on(self, tmp_path: Path):
        html = write_html(result(ALPHA, BRAVO), tmp_path / "r.html").read_text(encoding="utf-8")
        assert '<tr data-name="alpha">' in html
        assert '<tr data-name="bravo">' in html

    def test_a_name_cannot_break_out_of_the_data_attribute(self, tmp_path: Path):
        skill = Skill('say "hi" & run', "d", "SKILL.md")
        html = write_html(result(skill), tmp_path / "r.html").read_text(encoding="utf-8")
        assert 'say "hi"' not in html
        assert "data-name=" in html
        assert "&#34;" in html or "&quot;" in html

    def test_a_hidden_empty_state_waits_for_a_query_that_matches_nothing(self, tmp_path: Path):
        html = write_html(result(ALPHA), tmp_path / "r.html").read_text(encoding="utf-8")
        assert '<tr class="no-matches" hidden>' in html
        assert "No skill name contains" in html

    def test_an_empty_scan_has_nothing_to_search(self, tmp_path: Path):
        html = write_html(result(), tmp_path / "r.html").read_text(encoding="utf-8")
        assert 'type="search"' not in html
        assert "<script>" not in html


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

    def test_html_renders_one_row_per_copy(self, tmp_path: Path, shots):
        page = shots.page(write_html(result(AGENTS_COPY, CLAUDE_COPY), tmp_path / "r.html"))
        page.check("one row per copy", page.html.count('class="name"') == 2)
        page.check("and the count agrees", "Skills found <b>2</b>" in page.html)

    def test_each_copy_links_to_its_own_file(self, tmp_path: Path):
        html = write_html(result(AGENTS_COPY, CLAUDE_COPY), tmp_path / "r.html").read_text(encoding="utf-8")
        for skill in (AGENTS_COPY, CLAUDE_COPY):
            assert f'href="https://github.com/acme/widgets/blob/{"a" * 40}/{skill.path}"' in html


class TestSimilarGroups:
    """The near-duplicate section. Grouping itself is tested in test_similarity.py."""

    GROUP = SimilarGroup((AGENTS_COPY, CLAUDE_COPY), 1.0)
    LOOSER = SimilarGroup((ALPHA, Skill("alpha-2", "Formats things too.", "skills/alpha-2/SKILL.md")), 0.72)

    def scan(self, *groups: SimilarGroup) -> ScanResult:
        skills = [skill for group in groups for skill in group.skills]
        return result(*skills, similar=list(groups))

    def test_console_omits_the_section_when_nothing_resembles_anything(self):
        assert "Similar" not in format_console(result(ALPHA, BRAVO))

    def test_console_heads_the_section_with_the_group_count(self):
        assert "Similar skills (1 group;" in format_console(self.scan(self.GROUP))
        assert "Similar skills (2 groups;" in format_console(self.scan(self.GROUP, self.LOOSER))

    def test_console_says_what_the_percentage_measures(self):
        """A chained group's weakest member resembles the rest less than the headline."""
        assert "% is the strongest pair in the group" in format_console(self.scan(self.GROUP))

    def test_console_shows_the_score_and_every_member_path(self):
        text = format_console(self.scan(self.GROUP))
        assert "  100%  deploy" in text
        assert "        .agents/skills/deploy/SKILL.md" in text
        assert "        .claude/skills/deploy/SKILL.md" in text

    def test_console_names_each_distinct_skill_in_the_group(self):
        assert "   72%  alpha, alpha-2" in format_console(self.scan(self.LOOSER))

    def test_console_still_lists_every_member_in_the_full_list(self):
        """A group is a pointer, not a replacement -- the copies stay individually visible."""
        text = format_console(self.scan(self.GROUP))
        assert text.index("Found 2 skills:") < text.index("Similar skills")
        assert text.count(".agents/skills/deploy/SKILL.md") == 2  # once in each section

    def test_html_omits_the_section_when_nothing_resembles_anything(self, tmp_path: Path):
        html = write_html(result(ALPHA, BRAVO), tmp_path / "r.html").read_text(encoding="utf-8")
        assert 'class="similar"' not in html
        assert "Similar groups" not in html

    def test_html_renders_a_card_per_group_with_its_score(self, tmp_path: Path, shots):
        page = shots.page(write_html(self.scan(self.GROUP, self.LOOSER), tmp_path / "r.html"))
        page.check("a card per group", page.html.count('<div class="group">') == 2)
        page.check("the vendored copy scores 100%", '<div class="score">100%</div>' in page.html)
        page.check("the looser pair scores 72%", '<div class="score">72%</div>' in page.html)

    def test_html_counts_the_groups_in_the_summary_bar(self, tmp_path: Path):
        html = write_html(self.scan(self.GROUP, self.LOOSER), tmp_path / "r.html").read_text(encoding="utf-8")
        assert "Similar groups <b>2</b>" in html

    def test_html_links_each_member_to_its_own_file(self, tmp_path: Path):
        html = write_html(self.scan(self.GROUP), tmp_path / "r.html").read_text(encoding="utf-8")
        for skill in self.GROUP.skills:
            assert f'href="https://github.com/acme/widgets/blob/{"a" * 40}/{skill.path}"' in html

    def test_html_leaves_the_paths_plain_for_an_unknown_host(self, tmp_path: Path):
        scan = result(*self.GROUP.skills, similar=[self.GROUP], web_base_url=None)
        html = write_html(scan, tmp_path / "r.html").read_text(encoding="utf-8")
        assert "/blob/" not in html
        assert 'class="similar"' in html

    def test_html_escapes_a_members_name(self, tmp_path: Path):
        hostile = Skill("<script>alert(1)</script>", "d", "x/SKILL.md")
        group = SimilarGroup((hostile, Skill("safe", "d", "y/SKILL.md")), 0.9)
        html = write_html(self.scan(group), tmp_path / "r.html").read_text(encoding="utf-8")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_the_section_sits_outside_the_searchable_table(self, tmp_path: Path):
        """The search box narrows table rows; a group card must not be mistaken for one."""
        html = write_html(self.scan(self.GROUP), tmp_path / "r.html").read_text(encoding="utf-8")
        assert html.index("</table>") < html.index('<section class="similar">')


class TestCompareMatrix:
    """The pairwise comparison: checkboxes on the rows, and the scores to compare them with.

    Drawing the grid is the browser's job and no browser runs here, so what is pinned is
    the contract its script reads -- a checkbox per row carrying that row's index into the
    score triangle, and the triangle itself, intact after the template escaped it.
    """

    def scan(self, *skills: Skill, **overrides) -> ScanResult:
        return result(*skills, matrix=pairwise(skills), **overrides)

    def render(self, tmp_path: Path, scan: ScanResult) -> str:
        return write_html(scan, tmp_path / "r.html").read_text(encoding="utf-8")

    def scores(self, html: str) -> list[list[int]]:
        payload = re.search(r'<section class="compare" hidden data-scores="([^"]*)">', html)
        assert payload, "the compare section did not carry a data-scores attribute"
        return json.loads(payload.group(1))

    def test_every_row_gets_a_checkbox_numbered_with_its_own_index(self, tmp_path: Path):
        """The number on the box is the row's place in the triangle, so the pairing is the test."""
        html = self.render(tmp_path, self.scan(ALPHA, BRAVO))
        rows = re.findall(r'<tr data-name="([^"]*)">(.*?)</tr>', html, re.DOTALL)
        assert [name for name, _ in rows] == ["alpha", "bravo"]
        for index, (_, body) in enumerate(rows):
            assert f'data-index="{index}"' in body

    def test_the_header_offers_one_tick_for_every_row_shown(self, tmp_path: Path):
        html = self.render(tmp_path, self.scan(ALPHA, BRAVO))
        assert 'class="pick-all" aria-label="Select every skill shown"' in html

    def test_a_checkbox_is_labelled_with_the_skill_it_compares(self, tmp_path: Path):
        html = self.render(tmp_path, self.scan(ALPHA, BRAVO))
        assert 'aria-label="Compare alpha"' in html

    def test_every_checkbox_carries_where_its_skill_lives(self, tmp_path: Path):
        """Two copies of one name are only told apart by directory, so the grid needs it."""
        html = self.render(tmp_path, self.scan(AGENTS_COPY, CLAUDE_COPY))
        assert 'data-dir=".agents/skills/deploy"' in html
        assert 'data-dir=".claude/skills/deploy"' in html

    def test_a_skill_at_the_repo_root_still_has_somewhere_to_point_at(self, tmp_path: Path):
        html = self.render(tmp_path, self.scan(Skill("root", "d", "SKILL.md"), ALPHA))
        assert 'data-dir="."' in html

    def test_the_scores_survive_the_journey_into_the_attribute(self, tmp_path: Path):
        """Whole percentages only, so nothing here is for HTML escaping to mangle."""
        scan = self.scan(AGENTS_COPY, CLAUDE_COPY, ALPHA)
        assert self.scores(self.render(tmp_path, scan)) == scan.matrix

    def test_the_triangle_holds_the_pair_the_two_copies_make(self, tmp_path: Path):
        scores = self.scores(self.render(tmp_path, self.scan(AGENTS_COPY, CLAUDE_COPY)))
        assert scores == [[], [100]]

    def test_the_column_and_the_button_ship_hidden_for_readers_without_javascript(
        self, tmp_path: Path, shots
    ):
        report = write_html(self.scan(ALPHA, BRAVO), tmp_path / "r.html")
        # Split the same way the search box's twin is: the shipped-hidden check is
        # photographed without the script, the checks about the script with it.
        bare = shots.page(report, no_script=True)
        bare.check("the button ships hidden and disabled",
                   '<button type="button" class="compare-btn" hidden disabled>' in bare.html)
        shipped = shots.page(report)
        shipped.check("the script reveals the button", "button.hidden = false" in shipped.html)
        shipped.check("and the checkbox column with it",
                      "table.classList.add('pickable')" in shipped.html)

    def test_the_grid_ships_empty_and_hidden(self, tmp_path: Path):
        html = self.render(tmp_path, self.scan(ALPHA, BRAVO))
        assert '<section class="compare" hidden' in html
        assert '<table class="matrix"></table>' in html

    def test_the_bands_are_spelled_out_beside_the_grid(self, tmp_path: Path):
        """Colour never carries the ranking alone: there is a legend, and a number in every cell."""
        html = self.render(tmp_path, self.scan(ALPHA, BRAVO))
        assert 'class="scale"' in html
        for band in ("under 10%", "10–24%", "25–44%", "45–69%", "70% and over"):
            assert band in html
        assert html.count("data-heat=") == 4 + 4  # four swatches, four tint rules

    def test_the_empty_state_spans_the_extra_column(self, tmp_path: Path):
        html = self.render(tmp_path, self.scan(ALPHA, BRAVO))
        assert '<td colspan="5">No skill name contains' in html

    def test_one_skill_has_nothing_to_compare(self, tmp_path: Path):
        """pairwise() gives a lone skill one empty row, which is truthy and not a pair."""
        scan = self.scan(ALPHA)
        assert scan.matrix == [[]]
        html = self.render(tmp_path, scan)
        assert 'class="compare"' not in html
        assert 'class="pick"' not in html
        assert '<td colspan="4">No skill name contains' in html

    def test_a_scan_that_skipped_the_comparison_shows_none_of_it(self, tmp_path: Path):
        """--no-similar, or a collection past the cap: the skills table is untouched."""
        html = self.render(tmp_path, result(ALPHA, BRAVO))
        assert 'class="compare"' not in html
        assert "data-scores" not in html
        assert 'class="pick"' not in html
        assert "alpha" in html and "bravo" in html

    def test_a_name_cannot_break_out_of_its_checkbox_label(self, tmp_path: Path):
        hostile = Skill('say "hi" & <b>run</b>', "d", "x/SKILL.md")
        html = self.render(tmp_path, self.scan(hostile, ALPHA))
        assert "<b>run</b>" not in html
        assert 'say "hi"' not in html
        assert "aria-label=" in html

    def test_it_sits_between_the_table_and_the_groups(self, tmp_path: Path):
        scan = self.scan(AGENTS_COPY, CLAUDE_COPY, similar=[SimilarGroup((AGENTS_COPY, CLAUDE_COPY), 1.0)])
        html = self.render(tmp_path, scan)
        assert html.index("</table>") < html.index('<section class="compare"')
        assert html.index('<section class="compare"') < html.index('<section class="similar">')

    def test_the_compare_column_does_not_disturb_the_rows(self, tmp_path: Path):
        """Every control acts on one table, so the compare column must not disturb the rows."""
        html = self.render(tmp_path, self.scan(ALPHA, BRAVO))
        assert '<div class="search" role="search" hidden>' in html
        assert html.count('<tr data-name=') == 2


class TestStars:
    """Starring skills in the report, and filtering the table down to the starred ones.

    The stars themselves live in the reader's browser and no browser runs here, so what is
    pinned is what the script files them under -- a path per row and a key per repository --
    and the controls it reveals.
    """

    def render(self, tmp_path: Path, scan: ScanResult) -> str:
        return write_html(scan, tmp_path / "r.html").read_text(encoding="utf-8")

    def test_every_row_gets_a_star_filed_under_its_path(self, tmp_path: Path):
        html = self.render(tmp_path, result(ALPHA, BRAVO))
        rows = re.findall(r'<tr data-name="([^"]*)">(.*?)</tr>', html, re.DOTALL)
        assert [name for name, _ in rows] == ["alpha", "bravo"]
        for (_, body), skill in zip(rows, (ALPHA, BRAVO)):
            assert f'data-path="{skill.path}" aria-pressed="false"' in body

    def test_two_copies_of_one_name_are_starred_separately(self, tmp_path: Path):
        html = self.render(tmp_path, result(AGENTS_COPY, CLAUDE_COPY))
        assert 'data-path=".agents/skills/deploy/SKILL.md"' in html
        assert 'data-path=".claude/skills/deploy/SKILL.md"' in html

    def test_a_star_is_labelled_with_the_skill_it_stars(self, tmp_path: Path):
        html = self.render(tmp_path, result(ALPHA))
        assert 'aria-label="Star alpha"' in html

    def test_stars_are_filed_under_the_browsable_url_when_there_is_one(self, tmp_path: Path):
        html = self.render(tmp_path, result(ALPHA))
        assert '<table class="skills" data-stars-key="https://github.com/acme/widgets">' in html

    def test_ssh_and_https_spellings_of_one_repo_share_their_stars(self):
        keys = {
            result(ALPHA, source=url, web_base_url=web_base_url(url)).stars_key
            for url in (
                "https://github.com/acme/widgets.git",
                "https://github.com/acme/widgets",
                "git@github.com:acme/widgets.git",
            )
        }
        assert keys == {"https://github.com/acme/widgets"}

    def test_an_unknown_host_files_them_under_the_url_as_given(self, tmp_path: Path):
        scan = result(ALPHA, source=" file:///srv/widgets ", web_base_url=None)
        assert scan.stars_key == "file:///srv/widgets"
        assert 'data-stars-key="file:///srv/widgets"' in self.render(tmp_path, scan)

    def test_a_star_outlives_the_commit_it_was_placed_on(self):
        """Keyed by repository alone, so the next push does not wipe what the reader starred."""
        assert result(ALPHA, commit="a" * 40).stars_key == result(ALPHA, commit="b" * 40).stars_key
        assert result(ALPHA, ref="main").stars_key == result(ALPHA, ref="v2").stars_key

    def test_the_script_keeps_them_in_the_browser_under_that_key(self, tmp_path: Path):
        html = self.render(tmp_path, result(ALPHA))
        assert "window.localStorage" in html
        assert "`skill-atlas:stars:${table.dataset.starsKey}`" in html

    def test_a_source_cannot_break_out_of_the_key_attribute(self, tmp_path: Path):
        scan = result(ALPHA, source='https://evil/"><script>x</script>', web_base_url=None)
        html = self.render(tmp_path, scan)
        assert "<script>x</script>" not in html
        assert 'data-stars-key="https://evil/"' not in html
        assert "data-stars-key=" in html

    def test_a_path_cannot_break_out_of_its_data_attribute(self, tmp_path: Path):
        odd = Skill("odd", "d", 'skills/a "b" <c>/SKILL.md')
        html = self.render(tmp_path, result(odd, web_base_url=None))
        assert 'data-path="skills/a "b"' not in html
        assert "<c>" not in html

    def test_the_column_and_the_filter_ship_hidden_for_readers_without_javascript(
        self, tmp_path: Path, shots
    ):
        report = write_html(result(ALPHA, BRAVO), tmp_path / "r.html")
        # The same split as the search box and the compare button: what ships hidden is
        # photographed without the script, and what the script reveals with it.
        bare = shots.page(report, no_script=True)
        bare.check("the Starred filter ships hidden and disabled",
                   '<button type="button" class="starred-btn" aria-pressed="false" hidden disabled>'
                   in bare.html)
        shipped = shots.page(report)
        shipped.check("the script reveals the filter", "starredButton.hidden = false" in shipped.html)
        shipped.check("and a star beside every name",
                      "table.classList.add('starrable')" in shipped.html
                      and shipped.html.count('class="star-btn"') == 2)

    def test_a_hidden_empty_state_waits_for_a_filter_with_nothing_starred(self, tmp_path: Path):
        html = self.render(tmp_path, result(ALPHA, BRAVO))
        assert '<tr class="no-stars" hidden>' in html
        assert "Nothing is starred yet." in html
        assert '<span class="among-starred" hidden>' in html

    def test_both_empty_states_span_the_star_column(self, tmp_path: Path):
        html = self.render(tmp_path, result(ALPHA, BRAVO))
        assert '<td colspan="4">No skill name contains' in html
        assert '<td colspan="4">Nothing is starred yet.' in html

    def test_stars_do_not_depend_on_the_comparison(self, tmp_path: Path):
        """--no-similar drops the checkboxes; a star is a different question and stays."""
        html = self.render(tmp_path, result(ALPHA, BRAVO))
        assert 'class="pick"' not in html
        assert html.count('class="star-btn"') == 2

    def test_an_empty_scan_has_nothing_to_star(self, tmp_path: Path):
        html = self.render(tmp_path, result())
        assert 'class="star-btn"' not in html
        assert 'class="starred-btn"' not in html
        assert "data-stars-key" not in html


class TestUnusualPaths:
    def test_console_prints_an_odd_path_verbatim(self):
        skill = Skill("odd", "Strange home.", "my skills/café (v2)/SKILL.md")
        assert "my skills/café (v2)/SKILL.md" in format_console(result(skill))

    def test_an_odd_path_is_still_linked(self, tmp_path: Path, shots):
        skill = Skill("odd", "Strange home.", "my skills/café/SKILL.md")
        page = shots.page(write_html(result(skill), tmp_path / "r.html"))
        # The frame also catches the other way this goes wrong: a path that is linked
        # correctly and drawn as mojibake.
        page.check("the odd path is linked", f'/blob/{"a" * 40}/my skills/café/SKILL.md"' in page.html)

    def test_a_path_cannot_break_out_of_the_href_attribute(self, tmp_path: Path):
        """Directory names may legally contain quotes and ampersands on POSIX."""
        skill = Skill("odd", "d", 'weird "dir" & co/SKILL.md')
        html = write_html(result(skill), tmp_path / "r.html").read_text(encoding="utf-8")
        assert 'weird "dir"' not in html
        assert "&amp; co" in html
        assert "&#34;" in html or "&quot;" in html

    def test_an_issue_names_the_file_even_for_an_odd_path(self, tmp_path: Path, shots):
        skill = Skill("odd", "", "my skills/café/Skill.md", ("has no YAML frontmatter",))
        page = shots.page(write_html(result(skill), tmp_path / "r.html"))
        page.check("the issue names the file it is about",
                   "Skill.md has no YAML frontmatter" in page.html)
