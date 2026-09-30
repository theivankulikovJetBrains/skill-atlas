# skill-atlas — guide for agents

A CLI that clones a git repository and reports every Claude Agent Skill (`SKILL.md`) it
contains: a terminal summary plus a standalone HTML report.

**`spec/cli.md` is the behaviour contract.** Read it before touching flags, detection rules,
output or exit codes, and update it in the same change whenever behaviour moves. If the code
and the spec disagree, that is a bug in one of them — decide which, don't leave both.

## Commands

Nothing is installed globally; everything runs through `uv` (Python ≥3.14, `git` on PATH).

| Task | Command |
|---|---|
| Install / sync deps | `uv sync --locked` |
| Tests | `uv run pytest` |
| One test file or case | `uv run pytest tests/test_scanner.py -k frontmatter` |
| Run the CLI | `uv run skill-atlas scan <git repo url>` |
| Build sdist + wheel | `uv build` |

The suite is 200 tests and finishes in about 9 seconds — there is no reason to skip it or to
run a subset as a final check. Two of them probe the filesystem and skip themselves where it
cannot oblige: one needs case-sensitive names, the other needs symlinks.

Add dependencies with `uv add <pkg>` (or `uv add --dev <pkg>`) so `uv.lock` moves with
`pyproject.toml`; CI runs `--locked` and fails on a stale lock.

## Layout

```
src/skill_atlas/
  cli.py                     argparse wiring, exit codes, the report server and open policy
  repo.py                    clone(): shallow/blobless/sparse fetch into a temp dir
  scanner.py                 find_skills(): walk a tree, parse SKILL.md frontmatter
  report.py                  format_console() and write_html()
  models.py                  Skill and ScanResult dataclasses
  templates/report.html.j2   the HTML report (Jinja2, packaged as data)
tests/                       one test module per source module; conftest.py has make_repo
spec/cli.md                  the behaviour contract
```

Data flows one way: `repo` → `scanner` → `models` → `report` → `cli`. Keep it that way;
nothing below `cli.py` should print, and nothing should reach the network outside `repo.py` —
the report server in `cli.py` binds `127.0.0.1` and serves one in-memory document.

## Conventions

Follow what the existing modules already do rather than importing a new style:

- `from __future__ import annotations` at the top of every module; full type hints on
  public functions.
- Comments and docstrings explain **why**, not what. The codebase is deliberately dense
  with rationale for non-obvious choices (why hooks are disabled during clone, why stdio is
  reconfigured, why a browser is not opened under a pipe). Preserve that when editing near
  it, and match it when adding code — a subtle workaround without its reason will be
  "simplified" away by the next reader.
- Stdlib first. Runtime dependencies are `jinja2` and `pyyaml` only; don't add a third
  without a reason that survives the question "can stdlib do this?"
- Frozen dataclasses for values (`Skill`, `Checkout`), plain dataclasses for the mutable
  aggregate (`ScanResult`).
- Failures are values, not crashes: a malformed `SKILL.md` becomes a `Skill` with `issues`,
  and only fetch/write failures raise (`RepoError`) or set a non-zero exit code.
- No formatter or linter is configured — match surrounding style by hand. Lines run to
  roughly 110 characters.

### Tests

- Build fixtures with the `make_repo` fixture and the `skill_md()` helper in
  `tests/conftest.py` instead of writing files by hand. Compare paths that contain
  non-ASCII through `nfc()` from the same module — macOS can report a directory name
  decomposed, so a bare `"café"` literal does not match what `os.walk` saw.
- **Tests must not hit the network.** `repo.py` is tested by driving real `git` against
  local `file://` repositories; keep new tests offline the same way. Loopback is not the
  network — the report-server tests really bind a socket, but always on port 0, because a
  fixed 8888 would fail on whatever machine already has something there.
- Both platforms matter: CI runs Linux, Windows and macOS. Watch for path separators
  (repo-relative paths are always POSIX-style), file locking on Windows, and console
  encoding — see `_harden_stdio` in `cli.py`.

## Definition of Done

1. `uv run pytest` is green locally.
2. If tests are red, fix them and re-run until they pass. A failing test is a finding to
   report, never something to delete, skip or loosen to get to green.
3. For changes to packaging, the template, or the CLI entry point, also reproduce the CI
   smoke job — it is the only check that catches a data file missing from the wheel:
   ```bash
   uv build
   out=/tmp/smoke/report.html
   uv run --isolated --no-project --python 3.14 --with dist/*.whl \
     skill-atlas scan "file://$PWD" -o "$out" --no-open
   grep -q "<title>Skill Atlas" "$out"
   ```
4. `spec/cli.md` matches the behaviour that now exists.
5. Commit and push. CI (GitHub Actions: `.github/workflows/ci.yml`) then runs the same
   `uv sync --locked` + `pytest` on three OSes, plus the wheel smoke test.
6. Confirm the pushed run is green and fix it if it is not. `gh` is **not** installed, but
   the run can still be read from the shell: Git Credential Manager holds a `github.com`
   token with `repo, workflow` scope, so query the Actions API directly.
   ```bash
   # GIT_TERMINAL_PROMPT/GCM_INTERACTIVE keep the helper from opening a GUI prompt if the
   # credential is ever missing -- without them this hangs instead of failing.
   token=$(printf 'protocol=https\nhost=github.com\n\n' |
     GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never git credential fill | sed -n 's/^password=//p')
   repo=theivankulikovJetBrains/skill-atlas
   curl -s -H "Authorization: Bearer $token" \
     "https://api.github.com/repos/$repo/actions/runs?per_page=1"   # .workflow_runs[0].conclusion
   ```
   Match `head_sha` against the commit you pushed — a `success` from an older run proves
   nothing. Add `/<run_id>/jobs` for per-OS detail when a run is red. The header is required:
   unauthenticated calls from this network get HTTP 403 (rate limit), not an answer. Never
   echo `$token`. Do not report CI as passing on the strength of a local run; say whether you
   checked the run itself or only ran the suite locally.

## Gotchas

- The root `.gitignore` covers generated output: `report.html`, `__pycache__/`, `*.py[cod]`,
  `dist/`, `.venv/` and `.pytest_cache/`. The bare `report.html` pattern matches at any
  depth, so generated reports left inside the package directory are ignored too. The
  template is `templates/report.html.j2` and is *not* matched — gitignore patterns match
  full names, not prefixes.
- `report.html` is the CLI's default output path, so it appears at the repo root as soon as
  anyone runs a scan. Never point a test or a scan at it as a scratch output — a stale copy
  on disk can make assertions pass against nothing. Write to a temp path instead, as CI
  does, and guard it with `test ! -e`.
- `.idea/` is tracked, including `workspace.xml`, so IDE state shows up as diff noise.
  Don't sweep those changes into an unrelated commit.
