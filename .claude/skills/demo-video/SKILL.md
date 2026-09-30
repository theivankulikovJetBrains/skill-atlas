---
name: demo-video
description: Run skill-atlas locally through every user scenario in spec/cli.md and film the run as demo.mp4. Use when asked to run or demo the app end to end, to exercise all scenarios or all the flags, to record a video/screencast/demo/GIF of it working, or to show a change working in the real app rather than in tests.
---

# Run every scenario and film it

One command runs the app for real — real `git` fetches, real reports on disk, real HTTP
against the real report server — and turns the run into `demo-run/demo.mp4`.

It is a **verifier that films itself**, not a screen recorder. Every frame carries the
assertion that decides that scenario, so a red run is a finding to report rather than a nice
video of a broken app. The exit code is 0 only when every scenario passed *and* a video came
out.

```bash
python .claude/skills/demo-video/scripts/record_demo.py
```

Run it from the checkout you want to demo. No worktree is needed — the script only generates
artifacts, it never edits the tree, so `CLAUDE.md`'s isolation rule does not apply. Use the
**Bash** tool (or PowerShell with `python`); it needs `uv`, `git` and Edge, all already
present. Any Python 3.11+ on PATH will do — it drives `uv run` rather than importing the app,
so it does not need the project's 3.14 venv.

About **70 seconds** warm. A first run adds a clone of the public repo and, if there is no
`ffmpeg` on PATH, a one-off ~30 MB `imageio-ffmpeg` download.

| Flag | Why |
|---|---|
| `--offline` | skip the one over-the-network scenario; everything else uses local fixtures |
| `--repo <url>` | a different public repo for that scenario (default `anthropics/skills`) |
| `--project <path>` | film a different checkout, e.g. a feature worktree |
| `--out-dir <path>` | somewhere other than `<project>/demo-run` |
| `--workers <n>` | concurrent Edge launches (default 4); drop to 1 if frames come out blank |
| `--keep` | leave the scratch directory behind, which is how you debug a bad frame |
| `--edge <path>` | if `msedge.exe` is somewhere unusual |

`--fps`, `--timeout`, `--work-dir` and `--quiet` are there too; `--help` lists everything.

`--offline` is green, not degraded: the report steps fall back to the `atlas` fixture, and they
take their search query from the first row of whatever report they are filming rather than
naming a skill, so the same step reads against either one. Keep new steps that way — a count
only the public repo has is a step that breaks the moment the network is gone or that repo
gains a skill.

## What you get

Two files, about 3 MB:

```
demo-run/
  demo.mp4          the film: title card, every scenario, a summary of all of them
  transcript.md     every command, its output, and every assertion with its verdict
```

The last run: **37 scenarios, 145 assertions, 2m17s of film** at 1280×900 — and 36 of 36 under
`--offline`, which drops only the over-the-network scan.

**Read `transcript.md` and report from it.** Say how many scenarios were green, name any that
were not, and quote the assertion that failed. Do not describe the video as proof that the app
works — the assertions are the proof, and the transcript is where they are written down. Point
the user at `demo-run/demo.mp4`; offer to open it rather than opening it unasked.

## Nothing is left behind, and nothing is visible to git

Everything intermediate — the fixture repositories, each with a `.git` of its own, the
generated reports, 57 frame PNGs, the Edge profiles — is built in a fresh temp directory
**outside the checkout**, so none of it is visible to git even while the run is going, and it
is deleted as soon as the video exists. The run prints where the scratch was and confirms it
went. `--keep` suspends that for debugging and says where to look.

What lands in the checkout is the video and the transcript, under a `demo-run/` that
`.gitignore` covers, and last run's copies are deleted by name before this one starts. If
`--out-dir` puts them somewhere git *can* see, the run says so on stderr rather than letting a
3 MB video turn up in `git status`.

## Keeping "every scenario" true

`spec/cli.md` is the behaviour contract, and the scenario list in `record_demo.py` is a
reading of it — so **before running, check the two still agree**. Behaviour the spec describes
and the script does not exercise is a gap to close in the same change, not something to
mention afterwards:

- `cli_scenarios()` holds one entry per flag, exit code and console claim.
- `ui_steps()` holds one entry per claim the spec makes about the report — the search box, the
  comparison matrix, the groups, the no-JavaScript fallback, the 200-skill ceiling.
- `build_fixtures()` builds the repositories those scenarios need. Anything that depends on a
  tag, a duplicated skill name or an exact skill count belongs there, not in a public repo
  whose contents can change under the run.

Adding a scenario is a few lines: a `Check`-bearing entry in the list, and the numbers it
asserts. Keep the captions in the same voice as the commit subjects — what the scenario
proves, in the imperative.

## How it works, and why each part is like that

Four decisions in `record_demo.py` look arbitrary and are not. Preserve them:

- **Edge, not Chrome.** Chrome forwards the command line to an already-running instance and
  `--screenshot` silently produces nothing.
- **`--screenshot` and `--dump-dom` in one launch.** The picture and the assertions then come
  out of the same render and cannot disagree. The driver leaves its results in a hidden
  `<pre id="demo-checks">`, which the dumped DOM carries back.
- **`--screenshot` photographs the top of the document however far down the page has
  scrolled.** Two things follow. A section below the fold has to be moved into the viewport —
  `ui.hoist('.compare')`. And the smooth scroll that *Compare similarity* starts has to be
  neutralised (`scroll-behavior: auto !important`, then `scrollTo(0, 0)` once the step is
  done), because a page photographed mid-scroll comes back with a blank band where nothing was
  ever painted. That blank band is what this looked like before the guard went in.
- **`CREATE_NO_WINDOW` for the serve-and-open scenario.** Serving is gated on
  `stdout.isatty()`, so a captured pipe never reaches it; a child with its own console is a
  terminal without a window appearing on anyone's desktop. `$BROWSER` points at a recorder
  script, which is how the run proves the browser was handed the port that was actually bound
  without a real tab opening. Python honours `$BROWSER` on every platform.

The video is assembled by `ffmpeg` — from PATH, or from `imageio-ffmpeg`, which the script
fetches through `uv run --with` if it has to. With no ffmpeg at all it falls back to an
animated GIF through Pillow, and says so.

## When something is off

- **A blank or half-drawn frame.** Almost always the page was scrolled when it was
  photographed — see the scrolling note above. Re-run with `--keep --workers 1` and open that
  frame's HTML from the scratch directory in a browser to see what the driver left on screen.
- **`could not delete …`.** Something still has the file open — a media player holding
  `demo.mp4` for the output, Edge's crash handler for the scratch. The first is fatal, because
  the alternative is filming over a video that cannot be replaced; the second only leaves a
  directory in `%TEMP%` and the run says which one.
- **`driver error` in the checks.** The injected step JavaScript threw — usually a selector
  the template renamed. The report's own script is the reference for what exists.
- **A UI scenario reported as "skipped: its report was never written".** Its CLI scenario
  failed first; fix that one and the frame comes back.
- **A count assertion off by a few** after the fixtures change. The fixture is the source of
  truth for those numbers, so update the expectation rather than the repository — unless the
  repository is what you meant to change.
- **Red because of the app, not the harness.** Then you have found a bug. Report it with the
  failing assertion, and treat it the way `AGENTS.md` treats a failing test: something to fix,
  never something to loosen.
