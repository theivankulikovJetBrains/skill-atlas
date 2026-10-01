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
| Tests, photographing the report at each check | `uv run pytest --shots` |
| Run the CLI | `uv run skill-atlas scan <git repo url>` |
| Run every scenario and film it | `python .claude/skills/demo-video/scripts/record_demo.py` |
| Build sdist + wheel | `uv build` |

The suite finishes in seconds — there is no reason to skip it or to run a subset as a final
check. A few tests probe the filesystem and skip themselves where it cannot oblige: one
needs case-sensitive names, another needs symlinks. A skip on Windows or macOS that does
not appear on Linux is usually one of those, not a failure hiding.

Add dependencies with `uv add <pkg>` (or `uv add --dev <pkg>`) so `uv.lock` moves with
`pyproject.toml`; CI runs `--locked` and fails on a stale lock.

## Sandboxed feature development

**This is mandatory, not a convenience.** Every feature is developed in its own worktree and
sandbox; nothing is edited in the shared main checkout. `CLAUDE.md` states the rule for
agents and `.claude/hooks/require-worktree.sh` enforces it, denying `Edit`/`Write` against
the main checkout. The rule exists because the documentation alone did not hold: two features
were once developed in one checkout at the same time, one silently overwrote the other's
edits, and a `git checkout` carried uncommitted work onto an unrelated branch.

`scripts/sbx-feature.sh` gives each feature its own git worktree *and* its own Docker
Sandboxes container, so two features can be developed at once without sharing a checkout,
a `.venv` or a `report.html`. Needs `sbx` (Docker Sandboxes) and a running JetBrains
Central proxy.

Run it from Git Bash. PowerShell has no handler for `.sh`, and a bare `bash` there resolves
to the WSL shim in `WindowsApps`, where `cygpath` and `%LOCALAPPDATA%` are missing — so the
script cannot locate `sbx.exe` and exits with `sbx not found`. From PowerShell, name the
interpreter: `& 'C:\Program Files\Git\bin\bash.exe' scripts/sbx-feature.sh …`.

An AI agent session does not need to wait for a human here: everything the launcher needs
works from the agent's own Git Bash shell, so it can run `start -d <feature>` itself and go
straight on to the work. `-d` is not optional for that — plain `start` ends in
`exec sbx run`, an interactive attach with no terminal to attach to.

| Task | Command |
|---|---|
| Start a feature and attach | `scripts/sbx-feature.sh start <feature>` |
| Set one up without attaching | `scripts/sbx-feature.sh start -d <feature>` |
| List worktrees and sandboxes | `scripts/sbx-feature.sh list` |
| Stop the container, keep the code | `scripts/sbx-feature.sh stop <feature>` |
| Remove the container, worktree and branch | `scripts/sbx-feature.sh rm <feature>` |

For two in parallel, `start -d` each one and then attach from separate terminals. Re-running
`start` on an existing feature is safe: it reuses the worktree and sandbox and re-applies the
credentials. `rm` is how a feature normally ends rather than a way to abandon one — it is
step 8 of the Definition of Done, and `start -d` on the same name brings the worktree back on
the existing branch if review reopens the work.

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
  similarity.py              find_similar(): cluster skills that read as near-duplicates
  report.py                  format_console() and write_html()
  models.py                  Skill, SimilarGroup and ScanResult dataclasses
  templates/report.html.j2   the HTML report (Jinja2, packaged as data)
tests/                       one test module per source module; conftest.py has make_repo
tests/shots.py               screenshot collection: a frame per check made on the report
spec/cli.md                  the behaviour contract
scripts/sbx-feature.sh       one worktree + one sandbox per feature (host tooling, not shipped)
.claude/skills/demo-video/   runs every scenario in spec/cli.md and films it (host tooling too)
```

The suite pins behaviour; the `demo-video` skill pins the parts a unit test cannot reach — the
report's JavaScript in a real browser, and the server and browser-opening path that only
happens when stdout is a terminal. It asserts as it goes and exits non-zero on a red scenario,
so it is a check that happens to produce a video, not a screencast that happens to run the app.
Its scenario list is a reading of `spec/cli.md`: when behaviour moves, move that too.

Data flows one way: `repo` → `scanner` → `similarity` → `models` → `report` → `cli`. Keep it that way;
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
- **Tests must not touch the developer's desktop either.** Three autouse fixtures in
  `tests/conftest.py` see to that: `no_server_loop` keeps `serve_forever` from hanging the
  run, `opened` keeps `webbrowser.open` from launching a real tab, and `intact_stdio` hands
  the next test a `sys.stdout` that is still open. None of them goes red when it is missing,
  which is why all three are autouse rather than opt-in: what normally hides their absence
  is pytest's capture plugin — it makes `stdout.isatty()` false, so a scan skips the browser,
  and it reinstalls `sys.stdout` between tests, so a leaked stream never surfaces. Run with
  `-s` and both crutches go away. Ask for `opened` by name to assert on the URL.
- Keep the suite green under `-s` as well as plain `uv run pytest`. It is what a developer
  debugging a test reaches for, and it is the mode where anything the suite leaks into
  process-global state stops being invisible.
- Both platforms matter: CI runs Linux, Windows and macOS. Watch for path separators
  (repo-relative paths are always POSIX-style), file locking on Windows, and console
  encoding — see `_harden_stdio` in `cli.py`.

### Screenshots of the checks

`uv run pytest --shots` photographs the report at every check made against it, into a
gitignored `test-shots/` (`index.md` there lists each check, its verdict and its frame).
CI does the same in its own `report screenshots` job and uploads the result, so a red
check on a pull request comes with the picture that shows why. `tests/shots.py` explains
the mechanism; what matters when writing tests:

- **It is off unless asked for.** A plain `uv run pytest` starts no browser, and
  `page.check(...)` is then a bare `assert`. That is deliberate — the suite's promise to
  finish in seconds is worth more than always having the frames — so write report tests
  with `shots` freely; they cost a default run nothing.
- **The markup decides, never the picture.** Nothing is compared against a baseline
  image: fonts differ per runner, and these frames are evidence for a human reading a CI
  artifact. A check's verdict comes from the condition the test computed, exactly as
  before. A browser that will not start cannot fail a check that would otherwise pass —
  `--shots-strict` is how CI asks for the opposite, so a green run cannot ship an empty
  artifact.
- **Ask for a frame only where it is evidence.** A picture is worth a launch for a check
  about something a reader can see — that a hostile name is drawn as text, that the
  search box really is absent without JavaScript, that odd characters are glyphs and not
  mojibake. For a check on a script's source text or a `data-` attribute it is a second
  photograph of the same page; leave those as plain asserts.
- **`no_script=True` is not a way to simplify a picture.** It is the document a reader
  without JavaScript actually gets, and the only honest subject for the checks about what
  ships hidden: rendered as shipped, the report's own script reveals those elements
  before the shutter opens and the frame would contradict the check beside it.
- Spell the directory override `--shots-dir=PATH`, with the equals sign. pytest cannot
  know that flag takes a value until a conftest has loaded, so a separate argument that
  happens to be an existing directory is taken for a test path — which moves rootdir and
  then loads no conftest at all, and the flag comes back "unrecognized". Each flag has an
  environment variable behind it (`SKILL_ATLAS_SHOTS`, `SKILL_ATLAS_SHOTS_DIR`,
  `SKILL_ATLAS_SHOTS_STRICT`, `SKILL_ATLAS_SHOTS_BROWSER`) which has no such problem;
  that is what the CI job uses.
- This does not replace the `demo-video` skill, and the two do not overlap. These frames
  are of static documents and answer "does the report draw what the check says it
  contains". Whether the report's *JavaScript* works — typing in the search box, ticking
  rows, the matrix — is still that skill's question, and a new claim about behaviour
  belongs in its `ui_steps()`, not here.

### The demo GIF on a pull request

The `demo gif` job runs the `demo-video` skill with `--gif --offline`, publishes the film to
an orphan `demo-assets` branch and keeps one comment on the pull request linking it, so a
reviewer can watch the app work without checking the branch out.
`.github/scripts/publish-demo-gif.sh` does the publishing; the job is the skill, not a
second implementation of it, so a scenario added to `cli_scenarios()` or `ui_steps()` shows
up here with no workflow change.

- **Linked, not embedded, because the repository is private.** An `![](...)` pointing at
  `raw.githubusercontent.com` renders broken: that host answers 404 without a token, and
  GitHub does not proxy same-repo raw URLs through camo — it leaves them bare for the
  browser, which cannot authenticate to another domain. Measured, not assumed: 200 and
  3.5 MB with a token, 404 without. The comment therefore links
  `github.com/<repo>/blob/demo-assets/...`, which is on the session's own domain and
  animates the GIF in GitHub's viewer. **Do not "simplify" that link into an embed** unless
  the repository goes public, at which point an embed is the nicer thing and the script says
  so too.

- **It is a check, like `screenshots`.** The recorder exits non-zero when any scenario is
  red, which fails the job; the comment then *names* the red scenarios above the link,
  from `summary.json`. The GIF is evidence of a run and never the verdict — do not read a
  posted film as a green run, and do not report CI green off the strength of one.
- **Don't paste the film into the PR body.** The comment is updated in place, one per pull
  request, so a hand-pasted copy is a second thing to keep current. Nor should you run the
  skill by hand just to open a PR — the job does it.
- **The GIF lives on an orphan `demo-assets` branch**, never on `main`: GitHub has no API
  for attaching a file to a comment, so the film has to live at a URL, and the asset branch
  keeps generated output out of `main`'s history. Paths are keyed by head commit
  (`pr-<n>/<sha>.gif`) so the link names the commit it is a film of, and each run clears its
  own `pr-<n>/` first, so the branch holds about one GIF per pull request rather than one
  per push. Leave that branch alone; nothing reads it but the comments.
- **Fork pull requests skip the job.** Their `pull_request` token is read-only, so the push
  and the comment would both 403. They get the `demo-gif` artifact instead, and a
  maintainer can run the workflow by hand on the branch.
- **`--offline`, deliberately.** The one over-the-network scenario clones a public repo
  whose contents change, and this job comments on every push, so a flake there would post a
  wrong comment rather than cost a retryable tick.
- **It needs `--no-sandbox` on the runner**, for the same AppArmor reason `tests/shots.py`
  documents at length. `sandbox_flags()` in `record_demo.py` is a deliberate copy of that
  one; keep the two in step, because a frame CI cannot photograph is a frame the comment
  has to do without.

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
5. Commit and push the feature branch — `git push -u origin HEAD` from the worktree. Work
   does not land on `main` directly; the branch is what the pull request is opened from.
6. Open a pull request against `main`. This is part of finishing, not paperwork for later:
   CI (`.github/workflows/ci.yml`) triggers on `pull_request` and on pushes to `main` only,
   so a pushed feature branch with no PR gets **no run at all** and step 7 has nothing to
   confirm. `gh` is **not** installed, but Git Credential Manager holds a `github.com` token
   with `repo, workflow` scope, so the API can be driven straight from the shell.
   ```bash
   # GIT_TERMINAL_PROMPT/GCM_INTERACTIVE keep the helper from opening a GUI prompt if the
   # credential is ever missing -- without them this hangs instead of failing.
   token=$(printf 'protocol=https\nhost=github.com\n\n' |
     GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never git credential fill | sed -n 's/^password=//p')
   repo=theivankulikovJetBrains/skill-atlas
   # -d @- reads the body from the heredoc, so quotes and newlines never go through argv.
   curl -s -X POST -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
     "https://api.github.com/repos/$repo/pulls" -d @- <<'JSON'   # .html_url, .number
   {"title": "Stop the suite from opening real browser tabs",
    "head": "no-browser-in-tests",
    "base": "main",
    "body": "Why the change exists, then what moved.\n\n`uv run pytest` green on ..."}
   JSON
   ```
   `head` is the branch name, which is also the feature and worktree name. Title in the
   imperative, same voice as the commit subjects (`Group skills that read as near-duplicates`,
   not `Added grouping`). The body is prose, not a checklist: why the change exists first,
   then what moved, then what you verified — match PRs #1 and #2.
   `.github/pull_request_template.md` spells that shape out, but GitHub only pre-fills it in
   the web UI; a PR created through the API gets exactly the body you send, so follow the
   template by hand rather than assuming it applied. A second POST for a branch
   that already has an open PR returns 422 `A pull request already exists`; that is the
   correct outcome of a retry, so read the existing one from
   `?head=theivankulikovJetBrains:<branch>` rather than opening another. Merging the PR is the
   human's call unless they ask you to do it.
7. Confirm the PR's run is green and fix it if it is not. Reuse `$token` and `$repo`:
   ```bash
   curl -s -H "Authorization: Bearer $token" \
     "https://api.github.com/repos/$repo/actions/runs?branch=<feature>&per_page=1"
   ```
   `.workflow_runs[0].conclusion` is the answer; the same call without `branch=` gives the
   newest run repo-wide, which during parallel features is somebody else's. Match `head_sha`
   against the commit you pushed — a `success` from an older run proves nothing. Add
   `/<run_id>/jobs` for per-OS detail when a run is red. The header is required:
   unauthenticated calls from this network get HTTP 403 (rate limit), not an answer. Never
   echo `$token`. Do not report CI as passing on the strength of a local run; say whether you
   checked the run itself or only ran the suite locally.
8. Clean up the feature: once the run is green, throw away the sandbox and the worktree.
   Waiting for the merge is not worth it — the PR reads the branch from `origin`, so nothing
   local is still holding the work, and a worktree left behind is a second checkout of that
   branch for the next session to trip over. Call **ExitWorktree** first so the shell is back
   in the main checkout, then run one command from there:
   ```bash
   scripts/sbx-feature.sh rm <feature>
   ```
   Running `git worktree remove` from *inside* the worktree is the failure to avoid: git
   unregisters it and deletes the contents, then cannot delete the directory it is standing in
   ("Permission denied" on Windows), and the empty leftover makes the next
   `start <feature>` report "already exists, reusing it" and hand the sandbox a checkout with
   no `.git`. Ignored files (`.venv/`, `report.html`) do not get in the way, but anything
   genuinely untracked or modified makes the remove refuse instead of discarding it — that is
   a finding to look at and commit or delete deliberately, not something to force past. The
   last step is `git branch -d`, so an unmerged branch survives with a message saying so;
   leave it. Cleaning up costs nothing if review comes back: `start -d <feature>` re-adds the
   worktree and checks the existing branch straight back out.

## Gotchas

- The root `.gitignore` covers generated output: `report.html`, `/demo-run/`, `/test-shots/`,
  `__pycache__/`, `*.py[cod]`, `dist/`, `.venv/` and `.pytest_cache/`. The bare `report.html` pattern matches
  at any depth, so generated reports left inside the package directory are ignored too. The
  template is `templates/report.html.j2` and is *not* matched — gitignore patterns match
  full names, not prefixes. `/demo-run/` is anchored because it is one specific directory at
  the root; the `demo-video` skill builds everything else it needs outside the checkout.
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
