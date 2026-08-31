# spec: Worktree GC, delete half — opt-in deletion of dry-run inventory (20260831T050020Z) — v1

Per-run spec at `docs/pipeline/runs/20260831T050020Z/spec.md` (G-15: per-run path avoids PR #28/#29 shared-path conflict). Implements `docs/process/issues/012-worktree-gc-delete-half.md` as the delete half of `herdr-routines gc --dry-run` (issue 002, PR #28). Covers the second half intentionally deferred in `docs/plan-v1.md:448`.

## Problem

`gc --dry-run` (`src/herdr_routines/gc.py:168` `run_gc`, `src/herdr_routines/cli.py:543` `_cmd_gc`) inventories stale `auto/<name>-<ts>` branches without deleting anything: a branch is `stale` (`gc.py:28` `Row.stale`) when merged into base OR its worktree dir is gone (orphaned). Runs leave workspaces + branches in place by design ("Nothing is ever removed automatically"). Over time this accumulates stale branches/worktrees with no safe one-command cleanup. Issue 012 requires opt-in deletion of exactly the dry-run inventory — nothing scheduled/automatic, human-invoked `gc --delete` behind explicit `--yes` (interactive-only) and `--force` for unmerged orphaned branches.

## Approach

Reuse the dry-run's own inventory as the deletion source in the same process — no second scan race. `gc --delete` (`gc.py:226` `run_gc_delete`) calls `collect_rows` (`gc.py:115`) once, which resolves `branch_worktrees` (`gc.py:91`) and `list_auto_branches` (`gc.py:56`) a single time and reuses that `dict[str, Path]` mapping for removals.

### Inventory and eligibility

- Scope: `for-each-ref refs/heads/auto/*` (`gc.py:16` `BRANCH_PATTERN`) minus `auto/pipeline-*` (`gc.py:17` `PIPELINE_PREFIX`, G-14) sorted by name. Plumbing failures return `None` (`gc.py:65`) with `warning:` on stderr and abort delete with nonzero exit — never masquerading as empty.
- Base: `refs/remotes/origin/HEAD` symbolic ref else `main` (`gc.py:72` `detect_base`), overridable via `--base`.
- Per branch: `Row(branch, worktree_exists, merged_into_base)` where `worktree_exists` is `path in worktrees && path.exists()`, `merged_into_base` is `git merge-base --is-ancestor branch base` (`gc.py:81` — rc 0 true, rc 1 false, other warns).
- `stale` = `merged_into_base or not worktree_exists`. `candidates = [r for r in rows if r.stale]`.

### Deletion ordering and guards

1. **Single scan** — `collect_rows` returns `(rows, listing_failed, worktrees)`; `worktrees` reused for `_remove_worktree` (`gc.py:191`) — no second `git worktree list` (`gc.py:245` comment).
2. **Split by force** — if not `--force`: `to_delete = [r for r in candidates if r.merged_into_base]`, `skipped = [r for r in candidates if not r.merged_into_base]` (orphaned but unmerged). With `--force`: all candidates become `to_delete`.
3. **Require --yes** — `gc --delete` without `--yes`/`-y` prints `error: refusing to delete without --yes` to stderr and exits 2 (`gc.py:273`, `cli.py:159`); never prompts.
4. **Per-branch order** — for each `row in to_delete`: `git worktree remove <path> --force` if worktree exists (`gc.py:195`), warn and count as `failed` on nonzero; then `git branch -D <branch>` (`gc.py:202` always `-D` — HEAD-based `-d` safety is redundant after explicit `is_merged` and fails from diverged worktrees like `auto/pipeline-*`), warn on failure. Success prints `deleted: <branch>`; skipped unmerged prints `skipped (unmerged, needs --force): <branch>`.
5. **Timeouts and repo checks** — `run_git` 30s timeout (`gc.py:18` `GIT_TIMEOUT_SECONDS`); `TimeoutExpired` becomes `error: git timed out` with exit 1. `resolve_repo_root` (`gc.py:47`) fail-closed with `error: not a git repository`. `branch_worktrees` plumbing failure degrades to empty mapping with warning so every row becomes `worktree_exists=no` visibly, not silently.
6. **No Herdr dependency** — pure `subprocess` git + filesystem, no `HerdrClient`/socket (`gc.py:1` docstring, `cli.py:544` comment) — usable with no server running.

### CLI (`src/herdr_routines/cli.py:142`)

```
herdr-routines gc --dry-run [--repo PATH] [--base BASE]
herdr-routines gc --delete|--prune --yes/-y [--force/-f] [--repo PATH] [--base BASE]
```

`--dry-run` and `--delete`/`--prune` are mutually exclusive required (`cli.py:145`). `--yes` required for delete; `--force` opts into unmerged orphaned deletion. Exit: 0 all deleted or 0 needed, 1 any `failed` or branch-listing abort or not-a-repo/timeout, 2 missing `--yes`.

## Files touched

- `docs/pipeline/runs/20260831T050020Z/spec.md` — this file (per-run spec, G-15).
- `src/herdr_routines/gc.py` — `Row`, `run_git`, `resolve_repo_root`, `list_auto_branches`, `detect_base`, `is_merged`, `branch_worktrees`, `collect_rows` (single-scan tuple), `format_table`/`format_delete_table`, `run_gc` (dry-run), `_remove_worktree`, `_delete_branch`, `run_gc_delete` (delete with --yes/--force, single worktree dict reuse, -D branch delete, listing_failed abort).
- `src/herdr_routines/cli.py` — `gc` subparser (`cli.py:142`): `--dry-run` vs `--delete`/`--prune` mutually exclusive, `--yes`/`--force` flags, `--repo`/`--base` options, `_cmd_gc` (`cli.py:543`) dispatching to `run_gc`/`run_gc_delete` without `HerdrClient` or config load.
- `tests/test_gc.py` — dry-run table, delete with --yes, --force split (skipped vs deleted), missing --yes exit 2, -D vs -d, single worktree scan reuse, plumbing-failure abort, pipeline prefix exclusion, timeout handling.
- `docs/process/issues/012-worktree-gc-delete-half.md` — issue definition (acceptance: exact dry-run set, --yes required, --force for unmerged).

## Risks

- **Deleting the wrong branch.** Dry-run vs delete inventory drift if two git scans race. Mitigation: single `collect_rows` scan reused for removals; `--base` is resolved once and shared; fixed `BRANCH_PATTERN` + `PIPELINE_PREFIX` exclusion.
- **Plumbing failure masquerading as clean empty.** `for-each-ref` failure returning `[]` reads as "nothing to delete". Mitigation: `list_auto_branches` returns `None` on nonzero, `collect_rows` propagates `listing_failed`, `run_gc_delete` aborts with `error: branch listing failed; aborting delete` and exit 1, no deletions.
- **Unmerged loss.** Orphaned but unmerged branch deleted silently. Mitigation: default split — unmerged candidates become `skipped (unmerged, needs --force)` and are not deleted; only `--force` promotes them to `to_delete`.
- **Unattended deletion.** Scheduled/automatic `gc --delete` wiping branches overnight (explicitly unwanted). Mitigation: `--yes` required, exit 2 without it; no tick/systemd wiring, no auto-invoke; human-invoked only.
- **Worktree/branch ordering and partially deleted state.** Worktree remove succeeds but branch delete fails (or vice versa). Mitigation: per-branch worktree-then-branch order, failure counted as `failed: <branch> (worktree remove)` or `failed: <branch>`, summary `deleted / skipped / failed`, exit 1 if any failed.
- **Running from diverged worktree.** `git branch -d` safety check against HEAD fails when caller is on `auto/pipeline-*`. Mitigation: always `branch -D` after explicit `is_merged` check against `--base`.
- **Worktree list shape variance across git versions.** Strict porcelain line pairing breaks. Mitigation: tolerant scan — each `branch refs/heads/` paired with most recent preceding `worktree ` line, no layout assumption.
- **Git hang wedging the command.** Slow plumbing never returns. Mitigation: `GIT_TIMEOUT_SECONDS=30` on every `run_git`, `TimeoutExpired` mapped to `error: git timed out` with exit 1, never a traceback.
