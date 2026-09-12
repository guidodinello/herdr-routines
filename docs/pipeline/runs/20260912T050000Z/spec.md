# Spec — Per-job retry for transient failures (008) — 20260912T050000Z

Implements `docs/process/issues/008-retries-on-failure.md`. Per-run spec at `docs/pipeline/runs/20260912T050000Z/spec.md` (G-15: per-run path avoids PR shared-path conflict).

## Problem

Transient infrastructure failures end the run with no retry. Real Pi runs have shown `agent_start_failed`, `agent_not_interactive` (startup timeout — `runner.py:670` / `runner.py:686`), and `pane_creation_failed` / `clone_failed` / `repo_sync_failed` when `herdr-server` is down mid-run (`runner.py:584`, `runner.py:636`) as `RunOutcome.reason` values. Today `tick._process_job` (`src/herdr_routines/tick.py:1371`) calls `execute_run` once, appends one `failed` record, and returns — even when the reason is known-transient and a second attempt seconds later would succeed. Blind unconditional retry is explicitly rejected by the issue: a bad prompt or `no_report` / `blocked` / `quota_exhausted` / `gate_failed` failure is not fixable by re-running, and a non-idempotent job duplicated is worse than not retrying. There is no per-job opt-in, no eligibility filter, and no bounded-attempts contract. Default must remain no-retry so existing jobs are unaffected.

## Approach

Opt-in, per-job, `RunOutcome.reason`-gated retry loop around the existing single `execute_run` call path. Bounded, history-visible, default 0.

### 1. Config schema (`src/herdr_routines/config.py`)

Two new per-job keys, neither inheritable via `defaults.yaml` (`_DEFAULTS_ALLOWED_KEYS`):

- `retry_attempts: int` — total extra attempts after the first failure. `0` = no retries (default). Validated `0 <= retry_attempts <= 3` (cap prevents tick-hogging; matches fallback_model's single-retry precedent `tick.py:1373`). Non-int / bool / negative / >3 → `ConfigError`.
- `retry_on: list[str] | null` — whitelist of `RunOutcome.reason` values eligible for retry. `null` / absent = empty (no eligible reasons, so `retry_attempts` is inert). Validated: list of non-empty strings, each must be a known terminal reason string (closed set derived from `runner.py` + `tick.py` failure sites: `agent_start_failed`, `agent_not_interactive`, `pane_creation_failed`, `clone_failed`, `repo_sync_failed`, `agent_prompt_failed`, `tmp_full`, `report_dir_creation_failed`, `interrupted_unknown`/`unsettled_status_unknown`, `stale_running_record` is not a `RunOutcome.reason` — exclude). Unknown string → `ConfigError` with `retry_on` + value in message. Empty list is valid and means "opted into retries but nothing eligible" (warn, don't error).

Interaction rules:

- `retry_attempts > 0` with `retry_on` empty/null → `ConfigError` ("retry_attempts requires non-empty retry_on — declare which reasons are retry-eligible").
- `retry_on` non-empty with `retry_attempts == 0` → allowed but inert; `validate` emits soft `warning:` (exit 0) like the `$ROUTINE_REPORT` warning, not an error.
- `Job` dataclass gains `retry_attempts: int = 0` and `retry_on: tuple[str,...] | None = None` (frozen, slots). `retry_attempts` / `retry_on` are per-job only — adding either to `_DEFAULTS_ALLOWED_KEYS` is intentionally rejected (a shared `defaults.yaml` must not make every job retry-eligible implicitly; same precedent as `kind`/`checks` in issue 049 `config.py:102`).

Out of scope for this feature: `checks`/`target`-path gated jobs and `kind: pipeline` — the issue describes `execute_run` transient failures for routine jobs. Gated dispatch (`_process_gated_job` / `_process_base_target` / `_dispatch_fix_worker`) and pipeline detached launch keep current behavior; retry loop applies only in `_process_job`'s routine path (`tick.py:1250`).

### 2. Tick retry loop (`src/herdr_routines/tick.py`)

Wrap the current `outcome = execute_run(job, client, run_id=run_id)` + fallback block + single `append` terminal record (`tick.py:1371-1425`) in a bounded loop:

```
attempt = 0  # 0 = first try
while True:
    outcome = execute_run(job, client, run_id=attempt_run_id)  # attempt_run_id = run_id on attempt 0, f"{run_id}-retry{attempt}" on retries to keep branch/report distinct via build_branch_name injectivity
    # quota fallback (existing) runs first, still counts as one logical attempt
    if retry_eligible(outcome, job) and attempt < job.retry_attempts:
        log append of the failed attempt as distinct history record with attempt metadata (see §3)
        sleep with backoff (e.g. 5s, 15s — reuse PROMPT_RETRY_DELAYS_S shape) or immediate retry; bounded total wall-clock still inside job.timeout_ms budget per attempt, not summed
        attempt += 1; continue
    break
append terminal record for final outcome
```

- Eligibility: `outcome.state == "failed" and outcome.reason in set(job.retry_on or ())`. `interrupted_unknown` / `done` never retry; `blocked`, `no_report`, `quota_exhausted`, `unsettled_status_unknown` are not in the default eligible set and only retry if the job explicitly lists them (discouraged in docs; `quota_exhausted` already has its own `fallback_model` path `tick.py:1374` which stays separate and does not consume `retry_attempts`).
- Idempotency guard: docs must state retry is only safe for idempotent jobs; config does not enforce it (cannot statically), but `validate` warns when `retry_attempts > 0` and prompt contains mutation verbs without worktree isolation? No — keep warning simple: `retry_attempts > 0` on a `workspace: root` job warns that retries duplicate in-place side effects.
- Tick lock still held; retries happen synchronously within the same tick invocation, not deferred to next cron occurrence. This avoids collapsing via `catch_up_minutes` and keeps `late_seconds` / `scheduled_for` tied to the original occurrence.
- Existing `fallback_model` quota path stays orthogonal and runs inside each attempt before the eligibility check; it does not count toward `retry_attempts` and does not use retry history fields.

### 3. History logging (`src/herdr_routines/history.py` + `tick._outcome_extra`)

Each attempt writes its own terminal `HistoryRecord` (append-only JSONL, `history.jsonl`). Distinctness required by acceptance criterion:

- `run_id` for retries is suffixed (`{base_run_id}-retry1`, `-retry2` …) so `build_branch_name` (`runner.py:350`) yields a distinct `auto/<job>-<suffix>` branch and `default_reports_dir() / f"{run_id}.md"` a distinct report/tail file. Attempt 0 keeps the original `make_run_id(job.name, occurrence)` (`tick.py:1336`).
- `extra` gains `attempt: int` (0-indexed) and `retry_attempt: int` alias or `attempt_of: int` + `max_retries: int`, plus `retried_reason: str` on retried terminal records and `final_attempt: bool` on the last record. Minimum contract: `attempt` number visible and `retry_on` eligibility filter auditable from JSONL alone without reading `jobs.yaml`.
- `last_terminal_run(history_path, job.name)` (`history.py`) continues to return the latest terminal state (the final retry's record) — `schedule.decide` (`schedule.py`) sees only the final outcome for due/missed logic. Intermediate retry failures do not create a new `last` that would suppress the next scheduled occurrence.

### 4. Validation and CLI

- `herdr-routines validate` checks the two new keys, plus the cross-field rule above; emits `warning:` for `retry_on` non-empty with `retry_attempts == 0` and for `retry_attempts > 0` on `workspace: root`.
- `history` / `status` / `digest` need no change; they already render by `last_terminal_run` + `run_id` and will surface `-retryN` run_ids naturally.
- `run --dry-run` prints the would-be `herdr` argv for attempt 0 only; a `--retry` dry-run flag is out of scope.

### Sizing

Single PR, ~half-day. Config validation + `Job` fields, tick loop with eligibility predicate, history `attempt` stamping, tests. No pipeline/gated changes.

## Files touched

- `docs/pipeline/runs/20260912T050000Z/spec.md` — this file (per-run spec, G-15).
- `src/herdr_routines/config.py` — add `retry_attempts` / `retry_on` to `_JOB_ALLOWED_KEYS` (`:119`) and `Job` dataclass (`:199`), defaults in `_JOB_DEFAULTS` (`:145`), validation in `_build_job` (after `kind` block `~:720`), keep both out of `_DEFAULTS_ALLOWED_KEYS` (`:102`).
- `src/herdr_routines/tick.py` — wrap `execute_run` call in `_process_job` (`:1371`) with eligibility-checked bounded loop; suffix `run_id` per retry; append per-attempt `HistoryRecord` with `attempt` in `extra`; thread through `_outcome_extra` (`:1227`); keep fallback_model path inside loop iteration.
- `src/herdr_routines/history.py` — no format break; document `attempt`/`retried_reason` in `HistoryRecord.extra` contract; `last_terminal_run` semantics unchanged (final attempt wins).
- `src/herdr_routines/runner.py` — no change except `RunOutcome.reason` closed set documented for `retry_on` validation; `build_branch_name` injectivity already covers `-retryN` suffix (`:350`).
- `tests/test_config.py`, `tests/test_tick.py` — new tests for validation matrix and retry loop (see Acceptance below); no `test_history.py` format change test needed beyond `attempt` field presence.
- `deploy/jobs.example.yaml` / `docs/process/pi-update-runbook.md` — example job snippet showing `retry_attempts: 1` + `retry_on: [agent_start_failed, agent_not_interactive]`; no live-config migration (default 0 keeps existing jobs green).

Not touched: `src/herdr_routines/schedule.py` (catch-up/missed logic), gated job dispatch (`_process_gated_job`, `_process_pr_target`, `_process_base_target`, `_dispatch_fix_worker`), pipeline detached launch (`_process_pipeline_job`), `src/herdr_routines/cli.py` beyond validate warnings, `digest.py`.

## Risks

- [blocking] Non-idempotent jobs retried duplicate side effects (commit/push, external API call) — worse than not retrying. Mitigated by opt-in only (`retry_attempts` default 0), explicit `retry_on` whitelist, docs stating retries are only for idempotent jobs, and a `workspace: root` soft warning. — confidence: high
- [blocking] Retry loops wedge the tick (lock held, `TimeoutStartSec` in `deploy/systemd/herdr-routines.service` finite per `docs/plan-v1.md:279`). Bounded `retry_attempts <= 3` + per-attempt `timeout_ms` + short backoff keeps worst-case `3 * (start_timeout_ms + timeout_ms)` still inside human-reviewable bounds; `validate` must assert systemd timeout covers it or warn like the existing `TimeoutStartSec` check. — confidence: high
- [blocking] `retry_on` validated against an open-ended `reason` string set drifts as new `RunOutcome.reason` values are added. Mitigated by closed-set validation with `ConfigError` that names the allowed values; adding a new reason requires updating the set and tests together. — confidence: high
- [blocking] History ambiguity: two terminal records for one scheduled occurrence could confuse `last_terminal_run` / digest. Mitigated by suffixed `run_id` and `attempt` in `extra`; `decide` sees only final attempt's `last`. — confidence: high
- [non-blocking] `defaults.yaml` inheritance tempts shared retry policy. Rejected — keep both keys per-job only like `kind`/`checks` (issue 049 precedent); a global retry policy would silently make every job retry-eligible. — confidence: high
- [non-blocking] Interaction with `fallback_model` quota path (`tick.py:1374`): quota failure retries once via `fallback_model` before `retry_on` eligibility is checked, so a job listing `quota_exhausted` in `retry_on` would double-retry. Document that `quota_exhausted` should not be in `retry_on` (fallback covers it) and make eligibility check exclude it unless fallback is absent. — confidence: medium
- [non-blocking] Spec-path hygiene (G-15): must stay at `docs/pipeline/runs/<run_id>/spec.md`; writing to `docs/pipeline/spec.md` reintroduces PR #28/#29 shared-path merge conflict. — confidence: high

## Acceptance criteria

1. [blocking] A job can declare `retry_attempts` (0..3, default 0) and `retry_on` (list of known `RunOutcome.reason` strings, default null/empty) — unlisted reasons never retry. — confidence: high — Test: test_retry_config_requires_explicit_eligible_reasons
2. [blocking] Default is no retries: a job with no `retry_*` keys or `retry_attempts: 0` still fails once with no second `execute_run` call and one terminal history record. — confidence: high — Test: test_default_no_retry_unchanged
3. [blocking] Retry fires only when `outcome.reason in retry_on` and `attempt < retry_attempts`; other reasons (bad prompt, `no_report`, `blocked`) never retry even with `retry_attempts > 0`. — confidence: high — Test: test_retry_only_on_eligible_reason
4. [blocking] Retries are bounded: at most `retry_attempts` extra attempts, then the final failure is recorded. — confidence: high — Test: test_retry_bounded
5. [blocking] Each attempt is logged distinctly in `history.jsonl` with `attempt` number visible and distinct `run_id` (`-retryN` suffix) / branch / report path; `last_terminal_run` returns the final attempt. — confidence: high — Test: test_retry_history_distinct
6. [non-blocking] Validation rejects unknown `retry_on` strings, `retry_attempts` out of bounds, and `retry_attempts > 0` with empty `retry_on`; `retry_on` without `retry_attempts` warns but does not error. — confidence: medium — Test: test_retry_validation

## Review

- blocking: criteria 1–5 (opt-in, default-no-retry, eligibility gating, boundedness, distinct history).
- non-blocking: criterion 6 (validation matrix / warnings).
- confidence: high for blocking (directly tied to issue 008's three acceptance bullets + transient reasons already classified in `runner.py`/`tick.py`); medium for the validation edge matrix.


