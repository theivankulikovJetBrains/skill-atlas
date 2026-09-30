# Isolation — read this before editing anything

**Every task gets its own git worktree and its own sandbox container.** Never edit the shared
main checkout. `AGENTS.md` is the guide to the project itself; this file governs only *where*
you are allowed to work, and it is not optional — a `PreToolUse` hook
(`.claude/hooks/require-worktree.sh`) denies `Edit`, `Write` and `NotebookEdit` against the
main checkout, so ignoring it produces a blocked tool call rather than a mess.

## Start of every task

1. `git worktree list`. If a worktree already exists for this task, call **EnterWorktree**
   with its `path` and work there.
2. Otherwise start it yourself — do not stop and wait for the user. Run this through the
   **Bash** tool, which is Git Bash:

   ```
   scripts/sbx-feature.sh start -d <feature>
   ```

   `-d` is required. Without it the script ends in `exec sbx run`, an interactive attach that
   has no terminal to attach to and hangs the tool call.

   `sbx` not being on PATH is not a blocker: `find_sbx()` falls back to
   `%LOCALAPPDATA%\DockerSandboxes\bin\sbx.exe`, and `central` — the other thing the launcher
   needs — is on PATH already. Both work from the agent's shell.

   Use the **Bash** tool, not **PowerShell**. PowerShell has no handler for `.sh`, and a bare
   `bash` there resolves to the WSL shim in `WindowsApps`, where `cygpath` and `%LOCALAPPDATA%`
   are missing, so `find_sbx()` cannot reach `sbx.exe` and the script dies. If you do need it
   from the PowerShell side, spell the interpreter out:
   `& 'C:\Program Files\Git\bin\bash.exe' scripts/sbx-feature.sh start -d <feature>`.

   The feature name becomes the branch, the sibling directory `../<feature>` and the sandbox
   name at once, so it is limited to 2+ characters of letters, digits, `.` or `-`, starting
   with a letter or digit. Derive one from the task rather than asking.
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
