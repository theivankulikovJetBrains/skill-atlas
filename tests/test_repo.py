from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import nfc, skill_md

from skill_atlas.repo import RepoError, clone, web_base_url
from skill_atlas.scanner import find_skills


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/widgets", "https://github.com/acme/widgets"),
        ("https://github.com/acme/widgets.git", "https://github.com/acme/widgets"),
        ("https://github.com/acme/widgets/", "https://github.com/acme/widgets"),
        ("git@github.com:acme/widgets.git", "https://github.com/acme/widgets"),
        ("ssh://git@github.com/acme/widgets.git", "https://github.com/acme/widgets"),
        ("https://GitHub.com/acme/widgets", "https://github.com/acme/widgets"),
        ("https://gitlab.com/acme/group/widgets", "https://gitlab.com/acme/group/widgets"),
        ("  https://github.com/acme/widgets  ", "https://github.com/acme/widgets"),
    ],
)
def test_web_base_url_for_known_hosts(url: str, expected: str):
    assert web_base_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://git.internal.example.com/acme/widgets.git",  # unknown host layout
        "https://github.com/acme",  # user page, not a repo
        "/tmp/local/path",
        "not a url",
        "",
    ],
)
def test_web_base_url_declines_when_layout_is_unknown(url: str):
    assert web_base_url(url) is None


def _commit_repo(root: Path, files: dict[str, str], message: str = "initial") -> Path:
    for rel, contents in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=Test", *args],
            check=True,
            capture_output=True,
        )

    root.mkdir(parents=True, exist_ok=True)
    git("init", "--quiet", "--initial-branch=main")
    git("add", "-A")
    git("commit", "--quiet", "-m", message)
    return root


@pytest.fixture
def local_git_repo(tmp_path: Path) -> Path:
    """A real single-commit repository we can clone from, offline.

    Deliberately mixes skills with bulk content so tests can assert that only
    the skills are ever written to disk.
    """
    return _commit_repo(
        tmp_path / "origin",
        {
            ".claude/skills/alpha/SKILL.md": skill_md("alpha"),
            "SKILL.md": skill_md("root-level"),
            "skills/lower/Skill.md": skill_md("case-variant"),
            "README.md": "# not a skill\n",
            "src/bulk.bin": "x" * 200_000,
        },
    )


@pytest.fixture
def awkward_git_repo(tmp_path: Path) -> Path:
    """A repository whose layout stresses the sparse-checkout pattern itself.

    Duplicated skills under both agent directories, plus a directory name with a
    space and a non-ASCII character -- none of which the gitignore-style pattern
    ``**/[Ss][Kk][Ii][Ll][Ll].[Mm][Dd]`` may quietly fail to match.
    """
    return _commit_repo(
        tmp_path / "awkward-origin",
        {
            ".agents/skills/deploy/SKILL.md": skill_md("deploy", "From .agents."),
            ".claude/skills/deploy/SKILL.md": skill_md("deploy", "From .claude."),
            "my skills/café/SKILL.md": skill_md("odd"),
            "plugins/ops/node_modules/pkg/SKILL.md": skill_md("vendored"),
            "docs/README.md": "# not a skill\n",
        },
    )


class TestClone:
    def test_yields_a_working_copy_and_head_commit(self, local_git_repo: Path):
        with clone(local_git_repo.as_uri()) as checkout:
            assert (checkout.path / ".claude" / "skills" / "alpha" / "SKILL.md").is_file()
            assert checkout.commit is not None
            assert len(checkout.commit) == 40

    def test_temp_directory_is_removed_on_exit(self, local_git_repo: Path):
        with clone(local_git_repo.as_uri()) as checkout:
            path = checkout.path
            assert path.exists()
        assert not path.exists()

    def test_temp_directory_is_removed_when_body_raises(self, local_git_repo: Path):
        captured: list[Path] = []
        with pytest.raises(ZeroDivisionError):
            with clone(local_git_repo.as_uri()) as checkout:
                captured.append(checkout.path)
                raise ZeroDivisionError
        assert not captured[0].exists()

    def test_explicit_ref_is_recorded(self, local_git_repo: Path):
        with clone(local_git_repo.as_uri(), ref="main") as checkout:
            assert checkout.ref == "main"

    def test_unknown_ref_raises_repo_error(self, local_git_repo: Path):
        with pytest.raises(RepoError, match="could not clone"):
            with clone(local_git_repo.as_uri(), ref="no-such-branch"):
                pass

    def test_missing_repository_raises_repo_error(self, tmp_path: Path):
        with pytest.raises(RepoError, match="could not clone"):
            with clone((tmp_path / "does-not-exist").as_uri()):
                pass

    def test_repository_without_skills_clones_cleanly(self, tmp_path: Path):
        origin = _commit_repo(tmp_path / "bare-origin", {"README.md": "# nothing\n"})
        with clone(origin.as_uri()) as checkout:
            assert find_skills(checkout.path) == []

    def test_missing_git_binary_is_reported_clearly(self, monkeypatch: pytest.MonkeyPatch):
        def boom(*_args, **_kwargs):
            raise FileNotFoundError

        monkeypatch.setattr(subprocess, "run", boom)
        with pytest.raises(RepoError, match="git was not found on PATH"):
            with clone("https://github.com/acme/widgets.git"):
                pass


class TestFetchesOnlySkills:
    """The working tree must contain skills and nothing else."""

    def _worktree_files(self, root: Path) -> set[str]:
        return {
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(root).parts
        }

    def test_only_skill_files_are_written_to_disk(self, local_git_repo: Path):
        with clone(local_git_repo.as_uri()) as checkout:
            assert self._worktree_files(checkout.path) == {
                ".claude/skills/alpha/SKILL.md",
                "SKILL.md",
                "skills/lower/Skill.md",
            }

    def test_bulk_content_is_never_materialised(self, local_git_repo: Path):
        with clone(local_git_repo.as_uri()) as checkout:
            assert not (checkout.path / "src" / "bulk.bin").exists()
            assert not (checkout.path / "README.md").exists()

    def test_skills_at_every_depth_and_casing_are_found(self, local_git_repo: Path):
        with clone(local_git_repo.as_uri()) as checkout:
            names = {s.name for s in find_skills(checkout.path)}
        assert names == {"alpha", "root-level", "case-variant"}

    def test_duplicated_and_awkwardly_placed_skills_all_arrive(self, awkward_git_repo: Path):
        with clone(awkward_git_repo.as_uri()) as checkout:
            materialised = {nfc(p) for p in self._worktree_files(checkout.path)}
        assert materialised == {
            ".agents/skills/deploy/SKILL.md",
            ".claude/skills/deploy/SKILL.md",
            "my skills/café/SKILL.md",
            "plugins/ops/node_modules/pkg/SKILL.md",
        }

    def test_the_scan_keeps_both_copies_and_drops_the_vendored_one(self, awkward_git_repo: Path):
        """git fetches every SKILL.md; the scanner is what rules node_modules out."""
        with clone(awkward_git_repo.as_uri()) as checkout:
            skills = find_skills(checkout.path)
        assert [nfc(s.path) for s in skills] == [
            ".agents/skills/deploy/SKILL.md",
            ".claude/skills/deploy/SKILL.md",
            "my skills/café/SKILL.md",
        ]
        assert [s.description for s in skills[:2]] == ["From .agents.", "From .claude."]

    def test_no_hooks_are_left_runnable_in_the_clone(self, local_git_repo: Path):
        """An empty template dir means git installs no hooks at all to begin with."""
        with clone(local_git_repo.as_uri()) as checkout:
            hooks = checkout.path / ".git" / "hooks"
            active = [] if not hooks.exists() else [h.name for h in hooks.iterdir() if h.suffix != ".sample"]
        assert active == []
