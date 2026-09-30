# Isolation — read this before editing anything

**Every task gets its own git worktree and its own sandbox container.** Never edit the shared
main checkout. `AGENTS.md` is the guide to the project itself; this file governs only *where*
you are allowed to work, and it is not optional — a `PreToolUse` hook
(`.claude/hooks/require-worktree.sh`) denies `Edit`, `Write` and `NotebookEdit` against the
main checkout, so ignoring it produces a blocked tool call rather than a mess.

## Start of every task

1. `git worktree list`. If a worktree already exists for this task, call **EnterWorktree**
   with its `path` and work there.
2. Otherwise ask the user to run it, because `sbx` is not on the agent's PATH and the
   launcher needs the JetBrains Central proxy:

   ```
   ! scripts/sbx-feature.sh start -d <feature>
   ```

   The feature name becomes the branch, the sibling directory `../<feature>` and the sandbox
   name at once, so it is limited to 2+ characters of letters, digits, `.` or `-`, starting
   with a letter or digit.
3. Call **EnterWorktree** with `path: ../<feature>`, then do the work.

Do not `git checkout -b` in the main checkout instead. A branch switch there carries every
uncommitted change in the tree onto the new branch, which is exactly the collision the
worktrees prevent.

One worktree per session: EnterWorktree can enter a sibling worktree once from the launch
directory, but switching again afterwards only works for worktrees under `.claude/worktrees/`.
For a second feature, start a second session.

## What stays editable in the main checkout

`CLAUDE.md`, `AGENTS.md`, `.gitignore`, `.gitattributes` and `.claude/**` — documentation and
policy, including the hook itself, so a broken gate can always be repaired. Everything else
(`src/**`, `tests/**`, `spec/**`, `scripts/**`, `pyproject.toml`) needs a worktree.

For a genuine one-off, the user — not you — can lift the gate for a session by exporting
`SKILL_ATLAS_ALLOW_MAIN_CHECKOUT=1` before starting Claude Code. Do not ask for it routinely.

## Known gaps

The hook sees `Edit`/`Write`/`NotebookEdit` only. A shell command (`sed -i`, a redirect, a
`python -c` that opens a file for writing) can still reach the main checkout, so treat the
gate as a guardrail against forgetting, not a sandbox boundary. Write through the file tools
and it holds.
