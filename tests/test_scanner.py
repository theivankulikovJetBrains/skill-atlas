from __future__ import annotations

from pathlib import Path

import pytest
from conftest import skill_md

from skill_atlas.scanner import find_skills, parse_skill_file, split_frontmatter


class TestSplitFrontmatter:
    def test_extracts_block_and_body(self):
        raw, body = split_frontmatter("---\nname: a\n---\nhello\n")
        assert raw == "name: a\n"
        assert body == "hello\n"

    def test_no_frontmatter_returns_none(self):
        text = "# Just a heading\n"
        assert split_frontmatter(text) == (None, text)

    def test_delimiter_must_open_the_file(self):
        raw, _ = split_frontmatter("\n---\nname: a\n---\n")
        assert raw is None

    def test_handles_crlf_line_endings(self):
        raw, body = split_frontmatter("---\r\nname: a\r\n---\r\nhello\r\n")
        assert raw == "name: a\r\n"
        assert body == "hello\r\n"

    def test_accepts_yaml_document_end_delimiter(self):
        raw, _ = split_frontmatter("---\nname: a\n...\nbody\n")
        assert raw == "name: a\n"

    def test_unterminated_block_is_not_frontmatter(self):
        raw, _ = split_frontmatter("---\nname: a\nno closing delimiter\n")
        assert raw is None

    def test_body_may_contain_horizontal_rules(self):
        raw, body = split_frontmatter("---\nname: a\n---\nintro\n\n---\n\nmore\n")
        assert raw == "name: a\n"
        assert "more" in body


class TestParseSkillFile:
    def _parse(self, tmp_path: Path, contents: str, rel: str = "skills/demo/SKILL.md"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return parse_skill_file(path, tmp_path)

    def test_reads_name_description_and_path(self, tmp_path: Path):
        skill = self._parse(tmp_path, skill_md("pdf-tools", "Extracts tables from PDFs."))
        assert skill.name == "pdf-tools"
        assert skill.description == "Extracts tables from PDFs."
        assert skill.path == "skills/demo/SKILL.md"
        assert skill.issues == ()

    def test_path_is_posix_relative_to_repo_root(self, tmp_path: Path):
        skill = self._parse(tmp_path, skill_md(), rel=".claude/skills/deep/nested/SKILL.md")
        assert skill.path == ".claude/skills/deep/nested/SKILL.md"

    def test_directory_property(self, tmp_path: Path):
        skill = self._parse(tmp_path, skill_md())
        assert skill.directory == "skills/demo"

    def test_root_level_skill_directory_is_dot(self, tmp_path: Path):
        skill = self._parse(tmp_path, skill_md(), rel="SKILL.md")
        assert skill.directory == "."

    def test_multiline_description_is_collapsed(self, tmp_path: Path):
        contents = "---\nname: a\ndescription: >\n  first line\n  second line\n---\nbody\n"
        assert self._parse(tmp_path, contents).description == "first line second line"

    def test_quoted_and_unicode_values_survive(self, tmp_path: Path):
        contents = '---\nname: "café: tools"\ndescription: "Uses a colon: yes"\n---\n'
        skill = self._parse(tmp_path, contents)
        assert skill.name == "café: tools"
        assert skill.description == "Uses a colon: yes"

    def test_leading_bom_does_not_hide_frontmatter(self, tmp_path: Path):
        path = tmp_path / "SKILL.md"
        path.write_text(skill_md("bom-skill"), encoding="utf-8-sig")
        skill = parse_skill_file(path, tmp_path)
        assert skill.name == "bom-skill"
        assert skill.issues == ()

    def test_missing_frontmatter_is_reported_not_dropped(self, tmp_path: Path):
        skill = self._parse(tmp_path, "# Demo\n\nNo frontmatter here.\n")
        assert skill.name == "demo"  # falls back to the directory name
        assert skill.description == ""
        assert skill.issues == ("has no YAML frontmatter",)

    def test_missing_name_falls_back_to_directory(self, tmp_path: Path):
        skill = self._parse(tmp_path, "---\ndescription: Only a description.\n---\n")
        assert skill.name == "demo"
        assert skill.description == "Only a description."
        assert skill.issues == ("frontmatter is missing 'name'",)

    def test_missing_description_is_flagged(self, tmp_path: Path):
        skill = self._parse(tmp_path, "---\nname: solo\n---\n")
        assert skill.name == "solo"
        assert skill.issues == ("frontmatter is missing 'description'",)

    def test_invalid_yaml_is_flagged(self, tmp_path: Path):
        skill = self._parse(tmp_path, "---\nname: [unclosed\n---\nbody\n")
        assert skill.name == "demo"
        assert len(skill.issues) == 1
        assert "invalid YAML frontmatter" in skill.issues[0]

    def test_non_mapping_frontmatter_is_flagged(self, tmp_path: Path):
        skill = self._parse(tmp_path, "---\n- just\n- a list\n---\nbody\n")
        assert skill.issues == ("frontmatter is not a YAML mapping",)

    def test_non_scalar_name_falls_back_and_is_distinguished_from_absent(self, tmp_path: Path):
        skill = self._parse(tmp_path, "---\nname:\n  nested: value\ndescription: d\n---\n")
        assert skill.name == "demo"
        assert skill.issues == ("frontmatter 'name' is empty or not text",)

    def test_empty_frontmatter_block_reports_both_fields(self, tmp_path: Path):
        skill = self._parse(tmp_path, "---\n---\nbody\n")
        assert skill.name == "demo"
        assert skill.issues == (
            "frontmatter is missing 'name'",
            "frontmatter is missing 'description'",
        )

    def test_invalid_utf8_is_flagged(self, tmp_path: Path):
        path = tmp_path / "skills" / "demo" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"---\nname: \xff\xfe broken\n---\n")
        skill = parse_skill_file(path, tmp_path)
        assert skill.issues == ("is not valid UTF-8",)


class TestFindSkills:
    def test_finds_skills_across_the_tree(self, make_repo):
        root = make_repo(
            {
                ".claude/skills/alpha/SKILL.md": skill_md("alpha", "First."),
                "plugins/bravo/SKILL.md": skill_md("bravo", "Second."),
                "SKILL.md": skill_md("root", "At the root."),
            }
        )
        # Ordered by path, so dot-directories come first.
        assert [s.name for s in find_skills(root)] == ["alpha", "root", "bravo"]

    def test_results_are_sorted_by_path(self, make_repo):
        root = make_repo(
            {
                "z/SKILL.md": skill_md("z"),
                "a/SKILL.md": skill_md("a"),
                "m/SKILL.md": skill_md("m"),
            }
        )
        assert [s.path for s in find_skills(root)] == ["a/SKILL.md", "m/SKILL.md", "z/SKILL.md"]

    @pytest.mark.parametrize("excluded", [".git", "node_modules", ".venv", "__pycache__", "dist"])
    def test_noise_directories_are_skipped(self, make_repo, excluded: str):
        root = make_repo(
            {
                f"{excluded}/vendored/SKILL.md": skill_md("vendored"),
                "skills/real/SKILL.md": skill_md("real"),
            }
        )
        assert [s.name for s in find_skills(root)] == ["real"]

    def test_filename_match_is_case_insensitive(self, make_repo):
        root = make_repo({"skills/a/Skill.md": skill_md("lowercase-file")})
        assert [s.name for s in find_skills(root)] == ["lowercase-file"]

    def test_other_markdown_is_ignored(self, make_repo):
        root = make_repo(
            {
                "README.md": "# readme\n",
                "AGENTS.md": "---\nname: not-a-skill\n---\n",
                ".claude/commands/foo.md": "---\nname: a-command\n---\n",
                "skills/real/SKILL.md": skill_md("real"),
            }
        )
        assert [s.name for s in find_skills(root)] == ["real"]

    def test_empty_repository_yields_no_skills(self, make_repo):
        assert find_skills(make_repo({"README.md": "# nothing here\n"})) == []
