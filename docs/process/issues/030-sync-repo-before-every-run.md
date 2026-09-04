---
id: "030"
title: "Fetch+fast-forward every job's repo before every run, routine or pipeline"
status: open
priority: high
area: config
---

## Description

`ensure_repo()` (`src/herdr_routines/repos.py`, added by issue 016) is
documented as running "at the top of `runner.execute_run` and gated-dispatch
paths in `tick.py` so every worktree/tab creation starts from a known-good
checkout" — but it only fetches+fast-forwards for `repository:`-managed jobs
(`job.repository is not None`). For a plain `repo:` job (a pre-existing local
clone path — what every current job, including the overnight pipeline's own
`herdr-routines` checkout, actually uses) it returns `job.repo` unchanged:
**no fetch ever happens**, for any job, routine or pipeline.

Worse, the overnight pipeline doesn't go through `tick.py`/`runner.py` at
all. It schedules via a separate `systemd-run --on-calendar` launcher
(`~/.local/bin/pipeline-launch-*.sh`, outside this repo) that runs
`docs/pipeline/orchestrator-prompt.md`'s prerequisite step directly:

```sh
herdr worktree create --cwd "$REPO_PARENT" --base main --branch "auto/pipeline-$RUN_ID"
```

against whatever state `$REPO_PARENT` happens to be in locally. Nothing
fetches `$REPO_PARENT` first.

**Hit on 2026-09-03**: pipeline run `20260903T050016Z` (PR #69) branched from
commit `0858d9c` (#62, merged 2026-08-31) — 6 merged PRs and ~2 days behind
`origin/main` at branch-creation time, including PR #65
(`fallback_model` retried once on `quota_exhausted`, merged 2026-08-31),
which is the *same feature* this run's issue 022 asked for. Working from the
stale base, the run reimplemented overlapping logic in `config.py`,
`runner.py`, `tick.py`, producing a PR that conflicts with `main` and
duplicates already-shipped work. Root cause traced live in-session with
Claude Code; PR #69 closed manually, no code fix yet.

## Design (proposal)

One structural guarantee, not a per-caller instruction:

- Generalize `ensure_repo()` to fetch+fast-forward **every** job's checkout
  to `origin/<base>` before use, regardless of `repo:` vs `repository:`
  shape — drop the `job.repository is None: return job.repo` early return's
  fetch skip; only the clone-vs-fetch branch should depend on `repository:`.
- Give the pipeline launcher the same guarantee from the same source of
  truth, so there are not two partially-covered code paths. Concretely: add
  a small CLI entry point (e.g. `herdr-routines sync-repo --path <dir>
  --base main`) that wraps `_fetch_and_fast_forward`, and have the pipeline
  launcher script call it against `$REPO_PARENT` immediately before `herdr
  worktree create` — mirroring how `tick.py`/`runner.py` already call
  `ensure_repo` before worktree/tab creation for routine jobs.
- Fail behavior: a fetch/fast-forward failure here should abort the run the
  same way `ensure_repo` already maps a `RuntimeError` to a terminal
  `RunOutcome` for managed-repo jobs — never silently proceed on a stale or
  conflicted checkout.

Files: `src/herdr_routines/repos.py` (generalize `ensure_repo`, drop the
`repository is None` fetch-skip), `src/herdr_routines/cli.py` (new
`sync-repo` subcommand), `docs/pipeline/orchestrator-prompt.md` (prerequisite
step calls `herdr-routines sync-repo` instead of assuming `$REPO_PARENT` is
current), `tests/test_repos.py`.

## Acceptance

- A `repo:` job (non-managed, plain local path) is fetched+fast-forwarded to
  `origin/<base>` before every worktree/tab creation, same as `repository:`
  jobs already are.
- `ensure_repo` (or its generalized replacement) fails loudly (raises,
  mapped to a terminal `RunOutcome`) on a non-fast-forwardable local branch
  rather than silently running against a diverged checkout.
- The pipeline launcher's prerequisite step calls the same sync primitive
  used by `tick.py`/`runner.py` — one source of truth, not a prompt-level
  instruction duplicated in `orchestrator-prompt.md`.
- Given the PR #69 scenario (local checkout N commits behind `origin/main`),
  a fresh pipeline run branches from current `origin/main`, not the stale
  local ref.

## Log

- **2026-09-03**: filed after tracing PR #69's merge conflicts to a stale
  `$REPO_PARENT` (branched 2 days / 6 PRs behind `origin/main`, including the
  very feature — PR #65 — the run's own issue 022 asked for). Discussed with
  the human: fix belongs in code as a structural guarantee shared by
  routines and the pipeline, not a `docs/pipeline/orchestrator-prompt.md`
  instruction and not a bespoke reuse of `repos.py`'s `Job`-shaped API as-is.
- **2026-09-03**: review caught acceptance criterion 1 still unmet after the
  first pass — `ensure_repo()` itself was correctly generalized, but all 4
  call sites (`tick.py` x3, `runner.py` x1) still gated the call behind
  `if job.repository is not None:`, so the fixed function never ran for
  plain `repo:` jobs in the actual dispatch path. Removed the gate at each
  call site (unconditional `ensure_repo(job)`, same try/except mapping to a
  terminal outcome/history record already there). Fixed test fixtures in
  `tests/test_tick.py`, `tests/test_auto_fix.py`, `tests/test_runner.py`
  that pointed `job.repo` at a non-git tmp_path and don't exercise repo-sync
  behavior — added autouse fixtures stubbing `ensure_repo` in those files
  (real-git-fixture coverage of `ensure_repo` itself stays in
  `tests/test_repos.py`). Added `test_plain_repo_job_is_synced_in_dispatch` and
  `test_plain_repo_job_sync_failure_is_mapped_to_failed_history` in
  `tests/test_tick.py`, which build a `repository: None` job and assert
  `ensure_repo` is actually reached (and its failure mapped to a `failed`
  history record) via `run_tick` — verified these fail against the pre-fix
  gated call sites before confirming green.
