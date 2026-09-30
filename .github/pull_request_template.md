<!--
A body here is prose, not a checklist. Fill the sections in, delete any heading you have
nothing to say under, and delete these comments with them.

Title in the imperative, same voice as the commit subjects: "Group skills that read as
near-duplicates", not "Added grouping".
-->

<!--
Open with why the change exists: what a reader or a developer runs into today, before any
description of the code. One or two sentences is usually enough.
-->


## What moved

<!--
The change itself, and then the decisions inside it that its shape did not make on its own:
a calibrated constant, a workaround, an option you chose against. Rationale belongs in the
PR for the same reason it belongs in the comments -- without it the next reader simplifies
it away. New or changed flags are worth a small table.
-->


## Verification

<!--
What you actually ran, not what ought to pass:

- `uv run pytest` -- N passed, M skipped.
- The CI smoke job (Definition of Done step 3) if packaging, `templates/report.html.j2` or
  the CLI entry point moved. It is the only check that catches a data file missing from
  the wheel.
- The run on this branch, linked. CI triggers on `pull_request`, so it starts when this
  opens. A local suite is not a green CI run -- say which one you are reporting.
- Anything you drove by hand that the suite cannot reach. The demo below covers the report
  in a real browser; say what else you tried.

Then whether `spec/cli.md` matches the behaviour that now exists. It is the contract; if
behaviour moved and the spec did not, one of them is wrong.
-->


## Demo

<!--
The scenarios this branch can reach, run for real against real repositories and filmed:

    python .claude/skills/demo-video/scripts/record_demo.py --diff

Replace this whole section with `demo-run/pr-section.md`, which the run writes ready to
paste: it already carries the tally, every scenario's verdict, and the list of scenarios the
diff does not reach and which therefore were not filmed. Then drag `demo-run/demo.gif` onto
the blank line it leaves for it. That upload is the one step that only works in this editor
-- GitHub has no API for attachments -- and a dropped GIF plays here in the body, where an
mp4 turns into a player somebody has to press.

A change that reaches no scenario at all -- tests, CI, prose -- gets told so by the run, and
saying that here is a better answer than a film of something unrelated.
-->


<!--
Before opening this, from the feature worktree (AGENTS.md, Definition of Done):
tests green, spec updated, branch pushed with `git push -u origin HEAD`.
After the run goes green: `scripts/sbx-feature.sh rm <feature>`.
-->
