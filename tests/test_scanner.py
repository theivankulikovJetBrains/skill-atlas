from __future__ import annotations

from pathlib import Path

import pytest
from conftest import nfc, skill_md

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

    def test_trailing_spaces_on_the_delimiters_are_tolerated(self):
        raw, body = split_frontmatter("---  \nname: a\n---\t\nhello\n")
        assert raw == "name: a\n"
        assert body == "hello\n"

    def test_a_longer_run_of_dashes_is_not_a_delimiter(self):
        """``----`` opens a setext-style rule, not frontmatter; the whole file is body."""
        assert split_frontmatter("----\nname: a\n----\n")[0] is None

    def test_empty_file_has_no_frontmatter(self):
        assert split_frontmatter("") == (None, "")


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

    def test_unreadable_file_is_reported_not_raised(self, tmp_path: Path):
        """A *directory* called SKILL.md is the portable way to make open() fail:
        IsADirectoryError on POSIX, PermissionError on Windows -- both OSError, and
        their strerror differs, so only the prefix is worth asserting."""
        path = tmp_path / "skills" / "demo" / "SKILL.md"
        path.mkdir(parents=True)
        skill = parse_skill_file(path, tmp_path)
        assert skill.name == "demo"
        assert len(skill.issues) == 1
        assert skill.issues[0].startswith("could not be read:")

    def test_empty_file_is_reported_not_dropped(self, tmp_path: Path):
        skill = self._parse(tmp_path, "")
        assert skill.name == "demo"
        assert skill.issues == ("has no YAML frontmatter",)

    def test_repeated_key_takes_the_last_value(self, tmp_path: Path):
        """PyYAML resolves a duplicate key silently rather than raising; the skill
        must still come back whole instead of turning into a parse failure."""
        skill = self._parse(tmp_path, "---\nname: first\nname: second\ndescription: d\n---\n")
        assert skill.name == "second"
        assert skill.issues == ()

    def test_non_scalar_description_is_distinguished_from_absent(self, tmp_path: Path):
        skill = self._parse(tmp_path, "---\nname: a\ndescription:\n  - one\n  - two\n---\n")
        assert skill.description == ""
        assert skill.issues == ("frontmatter 'description' is empty or not text",)

    def test_explicit_null_reads_as_present_but_empty(self, tmp_path: Path):
        skill = self._parse(tmp_path, "---\nname:\ndescription: d\n---\n")
        assert skill.name == "demo"
        assert skill.issues == ("frontmatter 'name' is empty or not text",)

    def test_numeric_values_are_rendered_as_text(self, tmp_path: Path):
        """YAML types a bare ``2024`` as an int; the report only ever prints strings."""
        skill = self._parse(tmp_path, "---\nname: 2024\ndescription: 3.5\n---\n")
        assert (skill.name, skill.description) == ("2024", "3.5")
        assert skill.issues == ()

    def test_surrounding_whitespace_is_trimmed(self, tmp_path: Path):
        skill = self._parse(tmp_path, "---\nname: '   spaced   '\ndescription: \"  padded  \"\n---\n")
        assert skill.name == "spaced"
        assert skill.description == "padded"


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

    def test_a_missing_root_yields_no_skills(self, tmp_path: Path):
        assert find_skills(tmp_path / "does-not-exist") == []

    def test_root_may_be_given_as_a_string(self, make_repo):
        root = make_repo({"skills/a/SKILL.md": skill_md("a")})
        assert [s.name for s in find_skills(str(root))] == ["a"]


class TestDuplicateSkills:
    """Two copies of one skill are two findings, never one.

    A repository that keeps skills under both ``.agents/`` and ``.claude/`` is the
    normal case, and the whole point of the report is to show when those copies have
    drifted apart -- so nothing here may deduplicate by name.
    """

    def test_the_same_skill_in_agents_and_claude_is_reported_twice(self, make_repo):
        root = make_repo(
            {
                ".agents/skills/deploy/SKILL.md": skill_md("deploy", "From .agents."),
                ".claude/skills/deploy/SKILL.md": skill_md("deploy", "From .claude."),
            }
        )
        skills = find_skills(root)
        assert [s.path for s in skills] == [
            ".agents/skills/deploy/SKILL.md",
            ".claude/skills/deploy/SKILL.md",
        ]
        assert [s.name for s in skills] == ["deploy", "deploy"]
        assert [s.description for s in skills] == ["From .agents.", "From .claude."]

    def test_byte_identical_copies_are_both_kept(self, make_repo):
        contents = skill_md("deploy", "Ships it.")
        root = make_repo(
            {
                ".agents/skills/deploy/SKILL.md": contents,
                ".claude/skills/deploy/SKILL.md": contents,
            }
        )
        assert len(find_skills(root)) == 2

    def test_copies_across_many_roots_stay_ordered_by_path(self, make_repo):
        root = make_repo(
            {
                ".claude/skills/deploy/SKILL.md": skill_md("deploy"),
                ".agents/skills/deploy/SKILL.md": skill_md("deploy"),
                "plugins/ops/skills/deploy/SKILL.md": skill_md("deploy"),
                "skills/deploy/SKILL.md": skill_md("deploy"),
            }
        )
        assert [s.path for s in find_skills(root)] == [
            ".agents/skills/deploy/SKILL.md",
            ".claude/skills/deploy/SKILL.md",
            "plugins/ops/skills/deploy/SKILL.md",
            "skills/deploy/SKILL.md",
        ]

    def test_drifted_copies_keep_their_own_metadata_and_issues(self, make_repo):
        """The stale copy is the one worth finding, so its problems must not be
        smoothed over by the healthy one sharing its name."""
        root = make_repo(
            {
                ".agents/skills/deploy/SKILL.md": skill_md("deploy", "Current."),
                ".claude/skills/deploy/SKILL.md": "---\nname: deploy\n---\n",
            }
        )
        fresh, stale = find_skills(root)
        assert (fresh.description, fresh.issues) == ("Current.", ())
        assert stale.description == ""
        assert stale.issues == ("frontmatter is missing 'description'",)

    def test_one_directory_may_hold_two_spellings_of_the_filename(self, make_repo, case_sensitive_fs):
        """On Linux ``SKILL.md`` and ``skill.md`` are two files; both are skills."""
        if not case_sensitive_fs:
            pytest.skip("filesystem is case-insensitive; the two names are one file here")
        root = make_repo(
            {
                "skills/a/SKILL.md": skill_md("upper"),
                "skills/a/skill.md": skill_md("lower"),
            }
        )
        assert [s.name for s in find_skills(root)] == ["upper", "lower"]


class TestUnusualLayout:
    #: Legal on Linux, macOS and Windows alike -- no <>:"/\|?* and no trailing dot or space.
    ODD_DIRECTORIES = [
        "my skills",
        "skills (v2)",
        "skills.d",
        "a.b.c",
        "-leading-dash",
        "sk!ll#s",
        "café",
        "под_папка",
        "emoji \U0001f600",
    ]

    @pytest.mark.parametrize("directory", ODD_DIRECTORIES)
    def test_odd_directory_names_are_walked_and_reported_verbatim(self, make_repo, directory: str):
        root = make_repo({f"{directory}/SKILL.md": skill_md("odd", "Lives somewhere strange.")})
        skill = find_skills(root)[0]
        assert nfc(skill.path) == f"{directory}/SKILL.md"
        assert nfc(skill.directory) == directory
        assert skill.name == "odd"

    @pytest.mark.parametrize("directory", ODD_DIRECTORIES)
    def test_an_odd_directory_is_also_the_name_fallback(self, make_repo, directory: str):
        root = make_repo({f"{directory}/SKILL.md": "# no frontmatter\n"})
        assert nfc(find_skills(root)[0].name) == directory

    @pytest.mark.parametrize(
        "directory", ["distribution", "build-tools", "node_modules.bak", "target-audience", "envoy", "buildkite"]
    )
    def test_directories_merely_resembling_noise_are_still_scanned(self, make_repo, directory: str):
        """Exclusion matches a whole directory name, not a prefix: ``build-tools`` is
        not ``build``, and ``envoy`` is not ``env``."""
        root = make_repo({f"{directory}/skills/a/SKILL.md": skill_md("real")})
        assert [s.name for s in find_skills(root)] == ["real"]

    def test_noise_directories_are_skipped_at_any_depth(self, make_repo):
        root = make_repo(
            {
                "plugins/ops/node_modules/pkg/SKILL.md": skill_md("vendored"),
                "plugins/ops/SKILL.md": skill_md("real"),
            }
        )
        assert [s.name for s in find_skills(root)] == ["real"]

    def test_a_vendored_dot_claude_tree_goes_with_its_parent(self, make_repo):
        """``.claude`` is scanned, but not once ``node_modules`` has ruled the branch out."""
        root = make_repo({"node_modules/pkg/.claude/skills/a/SKILL.md": skill_md("vendored")})
        assert find_skills(root) == []

    @pytest.mark.parametrize("marker", [".agents", ".claude", ".cursor", ".github", ".config"])
    def test_agent_dot_directories_are_not_treated_as_noise(self, make_repo, marker: str):
        root = make_repo({f"{marker}/skills/a/SKILL.md": skill_md("real")})
        assert [s.name for s in find_skills(root)] == ["real"]

    def test_a_directory_named_skill_md_is_not_a_skill(self, make_repo):
        """os.walk lists it under dirnames; matching there would mean reading a directory."""
        root = make_repo({"skills/real/SKILL.md": skill_md("real")})
        decoy = root / "docs" / "SKILL.md"
        decoy.mkdir(parents=True)
        (decoy / "notes.md").write_text("not a skill\n", encoding="utf-8")
        assert [s.name for s in find_skills(root)] == ["real"]

    def test_deeply_nested_skills_are_found(self, make_repo):
        deep = "/".join("abcdefghij")
        root = make_repo({f"{deep}/SKILL.md": skill_md("deep")})
        assert [s.path for s in find_skills(root)] == [f"{deep}/SKILL.md"]

    def test_an_excluded_name_as_the_scan_root_is_still_scanned(self, make_repo):
        """Exclusion filters children only. The root is whatever clone() handed us, and
        a repository is free to be called ``dist``."""
        root = make_repo({"skills/real/SKILL.md": skill_md("real")}, name="dist")
        assert [s.name for s in find_skills(root)] == ["real"]

    def test_a_root_level_skill_without_a_name_falls_back_to_the_repo_directory(self, make_repo):
        root = make_repo({"SKILL.md": "---\ndescription: At the top.\n---\n"}, name="my-repo")
        skill = find_skills(root)[0]
        assert skill.name == "my-repo"
        assert skill.directory == "."

    def test_a_symlink_loop_does_not_hang_the_walk(self, make_repo):
        """os.walk does not follow links, so a self-referential tree still terminates."""
        root = make_repo({"skills/a/SKILL.md": skill_md("a")})
        try:
            (root / "skills" / "loop").symlink_to(root, target_is_directory=True)
        except OSError as exc:  # Windows without Developer Mode or admin rights
            pytest.skip(f"symlinks are not available here: {exc}")
        assert [s.name for s in find_skills(root)] == ["a"]
