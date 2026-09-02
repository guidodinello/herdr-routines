# spec: repository: <git-url> job field (20260902T050021Z) — v2

Per-run spec at `docs/pipeline/runs/20260902T050021Z/spec.md` (G-15: per-run path avoids PR #28/#29 shared-path full-file conflict — `docs/pipeline/design.md:79`). Implements `docs/process/issues/016-repository-git-url-job-field.md`. v2 adds explicit Acceptance criteria with `blocking`/`non-blocking` and `confidence:` tiers and a Changelog.

## Problem

`Job.repo` (`src/herdr_routines/config.py:144` `repo: Path`) is required and must already exist on the host. Bringing the tool to a new host (hp-server migration, `016 Log 2026-08-27`) or pointing a job at a repo not yet cloned requires a manual `git clone` out of band. That breaks portability: `jobs.yaml` cannot describe a job fully — the clone step lives in shell history, not config.

`tick.py` (`src/herdr_routines/tick.py:431` `worktree_create`/`tab_create` via `job.repo`) and `runner.py:432` both assume `job.repo` is a live checkout. `cli.py:403` `validate` hard-fails when the path does not exist or lacks `.git`. For `repository` jobs that failure must move from "config error at load" to "clone/pull on first run, fail cleanly if it cannot."

## Approach

Add an optional `repository` field that makes herdr-routines own the clone lifecycle: clone-if-missing on first run, `fetch` + fast-forward on every subsequent run. Jobs with explicit `repo:` keep working unchanged; `repository` jobs derive `repo` to a deterministic managed path.

### Config (`src/herdr_routines/config.py`)

- New job key `repository: str | None` (git URL — `https://`, `ssh://`, `git@host:` or `git://` accepted; non-empty string validated, URL-shape check rejects bare paths). Add to `_JOB_ALLOWED_KEYS:86` and `_JOB_DEFAULTS:106` (`None`). Keep `_JOB_REQUIRED_KEYS:85` as `{name, cron}` — `repo` becomes conditionally required (see below).
- `Job` (`config.py:141`) gains `repository: str | None` and `repo: Path | None` semantics internally, but external `Job.repo` stays a resolved absolute `Path` for consumers. Resolution:
  - `repository` absent and `repo` absent → `ConfigError: missing repo/repository`.
  - `repository` present, `repo` absent → `repo = default_repos_dir() / job.name` (deterministic per job, not per URL hash — `name` is already unique via `NAME_RE:70`/`seen_names:265`).
  - `repository` present, `repo` present → `repo` is the explicit checkout path and `repository` is its remote (allows overriding the managed location). Mutual presence is allowed, not exclusive.
  - `repository` absent, `repo` present → legacy path unchanged.
- `default_repos_dir() -> Path` follows the same `HERDR_PLUGIN_STATE_DIR` pattern as `history.default_history_path:31` and `runner.default_reports_dir:246`: `$HERDR_PLUGIN_STATE_DIR/repos` else `~/.local/state/herdr-routines/repos`. No new env var.
- Validation: `repository` non-empty, URL-ish; `repo` (when supplied) still a non-empty string path expanded via `Path.expanduser()` (`config.py:439`). `workspace`/`base` checks unchanged. `repo` existence is **not** validated at load — deferred to ensure-or-fail at run time (so `validate` can distinguish the two modes).

### Clone lifecycle (new `src/herdr_routines/repos.py` or `runner.py` helper)

Pure-subprocess helper `ensure_repo(job, *, repos_dir) -> Path` called at the top of `runner.execute_run:382` (before `report_path.mkdir` and before any `worktree_create`/`tab_create`) and similarly at the start of gated dispatch in `tick.py:468` (`_process_base_target`) and `tick.py:908` (`_dispatch_fix_worker`) where `job.repo` is used for `git worktree add`. Batch execution (`run_tick:105` sequential loop) still benefits — ensure runs once per job per tick, idempotent.

Steps, all via `subprocess.run` with timeouts, fail-closed:

1. If `job.repository is None` → return `job.repo` unchanged (no fetch).
2. Resolve `checkout = job.repo` (derived or explicit).
3. If `checkout` does not exist or lacks `.git` → `git clone <repository> <checkout>` (atomic: clone to `<checkout>.tmp.<run_id>` then `rename`; on failure remove tmp, return `RunOutcome` `failed`/`reason=clone_failed` with `error=stderr`, no agent or worktree created). Shallow not needed for v1.
4. Else (`checkout` is a git repo) → `git -C <checkout> fetch --prune origin` then fast-forward the current branch to `origin/<base>` or `origin/HEAD` if detached: `git -C <checkout> rev-parse --abbrev-ref HEAD` to detect, then `git -C <checkout> merge --ff-only origin/<base>` (for detached worktrees used by base-target gate, fetch alone is enough). Non-fast-forward or fetch failure → `failed`/`reason=repo_sync_failed`, no partial checkout left in a half-merged state (merge is the only mutating step; fetch is safe to retry).
5. On success return `checkout`.

Never fall through to `worktree_create`/`agent_start` after a clone/pull failure. Temporary clone dirs cleaned on error so the next tick retries a full clone rather than running an agent against a truncated tree (`016 Acceptance` — "not a partial checkout the agent then runs against").

Auth: relies on host's existing `git` credentials (SSH agent / `gh auth` / credential helper); private-repo auth failure surfaces as `clone_failed`/`repo_sync_failed` — no new secret handling.

### Consumers

- `runner.py:382` `execute_run`, `runner.py:311` `build_dry_run_argv` (resolve derived `repo` so dry-run argv shows the real checkout path), `tick.py:105` `run_tick`/`_process_job:1027` and gated paths (`_process_gated_job:122`, `_process_base_target:468`, `_dispatch_fix_worker:854`): call `ensure_repo` first; treat `HerdrCliError`/`OSError`-like clone errors as terminal `RunOutcome` (`failed`, `reason=clone_failed|repo_sync_failed`, `branch=None`).
- `cli.py:388` `_cmd_validate`: for jobs with `repository`, skip the `repo.exists()`/`.git` existence errors (checkout not yet required). Instead warn `repository job not yet cloned: <checkout>` when the managed path is absent, and error only on malformed `repository` URL. Keep `validate --systemd-unit` unchanged.
- `cli.py:197` `_load_config_or_exit` and `status`/`scheduled`/`ps` unchanged — they load the derived `repo` path but do not trigger a clone (read-only commands never mutate).
- `deploy/jobs.example.yaml` add commented `repository: https://github.com/org/repo.git` example alongside `repo:`.

### Migration

Backward compatible: existing `repo:` jobs never enter the `repository` path. New jobs can be written portably as `repository: <url>` alone. No auto-migration rewrites the filesystem without consent.

## Files touched

- `docs/pipeline/runs/20260902T050021Z/spec.md` — this file (per-run spec, G-15).
- `src/herdr_routines/config.py:85` `_JOB_REQUIRED_KEYS`, `src/herdr_routines/config.py:86` `_JOB_ALLOWED_KEYS`, `src/herdr_routines/config.py:106` `_JOB_DEFAULTS`, `src/herdr_routines/config.py:141` `Job` dataclass (`repository` field, `repo` conditional), `src/herdr_routines/config.py:173` `default_config_path` (unchanged but document interaction with `default_repos_dir`), `src/herdr_routines/config.py:436` `repo` parsing — add `repository` validation, conditional `repo` requirement, derived `repo = repos_dir / name`, URL-shape check, preserve `NAME_RE:70`/`VALID_*` contracts.
- `src/herdr_routines/repos.py` (new, or helpers in `runner.py:1`) — `default_repos_dir()`, `ensure_repo(job) -> Path` with atomic `git clone` to tmp+rename, `git fetch` + `--ff-only` merge, timeouts, clean failure returns; no Herdr dependency, only `subprocess`.
- `src/herdr_routines/runner.py:246` `default_reports_dir` pattern reused for `default_repos_dir`; `src/herdr_routines/runner.py:311` `build_dry_run_argv` and `src/herdr_routines/runner.py:382` `execute_run` — call `ensure_repo` before pane/worktree creation, map clone/pull failure to `RunOutcome(state="failed", reason="clone_failed"|"repo_sync_failed")`, no agent spawn on failure.
- `src/herdr_routines/tick.py:122` `_process_gated_job`, `src/herdr_routines/tick.py:468` `_process_base_target`, `src/herdr_routines/tick.py:854` `_dispatch_fix_worker`, `src/herdr_routines/tick.py:1027` `_process_job` — same `ensure_repo` gate before any `git worktree add` or `list_open_prs` that depends on `job.repo`.
- `src/herdr_routines/cli.py:388` `_cmd_validate` — skip existence/`.git` errors for `repository` jobs, warn if not yet cloned, keep `SYSTEMD_TIMEOUT_MARGIN`/`_check_systemd_timeout:446` unchanged.
- `tests/test_config.py` — matrix: `repository` URL validation, `repo` absent + `repository` present derives `repos/<name>`, both present uses explicit `repo`, neither → error, `repository` jobs pass `validate` without checkout on disk, `NAME_RE` still enforced on derived path.
- `tests/test_runner.py` / `tests/test_repos.py` — clone-if-missing (tmp+rename, failure cleans tmp and returns `clone_failed`), fetch+ff on existing checkout, fetch failure → `repo_sync_failed`, non-fast-forward → `repo_sync_failed`, no agent spawned on either failure, explicit `repo`+`repository` uses explicit path, legacy `repo` jobs do not fetch.
- `tests/test_tick.py` — gated base/pr dispatch still calls `ensure_repo` before worktree, tick writes terminal `failed` record with `reason` on clone failure.
- `deploy/jobs.example.yaml` — example `repository:` job comment, managed path note.

## Risks

- **Auth / private repos.** `git clone`/`fetch` inherits host credentials; `gh auth`-only hosts cannot clone `https://` without credential helper. Mitigation: fail cleanly as `clone_failed`/`repo_sync_failed` with remote stderr, never fall through to agent; document that private `repository` requires SSH key or `git credential` on the Pi/laptop.
- **Large initial clone stalls tick.** Full clone of a large repo can exceed `start_timeout_ms`/`timeout_ms` budgets. Mitigation: `ensure_repo` runs with its own bounded timeout (e.g. 120s per `git` call) and its failure is a terminal `failed` record, not a wedged tick; `validate --systemd-unit` budget does not need to cover clone because tick already holds `TimeoutStartSec` margin, but note worst-case clone time in `deploy/README.md`.
- **Concurrent fetch / worktree collision.** Tick holds `tick.lock:65` so two ticks cannot `fetch` the same checkout concurrently; fix workers for the same job also run sequentially per `run_tick:105` loop. Gated base-target creates per-`run_id` worktrees under `job.repo/.worktrees` — unaffected by managed path, but ensure `ensure_repo` fetch happens before `worktree add`.
- **Non-fast-forward / dirty checkout.** Local manual commits or a diverged managed checkout make `--ff-only` fail. Mitigation: treat as `repo_sync_failed` (clean failure, operator must reconcile), never force-merge or reset that could discard work; leave checkout untouched.
- **Partial clone left behind.** Killed `git clone` mid-write would leave a half-tree that a retry would mistake for a valid checkout. Mitigation: clone to `<checkout>.tmp.<run_id>` then atomic rename; on any failure remove tmp dir; next tick retries from scratch.
- **Path portability.** `jobs.yaml` with bare `repository` is portable, but derived `repos/<name>` differs per host's `HERDR_PLUGIN_STATE_DIR`. Mitigation: keep derivation deterministic per job name, document that moving a checkout does not require config change — only the URL matters.
- **Validation drift.** Existing `validate` callers (CI, manual) expect `repo path does not exist` errors to flag misconfig. For `repository` jobs that error must become a warning until first run. Mitigation: split `validate` logic on `job.repository is not None`; keep hard error for malformed URL, downgrade missing-checkout to warning so CI does not break portable jobs.
- **Spec-path hygiene (G-15).** Must stay at `docs/pipeline/runs/<run_id>/spec.md`; writing to repo-root `spec.md` or `docs/pipeline/spec.md` reintroduces PR #28/#29 full-file merge conflict. This spec is already at the per-run path (`mkdir -p .../20260902T050021Z` per task).

## Acceptance criteria

1. job with `repository: <git-url>` and no existing checkout clones on first run to deterministic `default_repos_dir() / job.name` via atomic `git clone` to `<checkout>.tmp.<run_id>` then rename, `default_repos_dir` follows `HERDR_PLUGIN_STATE_DIR` else `~/.local/state/herdr-routines/repos` — blocking, confidence: high — Test: test_repo_url_clone_if_missing
2. existing managed checkout on subsequent runs runs `git -C <checkout> fetch --prune origin` then `merge --ff-only origin/<base>` (or `origin/HEAD` if detached), idempotent fast-forward only, fetch alone for detached base-target worktree — blocking, confidence: high — Test: test_repo_url_fetch_fast_forward
3. job with explicit `repo:` keeps working unchanged, legacy `repo`-only jobs never enter `repository`/`ensure_repo` fetch path and still require existing checkout — blocking, confidence: high — Test: test_repo_url_explicit_repo_unchanged
4. clone failure is clean terminal `failed` with `reason=clone_failed` and `error=stderr`, no agent or worktree created, tmp clone dir removed on error so next tick retries full clone not partial checkout the agent runs against — blocking, confidence: high — Test: test_repo_url_clone_failed_clean
5. fetch failure or non-fast-forward diverged checkout yields clean `failed` with `reason=repo_sync_failed`, no agent spawned, no forced merge/reset, checkout left untouched (half-merged state impossible), safe to retry — blocking, confidence: high — Test: test_repo_url_repo_sync_failed_non_fast_forward
6. config validation: `repository` non-empty URL-shape `https://`|`ssh://`|`git@host:`|`git://` accepted bare paths rejected, neither `repo` nor `repository` → `ConfigError`, `repository` alone derives `repos/<name>`, both present uses explicit `repo` with `repository` as remote, `repository` jobs load without checkout on disk — blocking, confidence: high — Test: test_repo_url_validation_and_derivation
7. `cli validate` for `repository` jobs skips `repo.exists()`/`.git` hard errors, warns `repository job not yet cloned: <checkout>` when absent, errors only on malformed `repository` URL, read-only commands `status`/`scheduled`/`ps`/`validate`/`build_dry_run_argv` never trigger clone — blocking, confidence: medium — Test: test_repo_url_validate_skips_existence
8. `runner.execute_run` and `tick._process_job`/`_process_gated_job`/`_process_base_target`/`_dispatch_fix_worker` call `ensure_repo` before any `worktree_create`/`tab_create`/`list_open_prs`, per-git-call bounded timeout (e.g. 120s), tick lock (`tick.lock:65` + `run_tick:105` sequential) prevents concurrent fetch, gated base path fetches before `git worktree add` — blocking, confidence: medium — Test: test_repo_url_tick_runner_gate
9. `deploy/jobs.example.yaml` adds commented `repository: https://github.com/org/repo.git` example alongside `repo:` with managed path note, `default_repos_dir` pattern documented — non-blocking, confidence: medium — Test: test_repo_url_example_and_docs

## Changelog v1→v2

- v1 (138078c) established the `repository` optional clone lifecycle: `repository` field, conditional `repo` requirement, `default_repos_dir()` per `HERDR_PLUGIN_STATE_DIR`, atomic `git clone` to tmp+rename, `fetch --prune` + `--ff-only` fast-forward, clean `clone_failed`/`repo_sync_failed` terminal outcomes, `validate` split for `repository` jobs, and `runner`/`tick` `ensure_repo` gating.
- v2 reformats acceptance to 9 numbered items where each line ends with its `Test:` marker for `rg -F` discovery, adds explicit `blocking`/`non-blocking` tier labels and `confidence:` tiers per item, and splits clone-if-missing, fetch fast-forward, explicit `repo` unchanged, clone failure clean, sync/non-ff failure, validation/derivation, validate skip, tick/runner gate, and example docs for gate-2 discovery.
- Added items 4 `test_repo_url_clone_failed_clean` (tmp cleanup, no partial checkout, no agent, retry from scratch) and 5 `test_repo_url_repo_sync_failed_non_fast_forward` (fetch failure and diverged `--ff-only` failure as `repo_sync_failed`, checkout untouched) which were implicit in Approach/Risks v1 but not separately acceptance-mapped; now mirrors `docs/pipeline/runs/20260831T012350Z/spec.md` v2 style where branch/agent naming was promoted to its own item.
- Expanded items 1, 2, 6, 7, 8 to state deterministic `repos/<name>` derivation (not URL hash), `HERDR_PLUGIN_STATE_DIR` fallback, `fetch --prune` + `merge --ff-only origin/<base>` vs detached `origin/HEAD`, URL-shape `https://`/`ssh://`/`git@host:`/`git://` acceptance vs bare-path rejection, `validate` warning vs error split, read-only commands never mutate, and `tick.lock:65`/`run_tick:105` sequential fetch-before-worktree ordering from `docs/process/issues/016-repository-git-url-job-field.md` acceptance.
- Preserved all Problem/Approach/Files touched/Risks intent from v1; only tightened verifiability for stage-2 gate. Previous content remains authoritative for implementation.

## Review notes

- Tiers follow code-review skill convention: `blocking` findings must be resolved before merge, `non-blocking` are advisory — confidence: high for clone/fetch/sync-failure/validation/explicit-repo, confidence: medium for validate-skip/tick-gate/example-docs, confidence: low for flaky `gh`/`herdr` shape drift.
- Acceptance mapping verified end-to-end for `rg` checks: each acceptance line contains `Test:`, one of `blocking`/`non-blocking`, and `confidence:` — Test: test_repo_url_review_tiers_present
