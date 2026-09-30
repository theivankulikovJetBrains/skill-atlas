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

The suite finishes in seconds — there is no reason to skip it or to run a subset as a final
check. A few tests probe the filesystem and skip themselves where it cannot oblige: one
needs case-sensitive names, another needs symlinks. A skip on Windows or macOS that does
not appear on Linux is usually one of those, not a failure hiding.

Add dependencies with `uv add <pkg>` (or `uv add --dev <pkg>`) so `uv.lock` moves with
`pyproject.toml`; CI runs `--locked` and fails on a stale lock.

## Sandboxed feature development

`scripts/sbx-feature.sh` gives each feature its own git worktree *and* its own Docker
Sandboxes container, so two features can be developed at once without sharing a checkout,
a `.venv` or a `report.html`. Needs `sbx` (Docker Sandboxes) and a running JetBrains
Central proxy.

| Task | Command |
|---|---|
| Start a feature and attach | `scripts/sbx-feature.sh start <feature>` |
| Set one up without attaching | `scripts/sbx-feature.sh start -d <feature>` |
| List worktrees and sandboxes | `scripts/sbx-feature.sh list` |
| Stop the container, keep the code | `scripts/sbx-feature.sh stop <feature>` |
| Throw the whole feature away | `scripts/sbx-feature.sh rm <feature>` |

For two in parallel, `start -d` each one and then attach from separate terminals. Re-running
`start` on an existing feature is safe: it reuses the worktree and sandbox and re-applies the
credentials.

The feature name becomes the branch, the sibling worktree directory (`../<feature>`) and the
sandbox name at once, so it is restricted to what `sbx` accepts: two or more characters of
letters, digits, `.` or `-`, starting with a letter or digit.

Two details in the script are load-bearing and easy to "simplify" into breakage:

- **The sandbox gets two workspaces**: the worktree and the repo's `.git`. A worktree's
  `.git` is a *file* pointing at `<repo>/.git/worktrees/<feature>`, so without the second
  mount git inside the container has nothing to resolve. The script rewrites that pointer to
  a **relative** path, which is what makes it valid on the host and in the container at the
  same time — `git worktree repair` rewrites it back to an absolute Windows path and breaks
  the container, so don't run it on a feature worktree.
- **Feature worktrees are pinned to LF** (`core.autocrlf=false` per worktree, via
  `extensions.worktreeConfig`). Git for Windows turns `autocrlf` on in its *system* config,
  which does not exist in the container: the host would check files out as CRLF while
  container git compares them against LF blobs, every untouched file would read as modified
  there, and `git commit -a` would commit a CRLF copy of the whole tree. The main worktree
  keeps its own checkout style.

Auth comes from Central, not from an Anthropic key. Each `start` re-reads the proxy key and
hands it to the sandbox as a bare `-e NAME`, which copies the value out of the environment
rather than putting it in `argv` where `ps` and shell history would catch it. Central binds
`127.0.0.1` only, but every sandbox egresses through sbx's own proxy, which resolves
`host.docker.internal` host-side and can therefore reach host loopback — so this needs a
per-sandbox policy rule and no port forwarding. The rule has to name `localhost:<port>`,
because policy is evaluated against the *resolved* name; a `host.docker.internal` rule alone
still yields `Blocked by network policy: domain localhost:<port>`.

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
scripts/sbx-feature.sh       one worktree + one sandbox per feature (host tooling, not shipped)
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
- `.gitattributes` pins `*.sh` to LF. Git for Windows would otherwise check the scripts out
  as CRLF, and bash reads the trailing `\r` as part of the command — the script dies with
  `$'\r': command not found` before doing anything. Nothing else is normalised, so the rest
  of the tree keeps whatever line endings your checkout already uses.
- `report.html` is the CLI's default output path, so it appears at the repo root as soon as
  anyone runs a scan. Never point a test or a scan at it as a scratch output — a stale copy
  on disk can make assertions pass against nothing. Write to a temp path instead, as CI
  does, and guard it with `test ! -e`.
- `.idea/` is tracked, including `workspace.xml`, so IDE state shows up as diff noise.
  Don't sweep those changes into an unrelated commit.
- A feature worktree's `.venv` is built *inside* its Linux sandbox, so `uv run` on the
  Windows host in that directory will not work against it — run the suite in the sandbox, or
  delete `.venv` first if you want a host one. `.gitignore` already covers it either way.
- Every feature sandbox mounts the *same* `<repo>/.git` read-write, which is what lets each
  one commit to its own branch. Git's own locking makes that safe for ordinary work, but it
  does mean two sandboxes share refs and objects: a `git gc`, a force-push or a branch
  deletion from inside one is visible to the other and to the host.
