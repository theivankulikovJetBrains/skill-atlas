#!/usr/bin/env bash
#
# PreToolUse gate: refuse to edit the shared main checkout.
#
# Every feature gets its own worktree and its own Docker Sandboxes container
# (scripts/sbx-feature.sh, AGENTS.md "Sandboxed feature development"). That was already the
# documented workflow and it still got skipped, because nothing enforced it: two features
# were developed in one checkout at once, one overwrote the other's edits, and a branch
# switch carried uncommitted work onto an unrelated branch. This hook is the enforcement.
#
# stdin is the PreToolUse payload; the verdict goes to stdout as a permission decision.
# Anything this script cannot prove is a violation is allowed -- a gate that fails closed on
# its own bugs would make the repo unusable, and the failure mode it guards against is
# collision between parallel features, not a security boundary.
set -uo pipefail

allow() { exit 0; }

deny() {
	# permissionDecisionReason is the only text the model sees, so it has to carry the whole
	# remedy: which command produces a worktree, and how to get into it afterwards.
	printf '%s' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",'
	printf '%s' '"permissionDecisionReason":"'"$1"'"}}'
	exit 0
}

# --- read the payload -------------------------------------------------------------------

payload="$(cat)"

# jq if it is installed, python otherwise -- the same fallback scripts/sbx-feature.sh uses,
# so the hook adds no dependency the project does not already have.
json_field() {
	if command -v jq >/dev/null 2>&1; then
		printf '%s' "$payload" | jq -r "$1 // empty" 2>/dev/null
	else
		printf '%s' "$payload" | python -c 'import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d = d.get(k) if isinstance(d, dict) else None
print(d or "")' "${1#.}" 2>/dev/null
	fi
}

file="$(json_field '.tool_input.file_path')"
[ -n "$file" ] || allow

# --- locate the file in git -------------------------------------------------------------

# Write creates files that do not exist yet, and can create their directory too, so walk up
# to the first ancestor that is actually on disk before asking git anything.
dir="$(dirname "$file")"
while [ -n "$dir" ] && [ ! -d "$dir" ]; do
	parent="$(dirname "$dir")"
	[ "$parent" != "$dir" ] || break
	dir="$parent"
done
[ -d "$dir" ] || allow

# Outside any repository (a scratch file in the temp dir, say) there is nothing to protect.
git_dir="$(git -C "$dir" rev-parse --absolute-git-dir 2>/dev/null)" || allow
[ -n "$git_dir" ] || allow

# The discriminator. In the main checkout the worktree's git dir *is* the common dir; in a
# linked worktree it is <common>/worktrees/<name>. --path-format=absolute (git >= 2.31) keeps
# both sides comparable regardless of which directory we were invoked from.
common_dir="$(git -C "$dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || allow
[ "$git_dir" = "$common_dir" ] || allow   # linked worktree -> this is what we want

# --- escape hatches ---------------------------------------------------------------------

# An explicit override, for the one-line fix that is not worth a container. Deliberately not
# a flag the model can set for itself: it has to come from the environment of the session.
[ "${SKILL_ATLAS_ALLOW_MAIN_CHECKOUT:-}" != "1" ] || allow

# The policy's own files stay editable in the main checkout. Without this a broken hook could
# not be repaired, and CLAUDE.md/AGENTS.md edits -- which describe the workflow rather than
# taking part in it -- would demand a sandbox to make a one-line documentation change.
# Both sides have to be in one path flavour before a prefix can be stripped. On Windows git
# answers "C:/Users/..." while the shell's own pwd says "/c/Users/...", so the comparison
# silently never matched and every allowlisted path was being denied. cygpath is the
# normaliser in Git Bash and is simply absent in the Linux sandbox, where both are already
# POSIX and the passthrough is correct.
norm() {
	if command -v cygpath >/dev/null 2>&1; then cygpath -u "$1"; else printf '%s' "$1"; fi
}
root="$(norm "$(dirname "$common_dir")")"
rel="$(norm "$file")"
rel="${rel#"$root"/}"
case "$rel" in
	CLAUDE.md | AGENTS.md | .gitignore | .gitattributes | .claude/*) allow ;;
esac

deny "Refusing to edit the shared main checkout: $rel\n\nThis repo develops every feature in its own worktree and sandbox, so parallel features cannot overwrite each other or drag uncommitted work across a branch switch. See AGENTS.md \\\"Sandboxed feature development\\\".\n\nAsk the user to run:  scripts/sbx-feature.sh start -d <feature>\nThen call EnterWorktree with path: ../<feature> and redo this edit there.\n\nEditable here without a worktree: CLAUDE.md, AGENTS.md, .gitignore, .gitattributes, .claude/**. To override for a single session, the user can export SKILL_ATLAS_ALLOW_MAIN_CHECKOUT=1 before starting Claude Code."
