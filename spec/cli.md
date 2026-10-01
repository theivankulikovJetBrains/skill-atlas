# Skill Atlas
CLI utility which scans a git repository and finds all AI agent skills.

## Usage
```
skill-atlas scan <git repo url> [--ref <branch|tag>] [-o <path>] [--no-html] [--port <n>] [--no-open]
                                [--similarity <0-1>] [--no-similar]
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
| `--port` | `8888` | port for the report server; `0` picks any free one |
| `--no-open` | off | leave the report closed instead of serving and opening it |
| `--similarity` | `0.45` | how alike two skills must read to be grouped as near-duplicates; `1` is identical |
| `--no-similar` | off | skip comparing skills: no grouping, and no comparison in the report |
| `--version` | | print version and exit |

## Detection
- A skill is a `SKILL.md` at any depth (filename matched case-insensitively); `name` and `description` come from its YAML frontmatter.
- `.git`, `node_modules`, `.venv`, `dist`, `build` and similar noise directories are skipped.
- Missing or malformed frontmatter is reported and flagged, not dropped; `name` falls back to the containing directory.

## Similar skills
After the scan, skills that read as near-duplicates of one another are grouped — the vendored copy under both `.agents/` and `.claude/`, the renamed fork, the same job written twice.

- Two skills are scored from their frontmatter only, on a `0.0`–`1.0` scale: `0.4 ×` fuzzy match of the names (separators and case folded away) `+ 0.6 ×` overlap of the description's content words, with boilerplate words like "use this skill when" discounted. When either description is missing the name decides alone, so a copy that lost its frontmatter still matches the original it was copied from.
- Pairs scoring at least `--similarity` are grouped; groups are connected components, so if A matches B and B matches C all three group together even when A and C never matched. A group's reported percentage is its strongest pair. Groups are ordered by score, then by path, so two scans of one commit produce the same report.
- Every skill also stays listed individually — a group points at something to look at, it does not decide which copy is redundant. A repo where nothing resembles anything gets no section at all, as does `--no-similar`.
- `--similarity` outside `0-1`, or not a number, is a usage error (exit `2`).

## Comparing skills by hand
Grouping answers "what looks duplicated?". The report also answers "how close are *these* two?", for any pair the reader picks — the same score, without a threshold in the way.

- Each row of the report's table carries a checkbox, and a **Compare similarity** button above the table opens a matrix of every pair among the ticked skills: names down the side and across the top, a percentage in each cell, the diagonal blank. Ticking one skill is not a comparison, so the button stays disabled until two are.
- A name shared by more than one skill in the selection — the vendored copy, which is the comparison people reach for first — gets its directory printed under it, and only then; a name unique in the selection stands alone.
- Cells are tinted on one blue ramp in five bands — under 10%, 10–24%, 25–44%, 45–69%, 70% and over — with a legend above the grid and the strongest band also bolded, so the ranking survives greyscale and colour blindness. The bands are read off real collections, not rounded: unrelated skills still score a few percent, a family of skills on one topic reaches the high thirties, and the step at 45% is the threshold above which a pair is grouped at all. The percentage in the cell is the answer; the tint only ranks it. Hovering a cell names both skills.
- The matrix follows the selection while it is open, and **Hide** closes it. The search box and the selection are independent: the header checkbox ticks every row the query left on screen, and a ticked row that a later query hides stays in the matrix — narrowing the list is not a way to drop a skill from a comparison.
- Scores for all `n × (n-1) / 2` pairs are computed during the scan and travel inside the report, so comparing costs no request and works from `file://`. Above **200 skills** the report leaves the comparison out altogether — the payload grows as the square of the collection, and a grid that large is not readable anyway; the groups, which are bounded by what actually resembles something, still appear. `--no-similar` drops it too, as does a repo with fewer than two skills.
- Without JavaScript the checkbox column and the button are not shown, for the same reason the search box is hidden: they would be controls that do nothing.

## Starring skills
A reader can star the skills they care about in the report and come back to them, without the CLI or the repository knowing anything about it.

- Each row carries a star (☆) beside its name; clicking it stars the skill (★) and clicking again unstars it. It is a toggle button labelled `Star <name>`, so it is reachable from the keyboard and announced as pressed or not.
- A **Starred** button in the toolbar narrows the table to the starred rows, and says how many there are — `Starred (3)`. It is disabled while nothing in the report is starred, except while it is on, so unstarring the last row never leaves the way back greyed out. It narrows together with the search box: a query while Starred is on searches only the stars. With Starred on and no query the count beside the box reads `2 of 20 starred`. An empty result has two messages. With nothing starred: *Nothing is starred yet*. With stars, but none matching the query: *No skill name contains `x` among the starred ones*. Unstarring a row while the filter is on drops it from the table straight away. The table keeps its path order either way; starring does not move a row.
- A star belongs to a skill's **path** in a **repository**. The two copies of a vendored skill are starred separately, and a skill renamed in its frontmatter keeps its star; one that moves does not. The repository is its browsable URL when the host is known, so the https and ssh spellings, with `.git` or without, share one set of stars. For other hosts it is the URL as given. Commit and ref play no part, so stars outlive the next push, and switching `--ref` keeps them. A star for a path the current scan does not have is kept, not forgotten, and is just not counted.
- Stars are stored in the browser (`localStorage`), never in the report file, which every scan overwrites. Storage belongs to the page's origin, so stars carry over between scans served on the same port; `--port 0`, a different port, or opening the file from disk starts from an empty set. A star placed in another tab of the same origin shows up without a reload. Where the browser refuses storage, stars last as long as the page does.
- The header checkbox of the comparison ticks the rows on screen, so with Starred on it ticks the starred ones.
- Without JavaScript the star column and the Starred button are not shown, for the same reason as the other controls. A report with no skills has neither. The console output does not show stars; they exist only in the browser.

## Output
- Console: skill name, repo-relative file path, description — plus the source URL and scanned commit. Any similar-skill groups follow the full list, each as a percentage, the distinct names in the group, and one line per member path.
- `report.html`, written to the working directory and overwritten if present: standalone (no external assets), UTF-8, light/dark. On github/gitlab/bitbucket/codeberg URLs each skill links to its file at the scanned commit. Similar-skill groups appear as cards below the table, outside it so the search box keeps filtering one list of rows, and the group count joins the summary bar at the top. Between the two sits the pairwise comparison described above, empty until it is asked for.
- The report has a search box above the table, focused on load: typing narrows the table to the skills whose **name** contains what was typed, case-insensitively, keystroke by keystroke. A match count sits beside the box, Escape clears the query, and a query that matches nothing says so in place of the rows. It filters the rows already in the page — no request goes back to the server — and it is absent from a report with no skills. With **Starred** on it searches only the starred rows (see above). The box is hidden until its script runs, so a reader without JavaScript sees the full table rather than a control that does nothing.
- The written report is then served at `http://localhost:8888/`, or at `--port <n>` — `0` asks the OS for any free port, and the URL printed and opened always names the port actually bound. A port outside `0-65535`, or one that is not a whole number, is a usage error (exit `2`) rather than a bind failure after the scan. The served URL is what opens in the default browser. The server listens on `127.0.0.1` only, holds a copy of the report in memory, and answers `/` alone — every other path is a 404, so files sitting next to the report are never published. It keeps serving, so the page can be reloaded, until Ctrl+C; that ends the scan with exit `0`. Skipped by `--no-open`, by `--no-html`, when stdout is not a terminal (a pipe or CI, where the wait would hang the step), and on Linux without `DISPLAY`/`WAYLAND_DISPLAY` — where `webbrowser` would otherwise reach for lynx and seize the terminal. Neither a port already in use nor a browser that will not start is an error: the report is already on disk, and the reason goes to stderr.

## Exit codes
`0` success, including a repo with no skills. `1` clone failed or report unwritable. `2` bad arguments.

## Fetch
Shallow, blobless, no working tree; then only `SKILL.md` files are materialised, in one batched fetch. A repo whose full checkout is gigabytes costs a few MB — `JetBrains/kotlin` scans in ~5s. Hooks are disabled via an empty template dir, so git-lfs's `post-checkout` cannot trip git 2.45 clone protection. Servers without partial-clone support fall back to an unfiltered clone. The temp directory is deleted after the scan.

## Requirements
Python ≥3.14 and `git` on PATH. Credential prompts are disabled, so private repos need ambient auth such as a credential helper.

## CI
GitHub Actions, on push to `main` and every PR: `uv sync --locked` then `pytest`, on Linux/Windows/macOS. A second job builds the wheel and scans this repository through it — offline, and the only check that catches a packaged data file going missing. A third runs the suite once more on Linux with a headless browser photographing the report at each check made against it, and uploads the frames as an artifact whether the run passed or failed, so a red check comes with the picture that shows why. Collection is off in a plain `pytest`; the verdict comes from the markup either way, and no frame is compared against a baseline image.
