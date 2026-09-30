"""Fetch a git repository into a temporary working copy."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class RepoError(Exception):
    """Raised when a repository cannot be fetched."""


@dataclass(frozen=True)
class Checkout:
    """A local working copy of the scanned repository."""

    path: Path
    commit: str | None
    ref: str | None


_GIT_ENV = {
    # Never block on a credential prompt for a private or misspelled repo.
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
}

_URL_RE = re.compile(
    r"""^(?:
        (?P<scheme>https?|git|ssh)://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+)
        |git@(?P<ssh_host>[^:]+):(?P<ssh_path>.+)
    )$""",
    re.VERBOSE,
)


def web_base_url(url: str) -> str | None:
    """Best-effort browsable base URL, used to link skill files in the report.

    Returns ``None`` for hosts whose URL layout we cannot assume.
    """
    match = _URL_RE.match(url.strip())
    if match is None:
        return None
    host = match.group("host") or match.group("ssh_host")
    path = match.group("path") or match.group("ssh_path")
    if not host or not path:
        return None
    path = path.removesuffix(".git").strip("/")
    known_hosts = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")
    if host.lower() not in known_hosts or path.count("/") < 1:
        return None
    return f"https://{host.lower()}/{path}"


def _run_git(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, **_GIT_ENV},
            check=False,
        )
    except FileNotFoundError as exc:
        raise RepoError("git was not found on PATH; install git and try again.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RepoError(f"git timed out after {exc.timeout:.0f}s.") from exc


def _on_rm_error(func, path, _exc_info) -> None:
    """Clear the read-only bit git sets inside .git so cleanup can proceed."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


#: Gitignore-style pattern for the only files we need on disk. Spelled with
#: character classes so the match stays case-insensitive on case-sensitive
#: filesystems, matching what the scanner accepts.
_SPARSE_PATTERN = "**/[Ss][Kk][Ii][Ll][Ll].[Mm][Dd]"


@contextmanager
def clone(url: str, ref: str | None = None, timeout: float | None = 300.0) -> Iterator[Checkout]:
    """Fetch just enough of ``url`` to scan it, into a temp dir removed on exit.

    Clones shallow, blobless and with no working tree, then materialises only
    SKILL.md files. A repository whose full checkout is gigabytes costs a few
    megabytes here, and no file outside the pattern is ever downloaded.

    Raises :class:`RepoError` if git is unavailable or the fetch fails.
    """
    parent = Path(tempfile.mkdtemp(prefix="skill-atlas-"))
    target = parent / "repo"
    # An empty directory pointed at by core.hooksPath: nothing the repository or
    # a filter (git-lfs installs a post-checkout hook) plants can be run.
    no_hooks = parent / "nohooks"
    try:
        no_hooks.mkdir()
        _clone_metadata(url, target, ref, timeout)
        _checkout_skill_files(target, no_hooks, timeout)
        yield Checkout(path=target, commit=_head_commit(target), ref=ref)
    finally:
        _remove_tree(parent)


def _clone_metadata(url: str, target: Path, ref: str | None, timeout: float | None) -> None:
    """Clone commit and tree objects only -- no blobs, no working tree."""
    base = ["-c", "init.templateDir=", "clone", "--depth", "1", "--single-branch", "--no-checkout", "--quiet"]
    if ref:
        base += ["--branch", ref]
    tail = ["--", url, str(target)]

    result = _run_git([*base, "--filter=blob:none", *tail], timeout=timeout)
    if result.returncode == 0:
        return

    # Servers without partial-clone support reject --filter; retry without it so
    # those hosts still work, at the cost of downloading every blob.
    blobless_error = _detail(result)
    if target.exists():
        _remove_tree(target)
    retry = _run_git([*base, *tail], timeout=timeout)
    if retry.returncode != 0:
        raise RepoError(f"could not clone {url}\n{blobless_error}")


def _checkout_skill_files(repo: Path, no_hooks: Path, timeout: float | None) -> None:
    """Write only SKILL.md files into the working tree, in one batched fetch."""
    steps = (
        ["-C", str(repo), "sparse-checkout", "set", "--no-cone", _SPARSE_PATTERN],
        ["-C", str(repo), "-c", f"core.hooksPath={no_hooks.as_posix()}", "checkout", "--quiet"],
    )
    for args in steps:
        result = _run_git(args, timeout=timeout)
        if result.returncode != 0:
            raise RepoError(f"cloned {repo.name} but could not read its files\n{_detail(result)}")


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout).strip() or f"git exited with {result.returncode}"


def _remove_tree(path: Path) -> None:
    """Delete the temp clone; never let cleanup trouble mask the real error."""
    try:
        shutil.rmtree(path, onexc=_on_rm_error)
    except OSError:
        shutil.rmtree(path, ignore_errors=True)


def _head_commit(repo: Path) -> str | None:
    result = _run_git(["-C", str(repo), "rev-parse", "HEAD"], timeout=30.0)
    return result.stdout.strip() or None if result.returncode == 0 else None
