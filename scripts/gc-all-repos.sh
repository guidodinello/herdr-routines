#!/bin/bash
# Weekly cleanup of the job clones under the state dir, run by
# deploy/systemd/herdr-routines-gc.service.
#
# For every clone in $REPOS_DIR:
#   1. `herdr-routines gc --delete --yes`: removes merged `auto/*` branches older than
#      gc's default age gate (14 days) together with their worktrees under
#      ~/.herdr/worktrees/<repo>. Unmerged and dirty worktrees are skipped by gc itself.
#   2. `git gc`: packs the loose objects that agent commits in worktrees leave behind.
#
# Why: nothing removed old run worktrees, and by 2026-11-27 the Pi held 5+ GB of them
# (164 in the fitted clone alone) plus 37 MB of loose objects in one 2 MB repo.
#
# One failing repo does not stop the others; the script exits 1 if any step failed, so
# the unit shows as failed.
set -u

REPOS_DIR="${REPOS_DIR:-$HOME/.local/state/herdr-routines/repos}"
WORKTREES_DIR="${WORKTREES_DIR:-$HOME/.herdr/worktrees}"
UV="${UV:-$HOME/.local/bin/uv}"

status=0
for repo in "$REPOS_DIR"/*/; do
    repo="${repo%/}"
    [ -d "$repo/.git" ] || continue
    name="$(basename "$repo")"
    echo "== $name"
    if ! "$UV" run --locked herdr-routines gc --delete --yes \
        --repo "$repo" --worktrees-root "$WORKTREES_DIR/$name"; then
        echo "gc failed for $name" >&2
        status=1
    fi
    if ! git -C "$repo" gc --quiet; then
        echo "git gc failed for $name" >&2
        status=1
    fi
done
exit "$status"
