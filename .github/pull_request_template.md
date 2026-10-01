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
- For report or UI changes, what you drove in a browser. The suite can only pin markup.
  No need to paste a demo: the `demo gif` job films every scenario and keeps one inline
  comment on this PR up to date. Say whether you read it, and quote any scenario it
  reported red.

Then whether `spec/cli.md` matches the behaviour that now exists. It is the contract; if
behaviour moved and the spec did not, one of them is wrong.
-->


<!--
Before opening this, from the feature worktree (AGENTS.md, Definition of Done):
tests green, spec updated, branch pushed with `git push -u origin HEAD`.
After the run goes green: `scripts/sbx-feature.sh rm <feature>`.
-->
