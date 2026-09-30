# Skill Atlas
CLI utility which scans a git repository and finds all AI agent skills.

## Usage
```
skill-atlas scan <git repo url> [--ref <branch|tag>] [-o <path>] [--no-html] [--no-open]
```
Not installed globally; run through uv:
```
uv run skill-atlas scan https://github.com/anthropics/skills.git
uv run --project <repo> skill-atlas scan <url>     # from any other directory
```

| Flag | Default | Meaning |
|---|---|---|
| `--ref` | default branch | branch or tag to scan |
| `-o`, `--out` | `./report.html` | HTML report destination |
| `--no-html` | off | terminal output only |
| `--no-open` | off | leave the report closed instead of opening it |
| `--version` | | print version and exit |

## Detection
- A skill is a `SKILL.md` at any depth (filename matched case-insensitively); `name` and `description` come from its YAML frontmatter.
- `.git`, `node_modules`, `.venv`, `dist`, `build` and similar noise directories are skipped.
- Missing or malformed frontmatter is reported and flagged, not dropped; `name` falls back to the containing directory.

## Output
- Console: skill name, repo-relative file path, description — plus the source URL and scanned commit.
- `report.html`, written to the working directory and overwritten if present: standalone (no external assets), UTF-8, light/dark. On github/gitlab/bitbucket/codeberg URLs each skill links to its file at the scanned commit.
- The written report then opens in the default browser. Skipped by `--no-open`, by `--no-html`, when stdout is not a terminal (a pipe or CI), and on Linux without `DISPLAY`/`WAYLAND_DISPLAY` — where `webbrowser` would otherwise reach for lynx and seize the terminal. A browser that will not start is not an error: the report is already on disk.

## Exit codes
`0` success, including a repo with no skills. `1` clone failed or report unwritable. `2` bad arguments.

## Fetch
Shallow, blobless, no working tree; then only `SKILL.md` files are materialised, in one batched fetch. A repo whose full checkout is gigabytes costs a few MB — `JetBrains/kotlin` scans in ~5s. Hooks are disabled via an empty template dir, so git-lfs's `post-checkout` cannot trip git 2.45 clone protection. Servers without partial-clone support fall back to an unfiltered clone. The temp directory is deleted after the scan.

## Requirements
Python ≥3.14 and `git` on PATH. Credential prompts are disabled, so private repos need ambient auth such as a credential helper.

## CI
GitHub Actions, on push to `main` and every PR: `uv sync --locked` then `pytest`, on Linux/Windows/macOS. A second job builds the wheel and scans this repository through it — offline, and the only check that catches a packaged data file going missing.
