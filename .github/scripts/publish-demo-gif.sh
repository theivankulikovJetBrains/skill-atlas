#!/usr/bin/env bash
#
# Put the demo GIF where a reviewer can watch it, and point one comment at it.
#
# GitHub has no API for attaching a file to a comment -- the drag-and-drop uploader in the web
# UI is a private endpoint a workflow cannot reach -- so the film has to live at a URL. The
# cheapest durable host is this repository itself: a branch carrying nothing but these GIFs,
# with no connection to `main`'s history, so a clone that does not ask for it never pays for
# it and `main` stays free of generated output, the rule `.agents/automations/review.md`
# already enforces for `demo-run/`.
#
# Linked, not embedded, and that is not a style choice. This repository is private. An
# `![](...)` pointing at raw.githubusercontent.com renders broken here: that host answers 404
# without a token, and GitHub does not proxy same-repo raw URLs through camo -- it leaves them
# bare for the browser to fetch, which cannot authenticate to a different domain. Verified
# both ways: the URL is 200 and 3.5 MB with a token, 404 without one. So the comment links
# github.com/<repo>/blob/... instead, which is on the session's own domain and animates the
# GIF in GitHub's viewer for anyone who can see the repository.
#
# If this repository ever goes public, an embed becomes possible and is the nicer thing;
# until then do not "fix" the link into an `![](...)`.
#
# Driven by .github/workflows/ci.yml; every input arrives in the environment:
#
#   GIF        the file to publish
#   SUMMARY    demo-run/summary.json, for the verdict printed beside it
#   PR         pull request number
#   SHA        the head commit the GIF was recorded from
#   BASE       the branch it was merged with to record it
#   REPO       owner/name
#   BRANCH     the asset branch
#   SERVER_URL github.server_url; defaults to github.com, set for a GHES host
#   RUN_URL    link back to the run, for the comment footer
#   GH_TOKEN   read by `gh`; needs contents:write and pull-requests:write
#
set -euo pipefail

: "${GIF:?}" "${SUMMARY:?}" "${PR:?}" "${SHA:?}" "${BASE:?}" "${REPO:?}" "${BRANCH:?}" \
  "${GH_TOKEN:?}"

# The comment is found by this marker rather than by author, so a re-run updates its own
# comment and not whatever else a bot happened to post last. It is an HTML comment, so it is
# invisible in the rendered body.
MARKER='<!-- demo-gif -->'

# This runs under `if: always()`, so it is also what happens when the recorder died before
# producing anything -- a missing Edge, a scenario that hung. Nothing to publish is not a
# second failure to report: the step that actually broke has already failed the job and said
# why, and a red check here on top of it would only bury that.
for required in "$GIF" "$SUMMARY"; do
  if [ ! -f "$required" ]; then
    echo "publish-demo-gif: no ${required} -- the run produced nothing to publish" >&2
    exit 0
  fi
done

# Keyed by commit, not a fixed name like `latest.gif`, so the link names the commit it is a
# film of and a reader can tell a stale comment from a current one at a glance.
dir="pr-${PR}"
rel="${dir}/${SHA}.gif"
url="${SERVER_URL:-https://github.com}/${REPO}/blob/${BRANCH}/${rel}"

# One scratch root for the run, so a retry that dies partway still leaves nothing behind.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Every step is `|| return`, which looks redundant under `set -e` and is not: bash suspends
# errexit for the whole body of a function called as an `if` condition, which is exactly how
# the retry loop below calls this one. Without them a rejected push would fall through to the
# function's last command, return its status instead, and the retry would never fire.
push_gif() {
  local tmp
  tmp="$(mktemp -d -p "$WORK")" || return 1
  # A fresh empty repository rather than a clone of this one: `git init` plus `checkout -b`
  # *is* an orphan branch, which is what the first run has to create, and a shallow fetch of
  # an existing asset branch costs one commit instead of the project's whole history.
  git init -q "$tmp" || return 1
  git -C "$tmp" remote add origin \
    "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git" || return 1
  if git -C "$tmp" fetch -q --depth 1 origin "$BRANCH" 2>/dev/null; then
    git -C "$tmp" checkout -q -B "$BRANCH" FETCH_HEAD || return 1
  else
    echo "asset branch ${BRANCH} does not exist yet; this run creates it"
    git -C "$tmp" checkout -q -b "$BRANCH" || return 1
  fi

  # One GIF per pull request, not one per push. The comment below is updated in place, so
  # only the newest link is ever live, and keeping the rest would add megabytes to this
  # branch for every commit anyone pushes. Cleared before the new one lands so that a re-run
  # of the same commit also ends with exactly one.
  rm -rf "${tmp:?}/${dir}" || return 1
  mkdir -p "${tmp}/${dir}" || return 1
  cp "$GIF" "${tmp}/${rel}" || return 1
  git -C "$tmp" add -A -- "$dir" || return 1

  if git -C "$tmp" diff --cached --quiet; then
    echo "already published byte-for-byte at ${rel}; leaving the branch alone"
    return 0
  fi
  git -C "$tmp" \
    -c user.name='github-actions[bot]' \
    -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
    commit -q -m "Demo GIF for #${PR} at ${SHA:0:12}" || return 1
  git -C "$tmp" push -q origin "HEAD:refs/heads/${BRANCH}" || return 1
}

# Two pull requests whose runs overlap push to the same branch, and the loser of that race
# gets a non-fast-forward rejection. Re-fetching and replaying is correct here because each
# run only ever touches its own `pr-<n>/` directory, so there is nothing to merge -- the
# retry simply rebuilds the commit on top of whatever arrived first. Not a force-push: that
# would drop the other pull request's GIF and break its comment.
for attempt in 1 2 3 4 5; do
  if push_gif; then
    break
  fi
  if [ "$attempt" = 5 ]; then
    echo "publish-demo-gif: could not push ${BRANCH} after 5 attempts" >&2
    exit 1
  fi
  echo "push to ${BRANCH} was rejected (attempt ${attempt}); refetching and retrying"
  sleep $((attempt * 3))
done

# ----------------------------------------------------------------- the comment

passed=$(jq -r '.passed' "$SUMMARY")
scenarios=$(jq -r '.scenarios' "$SUMMARY")
assertions=$(jq -r '.assertions' "$SUMMARY")
megabytes=$(jq -r '(.bytes / 1000000 * 10 | round / 10)' "$SUMMARY")

if [ "$passed" = "$scenarios" ]; then
  verdict="all ${scenarios} scenarios green"
else
  verdict="**$((scenarios - passed)) of ${scenarios} scenarios red**"
fi

body="$(mktemp -p "$WORK")"
{
  printf '%s\n' "$MARKER"
  printf '### Demo\n\n'
  # `%s merged into %s`, because the workflow records the pull request's merge ref -- the
  # same thing the test jobs run -- and not the branch in isolation. Saying so keeps the
  # commit named here from being read as "this is a film of that commit alone".
  printf '%s, %s assertions, recorded from `%s` merged into `%s`.\n\n' \
    "$verdict" "$assertions" "${SHA:0:12}" "$BASE"
  # The picture is evidence of a run, never the verdict -- AGENTS.md draws that line for the
  # screenshot artifact and it holds here too. The assertions in the transcript are the proof,
  # so the red ones are named in the comment rather than left for someone to spot in the film.
  if [ "$passed" != "$scenarios" ]; then
    printf 'Red scenarios:\n\n'
    jq -r '.red[] | "- `\(.id)` \(.caption) — \(.verdict)"' "$SUMMARY"
    printf '\n'
  fi
  printf '**[▶ Watch demo.gif (%s MB)](%s)** — every scenario in `spec/cli.md`, run for real.\n\n' \
    "$megabytes" "$url"
  # Says why it is a link and not an embed, so the next reader does not "simplify" it into one
  # and ship a broken image. Same reason the rationale is in the header above.
  printf '<sub>Linked rather than embedded because this repository is private: GitHub leaves '
  printf 'same-repo raw URLs unproxied, and that host needs a token, so an inline image '
  printf 'renders broken. The link opens in GitHub'"'"'s viewer, which animates it. · '
  printf '`transcript.md` and the GIF are also on the run as artifacts'
  if [ -n "${RUN_URL:-}" ]; then
    printf ' · [run](%s)' "$RUN_URL"
  fi
  printf '</sub>\n'
} >"$body"

# --paginate applies --jq per page, so this can emit one id per page; the first is the one
# wanted and `head -n1` is also what makes an empty result an empty string.
existing="$(gh api --paginate "repos/${REPO}/issues/${PR}/comments" \
  --jq "map(select(.body // \"\" | startswith(\"${MARKER}\"))) | .[0].id // empty" \
  | head -n1)"

if [ -n "$existing" ]; then
  # `issues/comments/<id>`, with no issue number in it. Editing an issue comment is keyed by
  # comment id alone; `issues/<n>/comments/<id>` is not an endpoint and answers 404, which is
  # how this failed on the second run -- the first one only ever took the POST branch below.
  gh api -X PATCH "repos/${REPO}/issues/comments/${existing}" \
    -F "body=@${body}" --silent
  echo "updated comment ${existing}"
else
  gh api -X POST "repos/${REPO}/issues/${PR}/comments" \
    -F "body=@${body}" --silent
  echo "posted a new comment"
fi

echo "published ${url}"
