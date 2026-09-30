#!/usr/bin/env bash
#
# One git worktree + one Docker Sandboxes container per feature, so two features can be
# developed in parallel without sharing a checkout, a .venv or a report.html.
#
#   scripts/sbx-feature.sh start <feature>   create/attach the worktree and run claude in it
#   scripts/sbx-feature.sh start -d <f>      set it up but do not attach
#   scripts/sbx-feature.sh list              show every feature worktree and its sandbox
#   scripts/sbx-feature.sh stop  <feature>   stop the container, keep the worktree
#   scripts/sbx-feature.sh rm    <feature>   remove container, worktree and branch
#
# To run two features at once, start each one with -d and then attach to each from its own
# terminal with "start <feature>"; re-running start on an existing feature is safe.
#
# Run it from Git Bash on Windows (or any bash on macOS/Linux). The model is reached through
# the JetBrains Central proxy on the host -- see central_env() for why that needs a policy
# rule but no port forwarding.
#
set -euo pipefail

usage() {
	sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'
	exit "${1:-2}"
}

die() { printf 'sbx-feature: %s\n' "$*" >&2; exit 1; }

# --- host tooling -----------------------------------------------------------------------

# sbx ships into %LOCALAPPDATA% and its installer does not always put it on PATH, so fall
# back to the known location rather than failing with a bare "command not found".
find_sbx() {
	if command -v sbx >/dev/null 2>&1; then command -v sbx; return 0; fi
	local base="$HOME/AppData/Local"
	if [ -n "${LOCALAPPDATA:-}" ] && command -v cygpath >/dev/null 2>&1; then
		base="$(cygpath -u "$LOCALAPPDATA")"
	fi
	local cand="$base/DockerSandboxes/bin/sbx.exe"
	[ -x "$cand" ] || return 1
	printf '%s\n' "$cand"
}

# Read one field out of a JSON file without taking a jq dependency: jq if it is there,
# otherwise python, which this project already requires.
json_field() {
	local file="$1" key="$2"
	[ -f "$file" ] || return 1
	if command -v jq >/dev/null 2>&1; then
		jq -r --arg k "$key" '.[$k] // empty' "$file" 2>/dev/null
	else
		python -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8")).get(sys.argv[2],"") or "")' \
			"$file" "$key" 2>/dev/null
	fi
}

# --- Central auth -----------------------------------------------------------------------

# The live port is in proxy.pid. config.json only records the *configured* port, so reading
# config alone silently misses a proxy that bound somewhere else.
central_port() {
	local dir="$HOME/.jetbrains-central" port=""
	port="$(json_field "$dir/proxy.pid" port || true)"
	[ -n "$port" ] || port="$(json_field "$dir/config.json" proxy_port || true)"
	printf '%s\n' "${port:-19516}"
}

# Export the two variables Claude Code needs, without ever putting the key in argv: the
# caller passes them to sbx as bare "-e NAME", which copies the value out of this process's
# environment. A "-e NAME=$KEY" would leak the key into ps output and shell history.
#
# Central binds 127.0.0.1 only, so the container cannot dial it directly -- but every
# sandbox egresses through sbx's own proxy, which resolves host.docker.internal on the host
# side and therefore *can* reach host loopback. All that is missing is an allow rule.
central_env() {
	local port key
	port="$(central_port)"
	command -v central >/dev/null 2>&1 || die "central not on PATH; install the JetBrains Central CLI"
	key="$(central proxy start --ensure-updated --caller agent --return-key)"
	[ -n "$key" ] || die "central returned an empty proxy key"
	export ANTHROPIC_API_KEY="$key"
	export ANTHROPIC_BASE_URL="http://host.docker.internal:${port}/wire/${key}/claude-code/anthropic"
	CENTRAL_PORT="$port"
}

# --- worktree ---------------------------------------------------------------------------

# Always resolve the *main* worktree, never the current one: running this from inside one
# feature worktree must not nest the next feature underneath it, and the relative gitdir
# written below is only correct relative to the main checkout. --show-toplevel would return
# whichever worktree we happen to be standing in; --git-common-dir always names the real
# repository, and is plain ".git" when we are already at the top of it.
main_worktree() {
	local common
	common="$(git rev-parse --git-common-dir)" || die "not inside a git repository"
	(cd "$(dirname "$common")" && pwd)
}

# --- worktree helpers --------------------------------------------------------------------

# A freshly added worktree is unusable inside the container for two reasons, both fixed here
# once at creation time:
#
#  1. Its .git is a *file* holding an absolute gitdir. On Windows that reads
#     "C:/Users/.../.git/worktrees/<f>", which is not a path inside a Linux container, so git
#     there fails with "not a git repository". A *relative* gitdir resolves correctly on the
#     host and in the container, because sbx mounts the repo's .git at its mirrored absolute
#     path -- so no GIT_DIR override is needed on either side.
#  2. Git for Windows sets core.autocrlf=true in its *system* config, which does not exist in
#     the container. The host checks files out as CRLF while container git compares against
#     LF blobs, so untouched files read as modified there and "git commit -a" would commit a
#     CRLF copy of the whole tree. Pinning autocrlf per worktree makes both sides agree.
prepare_worktree() {
	local wt="$1" feature="$2" repo_dir="$3"

	printf 'gitdir: ../%s/.git/worktrees/%s\n' "$repo_dir" "$feature" > "$wt/.git"

	# --worktree needs the extension enabled once; it is repo-wide but changes no behaviour
	# on its own, and it keeps the main worktree's CRLF checkout untouched. Worktrees share
	# one config file, so setting it through the new worktree lands in the same place.
	git -C "$wt" config extensions.worktreeConfig true
	git -C "$wt" config --worktree core.autocrlf false
	# Re-materialise the tracked files under the new setting. Only ever safe on a worktree we
	# just created -- it overwrites the working copy, so it must not run on an existing one.
	# -u is load-bearing. "git worktree add" checked the tree out as CRLF and recorded those
	# byte counts in the index; rewriting the files as LF makes every cached size wrong by one
	# byte per line. Git treats a size mismatch as proof of modification and never compares
	# content, so without -u the whole tree reads as modified forever and no flavour of
	# "--refresh" clears it. -u writes the new stat data as the files are checked out.
	git -C "$wt" checkout-index -u -a -f
}

# --- commands ---------------------------------------------------------------------------

cmd_start() {
	local detached=""
	# Starting both features detached, then attaching to each in its own terminal, is the
	# normal way to get two of them going at once.
	while [ "${1:-}" = "-d" ] || [ "${1:-}" = "--detached" ]; do detached=1; shift; done

	local feature="${1:-}"
	[ -n "$feature" ] || usage
	# sbx sandbox names are stricter than git branch names: two or more characters, opening
	# with a letter or digit, and only letters, digits, hyphens and periods after that. The
	# name is used for the branch, the worktree directory and the sandbox, so it has to
	# satisfy the strictest of the three.
	case "$feature" in
		*[!A-Za-z0-9.-]* | [!A-Za-z0-9]* | ?) die "invalid feature name: use 2+ chars of letters, digits, '.' or '-', starting with a letter or digit" ;;
	esac

	local SBX root repo_dir wt
	SBX="$(find_sbx)" || die "sbx not found; install Docker Sandboxes or set it on PATH"
	root="$(main_worktree)"
	repo_dir="$(basename "$root")"
	wt="$(dirname "$root")/$feature"

	[ "$feature" != "$repo_dir" ] || die "feature name must differ from the repo directory"

	if [ -e "$wt" ]; then
		printf 'worktree %s already exists, reusing it\n' "$feature"
	else
		# A new branch per feature is what keeps two parallel features from fighting over refs.
		git -C "$root" worktree add "$wt" >/dev/null
		prepare_worktree "$wt" "$feature" "$repo_dir"
		printf 'created worktree %s on branch %s\n' "$feature" "$feature"
	fi

	central_env

	# Create before allowing: a --sandbox policy rule needs the sandbox to exist, so doing
	# this in one "sbx run" leaves the rule unapplied and the agent gets a 403 from the proxy
	# the first time it calls the model.
	#
	# The worktree is the agent's workspace; the repo's .git has to come along as a second
	# workspace or the relative gitdir above has nothing to point at.
	if ! "$SBX" ls -q 2>/dev/null | grep -Fxq "$feature"; then
		"$SBX" create --name "$feature" \
			-e ANTHROPIC_API_KEY \
			-e ANTHROPIC_BASE_URL \
			claude "$wt" "$root/.git"
	fi

	# Scope the rules to this sandbox: a global allow would open host loopback to every
	# sandbox on the machine, including ones belonging to other projects.
	#
	# "localhost" is the rule that actually matches. The sandbox dials host.docker.internal,
	# but sbx's egress proxy resolves that name on the host and evaluates policy against the
	# resolved name, so a host.docker.internal rule alone still gets "Blocked by network
	# policy: domain localhost:<port>". Both names are registered so this keeps working if
	# that normalisation ever changes. Only the host's loopback port is opened, and requests
	# the container makes to its *own* localhost never reach the proxy at all -- no_proxy
	# covers them -- so this does not expose anything inside the sandbox.
	local host
	for host in "localhost:${CENTRAL_PORT}" "host.docker.internal:${CENTRAL_PORT}"; do
		"$SBX" policy allow network "$host" --sandbox "$feature" >/dev/null ||
			die "could not allow egress to the Central proxy ($host) for sandbox $feature"
	done

	if [ -n "$detached" ]; then
		printf '%s is ready (Central proxy on :%s); attach with: %s start %s\n' \
			"$feature" "$CENTRAL_PORT" "$0" "$feature"
		return 0
	fi

	printf 'attaching to sandbox %s (Central proxy on :%s)\n' "$feature" "$CENTRAL_PORT"
	# Re-pass the credentials on attach: the proxy key is re-read every start and a stale one
	# baked in at create time would fail once it rotates.
	exec "$SBX" run --name "$feature" \
		-e ANTHROPIC_API_KEY \
		-e ANTHROPIC_BASE_URL
}

cmd_list() {
	local SBX
	SBX="$(find_sbx)" || die "sbx not found; install Docker Sandboxes or set it on PATH"
	printf '== worktrees ==\n'
	git worktree list
	printf '\n== sandboxes ==\n'
	"$SBX" ls
}

cmd_stop() {
	local feature="${1:-}" SBX
	[ -n "$feature" ] || usage
	SBX="$(find_sbx)" || die "sbx not found; install Docker Sandboxes or set it on PATH"
	"$SBX" stop "$feature"
	printf 'stopped %s; the worktree is untouched\n' "$feature"
}

cmd_rm() {
	local feature="${1:-}" SBX root
	[ -n "$feature" ] || usage
	SBX="$(find_sbx)" || die "sbx not found; install Docker Sandboxes or set it on PATH"
	root="$(main_worktree)"

	"$SBX" rm -f "$feature" >/dev/null 2>&1 || true
	# Refuse to discard work: let git object to a dirty worktree instead of forcing.
	git -C "$root" worktree remove "$(dirname "$root")/$feature"
	git -C "$root" branch -d "$feature" 2>/dev/null ||
		printf 'branch %s kept (not merged); delete it with: git branch -D %s\n' "$feature" "$feature"
	printf 'removed sandbox and worktree %s\n' "$feature"
}

case "${1:-}" in
	start) shift; cmd_start "$@" ;;
	list)  shift; cmd_list "$@" ;;
	stop)  shift; cmd_stop "$@" ;;
	rm)    shift; cmd_rm "$@" ;;
	-h|--help|help) usage 0 ;;
	*) usage ;;
esac
