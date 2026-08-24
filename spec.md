# spec: herdr-routines gc --dry-run

## Problem

Routine runs create `auto/<name>-<ts>` branches (and linked worktrees under `~/.herdr/worktrees/`) via `herdr worktree create`. Finished work accumulates: the branches remain after the report is read and the pane is closed, and no tool enumerates which are safe to delete. Operators currently hunt manually with `git branch --list 'auto/*'` and `git worktree list`, then guess whether a branch is merged or orphaned.

We need a read-only inventory command that turns cleanup into a reviewed decision: list every `auto/<name>-<ts>` branch in the target repo, flag each as stale when (a) the branch is fully merged into its base branch, or (b) its linked worktree directory no longer exists on disk, and report counts — without deleting or writing anything. Pipeline branches `auto/pipeline-*` must never appear (the pipeline keeps its own branch until its PR merges per `docs/pipeline/design.md` G-14). Critically, the command must work with no Herdr server running — it inspects only local git/filesystem state, so it remains usable when Herdr is down or on a host that never runs Herdr.

## Approach

### CLI extension point — `src/herdr_routines/cli.py`

Add a new subcommand `gc` to `_build_parser()` alongside `tick|status|history|validate|run`. Signature:

```
herdr-routines gc --dry-run [--repo PATH] [--base BASE]
```

- `--dry-run` is required in v1 (the only mode); it makes the read-only guarantee explicit and reserves `--no-dry-run`/delete for the later Next item. The handler prints a table + summary to stdout and exits 0. No file is written or deleted regardless of flags.
- `--repo` defaults to `Path.cwd()` / the current git repository (resolved via `git rev-parse --show-toplevel`); if not inside a git repo, error to stderr and exit 1. This matches the target repo described in the feature idea without coupling to `jobs.yaml` or `HERDR_PLUGIN_CONFIG_DIR`.
- `--base` defaults to `main` (fallback: repo's default branch via `git symbolic-ref refs/remotes/origin/HEAD` or `main` if unresolvable). Used as the merge target for the merged-check.
- No `HerdrClient` import or Herdr socket touch; `--version` precedent applies — the command must not require config loading or a running Herdr server.

Args parsing lives in `cli.py`; scanning/formatting delegates to a small pure-ish module `src/herdr_routines/gc.py` (alternatively inline in `cli.py` if the implementer prefers — the contract is the same). Keep `herdr.py` untouched.

### Scanning logic — `src/herdr_routines/gc.py` (new)

1. Enumerate local branches matching `auto/*`:
   ```
   git for-each-ref --format='%(refname:short)' refs/heads/auto/*
   ```
   (or `git branch --list 'auto/*' --format='%(refname:short)'` — either suffices). Filter in Python:
   - Exclude any ref starting with `auto/pipeline-` entirely — it never appears in output, counts, or summary. This satisfies G-14.
   - Remaining branches are `auto/<name>-<ts>` candidates; no strict timestamp validation in v1 (any `auto/*` that is not `auto/pipeline-*` qualifies).

2. For each candidate branch, compute two booleans:

   - **merged-into-base**: `git merge-base --is-ancestor <branch> <base>` — exit 0 means the branch tip is reachable from `base` (fully merged). Exit 1 means not merged. Any other exit (e.g. unknown ref, no base) treated as not-merged with a warning on stderr, never crashing the table.
   - **worktree-exists**: parse `git worktree list --porcelain` for a `branch refs/heads/<branch>` entry and check the preceding `worktree <path>` path exists on disk via `Path(path).exists()`. If the branch has no worktree entry, treat as no worktree (`exists=no`). If a worktree path is listed but `Path.exists()` is false, that is the stale-worktree signal. Do not invoke `herdr worktree list` — pure git + filesystem.

3. Eligibility is informational at display time (both booleans shown); a branch is considered stale/eligible when `merged_into_base or not worktree_exists` (OR, as stated in the idea). The table shows all `auto/*` (minus pipeline) regardless, so the reviewer sees why each row is or isn't eligible.

### Table output

To stdout (not stderr), e.g.:

```
BRANCH                          WORKTREE-EXISTS  MERGED-INTO-BASE
auto/nightly-dep-audit-20260822T030000Z  yes              yes
auto/fix-foo-20260823T011500Z             no               no
...
3 branch(es) listed (dry-run, nothing deleted)
```

- Header row `BRANCH  WORKTREE-EXISTS  MERGED-INTO-BASE` (exact casing up to implementer, but three columns in that order).
- Values are `yes`/`no` per column.
- Rows sorted lexicographically by branch name for determinism.
- Summary line last: `<N> branch(es) listed` plus a parenthetical noting dry-run wrote nothing (e.g. `dry-run, 0 deleted` / `eligible: M merged, W missing worktree` — exact wording flexible, count must be present).
- Empty case: header + `0 branch(es) listed ...` (or `No auto branches found` with count 0).
- Pure text table; no JSON flag in v1. Use fixed-width columns or tab separation — must be human-readable without extra tooling.

### No Herdr server required

All git invocations use `subprocess.run(["git", ...], capture_output=True, text=True)` with a short timeout. No `HerdrClient`, no socket, no `herdr` binary. Verify with Herdr stopped: `systemctl --user stop herdr-server` then `herdr-routines gc --dry-run` still exits 0 and lists branches.

## Files touched

- `src/herdr_routines/cli.py` — add `gc` subparser (`--dry-run`, `--repo`, `--base`) and handler `_cmd_gc`; wire to scanning helper; ensure `herdr-routines gc --help` appears and `--version`/`--help` still work without a repo or server.
- `src/herdr_routines/gc.py` — new, small module: `list_auto_branches(repo: Path) -> list[str]`, `is_merged(branch, base, repo) -> bool`, `worktree_exists(branch, repo) -> bool`, `collect_rows(...) -> list[Row]`, `format_table(rows) -> str`. Keeps subprocess/git details isolated for testability (injectable runner or patch `subprocess.run` in tests). No Herdr dependency.
- Tests (stage 3): `tests/test_gc.py` — unit tests against a faked `subprocess.run` / temp git repo fixtures covering: filtering `auto/pipeline-*`, merged vs not-merged, worktree present/missing, empty repo, table formatting and summary count, and Herdr-not-required (no `herdr` binary on PATH). No new runtime dependencies; uses stdlib `subprocess`, `pathlib`, `argparse`.
- Docs: no config schema change, no `jobs.yaml` change, no systemd unit change.

## Risks

- **Base branch ambiguity.** `--base` defaults to `main` but repos may use `master`/`develop`. Auto-detecting `origin/HEAD` can fail in detached or no-remote clones. Mitigation: explicit `--base` flag, fallback to `main`, and treat a missing base as not-merged rather than erroring the whole command.
- **`git merge-base --is-ancestor` semantics.** A branch that was squash-merged or cherry-picked is not considered merged by this check (no common ancestor). That's correct for the read-only signal — the table will show `merged-into-base=no` and the reviewer decides; a future delete half must use a stricter check if it wants squash awareness.
- **Worktree detection fragility.** `git worktree list --porcelain` output varies slightly across git versions; parsing must be tolerant (scan for `worktree ` + `branch refs/heads/` pairs, don't assume ordering). Filesystem check races with a concurrently removed directory are acceptable — the command is point-in-time and dry-run.
- **Branch pattern scope.** Any `auto/*` branch that is not `auto/pipeline-*` is listed, even if created manually outside `herdr-routines`. That's intentional for completeness; over-listing is safer than hiding a stale branch behind a strict regex that drifts.
- **Empty / non-repo invocation.** Running outside a git repo must fail cleanly (stderr + exit 1/2) without a traceback, but running inside a repo with zero `auto/*` branches must succeed with count 0. Don't conflate the two.
- **Table vs machine parsing.** Text tables are convenient for review but brittle for scripting. Keep column order and `yes/no` tokens stable; if machine use is needed later, add `--json` in a follow-up without changing v1 output.
