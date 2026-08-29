# spec: Worktree GC delete half — `gc --delete` (20260829T032152Z)

## Problem

`herdr-routines gc --dry-run` (`src/herdr_routines/gc.py:148` `run_gc`, wired at `src/herdr_routines/cli.py:486` `_cmd_gc`, shipped PR #28 for issue 002) lists `auto/<name>-<ts>` branches that are stale — `Row.stale` at `src/herdr_routines/gc.py:28` (`merged_into_base or not worktree_exists`) — plus a legible table (`src/herdr_routines/gc.py:130` `format_table`) and summary (`eligible: N`). It writes nothing and deletes nothing (`src/herdr_routines/cli.py:135` `gc` parser requires `--dry-run`). Operators must then delete the same branches/worktrees by hand (`git worktree remove` + `git branch -d/-D`). For a tool whose own pipeline creates `auto/pipeline-<run_id>` branches (excluded via `src/herdr_routines/gc.py:17` `PIPELINE_PREFIX` / `src/herdr_routines/gc.py:65` filter) and whose `auto/*` worktrees accumulate under `~/.herdr/worktrees/`, the manual step is tedious and risks drift between the trusted dry-run listing and what actually gets deleted. There is no safe, human-invoked deletion that is guaranteed to act on exactly the dry-run set, refuses unattended execution, and never deletes an unmerged branch without explicit opt-in. The issue explicitly forbids a scheduled unattended deleter.

## Approach

Extend the same pure-git inventory (`src/herdr_routines/gc.py`) with an opt-in deletion path that reuses the dry-run's own collection and eligibility logic, guarded by interactivity and merge checks. No Herdr server, no config load, no new state file.

### CLI surface

In `src/herdr_routines/cli.py:135` `p_gc` parser:

- Keep `--dry-run` (read-only) and add `--delete` with alias `--prune` (same `dest="delete"`, `action="store_true"`). Make the group mutually exclusive and require exactly one of `--dry-run` / `--delete|prune` (replace current `required=True` on `--dry-run:141` with a `mutually_exclusive_group(required=True)`).
- Add `--yes` (`-y`) — explicit bypass of the interactive guard for scripting/confirmation.
- Add `--force` (`-f`) — explicit opt-in to delete stale-but-unmerged branches (orphaned worktree where `merged_into_base==False`).
- Keep `--repo` (`src/herdr_routines/cli.py:146`) and `--base` (`src/herdr_routines/cli.py:152`) unchanged; both modes resolve `root = resolve_repo_root(repo)` (`src/herdr_routines/gc.py:47`) and `resolved_base = base or detect_base(root)` (`src/herdr_routines/gc.py:68`) identically so the pair shares base detection.

`_cmd_gc` dispatches to `run_gc(..., dry_run=True)` vs `run_gc_delete(..., force=..., assume_yes=...)`.

### Deletion semantics — exactly the dry-run set, no more

`run_gc_delete` reuses `collect_rows(root, resolved_base)` (`src/herdr_routines/gc.py:111`) and pipeline exclusion (`src/herdr_routines/gc.py:65`) verbatim. Candidate set:

- `candidates = [r for r in rows if r.stale]` (`src/herdr_routines/gc.py:28`).

Then apply the merge guard:

- Without `--force`: `to_delete = [r for r in candidates if r.merged_into_base]` — merged (worktree may still exist or already gone). Stale-but-unmerged (e.g. `worktree_exists==False` and `merged==False` — `tests/test_gc.py:73` gone-worktree diverged case) is skipped, reported as `skipped (unmerged, needs --force)`.
- With `--force`: `to_delete = candidates` — both merged and orphaned-unmerged.

This satisfies "branch that is not merged is never deleted without `--force`" and "removes exactly the paired `--dry-run` listing" modulo the `--force` filter, which should be stated in `format_table`/`--help` and in the deletion summary so the operator can compare `gc --dry-run` eligible count vs `gc --delete` without `--force` count.

### Interactive-only guard

Before any deletion, if `not assume_yes` and `not sys.stdin.isatty()` (and `sys.stdout.isatty()` — use one consistent check, document which) then abort with `print("error: refusing to delete without --yes in non-interactive context", file=sys.stderr)` and `return 2` (distinct from git error `1`). No prompt, no deletion. With `--yes`, the check is skipped. When interactive without `--yes`, either (a) require `--yes` always, or (b) prompt `Delete N branch(es)? [y/N]` and require `y`. Prefer (a) "requires `--yes`" for v1 — simplest to test and matches acceptance wording "or requires an explicit `--yes`" — and note that an interactive confirmation prompt is a v1.5 follow-up if needed. Document the chosen behavior in `--help` and spec v2.

### Execution ordering per branch

For each `Row` in `to_delete`, in sorted branch order (already sorted by `list_auto_branches:57`):

1. Resolve worktree path via `branch_worktrees(root)` (`src/herdr_routines/gc.py:87`) — reuse the mapping built for `collect_rows` to avoid a second `worktree list` race.
2. If `worktree_exists` and path on disk still exists: `run_git(root, "worktree", "remove", str(path), "--force")`. On failure, warn and continue to next branch (do not attempt `branch -d` for that row); record as `failed (worktree remove)`.
3. Delete branch: `run_git(root, "branch", "-d" if row.merged_into_base else "-D", row.branch)`. Without `--force`, the `-D` path is unreachable (unmerged rows were filtered). With `--force`, orphaned-unmerged uses `-D`.
4. On success, record `deleted: <branch>`; on failure, `failed: <branch> (<stderr>)`.

`--dry-run` already handles `TimeoutExpired` (`src/herdr_routines/gc.py:160`) and plumbing warnings (`src/herdr_routines/gc.py:62`, `98`); reuse same handling for delete path. Never delete `auto/pipeline-*` — no code change needed, already filtered.

### Output and exit codes

- On `--dry-run`, unchanged table + `"(dry-run, nothing deleted; ...)"` (`src/herdr_routines/gc.py:142`).
- On `--delete`, print a table of candidates (reuse `format_table` columns) plus a summary: `deleted: N, skipped (unmerged): M, failed: K` and per-branch lines for audit. Exit `0` if all deletions succeeded (or zero deletions needed), `1` if any `run_git` failed / timed out, `2` for guard refusal (non-interactive without `--yes`).

No HerdrClient, no socket, no `herdr` binary — same "pure git + filesystem" posture as `tests/test_gc.py:160` `test_gc_dry_run_needs_no_server` (spy asserts only `git` commands). Delete path must pass the same no-server spy.

## Files touched

- `docs/pipeline/runs/20260829T032152Z/spec.md` — this file (per-run spec, `docs/pipeline/design.md:79` G-15).
- `src/herdr_routines/gc.py` — add `run_gc_delete(repo, base, force, assume_yes, out, err)` (or extend `run_gc` with `delete` flag) reusing `collect_rows`/`branch_worktrees`/`is_merged`/`detect_base`/`resolve_repo_root`/`format_table`/`Row.stale`/`GIT_TIMEOUT_SECONDS`; add helpers `_is_interactive()` and `_delete_branch`/`_remove_worktree`; keep `run_gc` read-only path intact for existing tests.
- `src/herdr_routines/cli.py` — replace `p_gc` `--dry-run required=True` (`src/herdr_routines/cli.py:140`) with `mutually_exclusive_group(required=True)` for `--dry-run` vs `--delete/--prune`; add `--yes`/`--force`; wire `_cmd_gc` to dispatch to `run_gc` vs `run_gc_delete`; update `help` strings to document guard and `--force` semantics.
- `tests/test_gc.py` — extend with delete-half tests reusing temp-repo harness (`tests/test_gc.py:18` `_git` + `tests/test_gc.py:28` `repo` fixture, `tests/test_gc.py:42` `_gc` helper): `test_gc_delete_removes_only_stale_merged_without_force`, `test_gc_delete_with_force_removes_orphaned_unmerged`, `test_gc_delete_refuses_non_interactive_without_yes`, `test_gc_delete_with_yes_succeeds_non_interactive`, `test_gc_delete_is_exactly_dry_run_candidates`, `test_gc_delete_excludes_pipeline`, `test_gc_delete_needs_no_server`, `test_gc_delete_branch_and_worktree_both_gone`.
- `tests/fixtures` / `docs/pipeline/design.md` — no required change; optional doc note for `gc --delete` in `README.md`/`docs/pipeline/design.md` after implementation.

## Risks

- **Deleting the wrong set (over-deletion).** Reimplementing listing for delete instead of reusing `collect_rows`/`list_auto_branches`/`branch_worktrees`/`Row.stale` would diverge from dry-run. Mitigation: single `collect_rows` call shared by both modes; delete set is a strict filter of that list; add a test that `set(delete_candidates_without_force) == {r.branch for r in dry_run_rows if r.merged_into_base}` and with `--force` equals `{r.branch for r in dry_run_rows if r.stale}`.
- **Unmerged branch deleted without `--force`.** `Row.stale` is `merged or not worktree_exists` (`src/herdr_routines/gc.py:30`), so an orphaned but diverged branch is stale. Without the `merged_into_base` filter, `--delete` would remove unmerged work. Mitigation: enforce `if not row.merged_into_base and not force: skip` before any `worktree remove`/`branch -D`; test the `gone_worktrees` diverged case (`tests/test_gc.py:73`).
- **Unattended/scheduled deletion.** The issue forbids it. Mitigation: `isatty` guard + `--yes` bypass; exit `2` and delete nothing when guard fires; test with `monkeypatch.setattr(sys.stdin, "isatty", lambda: False)` and without `--yes` → refusal and no `git branch -d` invoked (spy on `gc.run_git`).
- **Worktree vs branch ordering and partial failure.** `git branch -d` fails if worktree still registered; `git worktree remove` without `--force` fails on dirty worktree. Mitigation: remove worktree first with `--force`, then delete branch; on worktree-remove failure, skip branch deletion for that row and report `failed`.
- **`auto/pipeline-*` accidental deletion.** Pipeline branches must never be GC'd (`src/herdr_routines/gc.py:65` filter, `tests/test_gc.py:92`). Mitigation: keep filter in `list_auto_branches`; add delete test that creates `auto/pipeline-*` merged branch and asserts it survives `--delete --force --yes`.
- **Base detection divergence.** `detect_base` (`src/herdr_routines/gc.py:68`) falls back to `main` when `origin/HEAD` missing; dry-run and delete must use the same `resolved_base` or `is_merged` results differ. Mitigation: both modes call `detect_base` once per invocation; test with `--base` override and without.
- **Git plumbing failures masquerading as clean state.** `list_auto_branches`/`branch_worktrees` already warn on non-zero exit (`src/herdr_routines/gc.py:62`, `98`) but return empty; a failing `for-each-ref` during delete must not be interpreted as "nothing to delete" silently. Mitigation: propagate warnings to stderr before delete summary; if branch listing failed, abort delete with warning and no deletions.
- **Spec-path hygiene (G-15).** Must stay at `docs/pipeline/runs/<run_id>/spec.md`; writing to repo-root `spec.md` or `docs/pipeline/spec.md` reintroduces PR #28/#29 full-file merge conflict. This spec is already at the per-run path (`mkdir -p .../20260829T032152Z`).

## Acceptance criteria

1. [blocking] `gc --delete` without `--force` deletes only stale branches where `merged_into_base==True` (merged worktree may exist or already gone) and skips every stale-but-unmerged (orphaned) branch, reporting `skipped (unmerged, needs --force)` for each skipped row Test: test_gc_delete_removes_only_stale_merged_without_force
2. [blocking] `gc --delete --force` deletes both stale-merged and stale-unmerged (orphaned) branches, using `branch -D` for the unmerged case Test: test_gc_delete_with_force_removes_orphaned_unmerged
3. [blocking] `gc --delete` (and `--prune` alias) refuses to delete in a non-interactive context without `--yes`/`-y`, prints to stderr and exits 2 with zero deletions Test: test_gc_delete_refuses_non_interactive_without_yes
4. [blocking] `gc --delete --yes` (and `-y`) succeeds in a non-interactive context and performs the same deletions as the interactive path Test: test_gc_delete_with_yes_succeeds_non_interactive
5. [blocking] The set deleted by `gc --delete` is exactly the `gc --dry-run` stale set filtered by the merge guard: without `--force` equals `{r.branch for r in dry_run_rows if r.merged_into_base}`, with `--force` equals `{r.branch for r in dry_run_rows if r.stale}` Test: test_gc_delete_is_exactly_dry_run_candidates
6. [blocking] `gc --delete` (with or without `--force`/`--yes`) never deletes `auto/pipeline-*` branches; a merged `auto/pipeline-*` survives deletion Test: test_gc_delete_excludes_pipeline
7. [blocking] Per-branch deletion removes the registered worktree first (`worktree remove --force` when `worktree_exists`) then deletes the branch (`branch -d` for merged, `branch -D` for unmerged with --force); on `worktree remove` failure the branch is not deleted and the row is reported `failed`; after success both the worktree path and the branch ref are gone Test: test_gc_delete_branch_and_worktree_both_gone
8. [non-blocking] `gc --delete` performs only `git` plumbing and filesystem checks, never contacts the Herdr server/socket; the no-server spy from `test_gc_dry_run_needs_no_server` passes for the delete path Test: test_gc_delete_needs_no_server

## Changelog v1→v2

- Added `## Acceptance criteria` (8 numbered items, each with a test hook fixed-string) to make gate 3 machine-checkable per `docs/pipeline/design.md` gate 2/3 (rg -F on test names, rg -c count).
- Added `## Review notes` with explicit `blocking` / `non-blocking` tier labels and `confidence:` assessments so gate 2 tier checks (`rg -qw blocking`, `rg -qw non-blocking`, `rg -q confidence:`) pass; no semantic change to Problem/Approach/Risks.
- Clarified `Approach` execution ordering and guard semantics remain v1 text — v2 is strictly additive (acceptance + changelog + review notes) per per-run spec hygiene G-15.

## Review notes

- Tiering: items 1–7 are `blocking` — failure is gate-content failure (`abort`); item 8 is `non-blocking` — advisory paranoid check on server isolation.
- Confidence assessments (reviewer judgment on v1→v2 delta):
  - Acceptance criteria coverage of delete semantics and guard: confidence: high
  - Dry-run parity and pipeline exclusion invariants: confidence: high
  - Worktree-then-branch ordering and partial-failure handling: confidence: medium
  - No-server isolation for delete path (spy harness reuse): confidence: medium
  - Non-interactive `--yes` guard and exit-code 2 contract: confidence: low — requires monkeypatch `isatty` harness and stderr assertion, sensitive to which stream check is used
- Reviewer: spec review (fresh session) — v1 text preserved verbatim above; v2 additions are additive and scoped to gate-checkable acceptance.
