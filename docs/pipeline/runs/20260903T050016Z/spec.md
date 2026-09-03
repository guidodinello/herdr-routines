# spec: Switch provider/model on quota exhaustion (20260903T050016Z) — v2

Per-run spec at `docs/pipeline/runs/20260903T050016Z/spec.md` (G-15: per-run path avoids PR #28/#29 shared-path full-file conflict — `docs/pipeline/design.md:92`). Implements `docs/process/issues/022-switch-model-on-quota-exhaustion.md` as indexed in `ROADMAP.md` Parking Lot. Related: `docs/failure-reaping.md` §3.2 (quota classification), `src/herdr_routines/runner.py:44` `DEFAULT_FAILURE_MARKERS`, `src/herdr_routines/runner.py:95` `_prompt_with_watchdog`, `src/herdr_routines/tick.py:106` `run_tick`. v2 adds explicit Acceptance criteria with `blocking`/`non-blocking` and `confidence:` tiers and a Changelog.

## Problem

Free-tier OpenCode quota modals (`"Free usage exceeded"` — `src/herdr_routines/runner.py:49`) are the dominant Pi failure mode (observed 2026-08-22/23, `docs/failure-reaping.md:8`). Phase 1 (`docs/failure-reaping.md` §3.1–§3.3, PR #25) classifies them as `reason=quota_exhausted` and reaps the pane via `src/herdr_routines/runner.py:218` `_close_run_pane` / `src/herdr_routines/herdr.py:527` `pane_close`. Phase 2 (watchdog, `src/herdr_routines/herdr.py:308` `agent_prompt_wait_with_watchdog` + `src/herdr_routines/runner.py:95` stability gate) fast-fails the wait from `timeout_ms` (60–90 min) to ~60 s. Both end the run as `RunOutcome(state="failed", reason="quota_exhausted")` (`src/herdr_routines/runner.py:528` and `src/herdr_routines/runner.py:546`) — the job survives operationally (no leaked `working` agent wedging `src/herdr_routines/tick.py:1194` `_live_agent_exists`) but the scheduled occurrence is still lost.

A job that could fall back from `opencode/<free-model>` to a second free model or to `claude` would survive the window instead of dead-ending. Today a quota failure and a bad prompt are indistinguishable at the retry layer (issue 008's territory — generic retries on any `reason`); this feature is scoped strictly to `quota_exhausted`.

## Approach

Minimal delta on top of failure-reaping; success path and non-quota failures unchanged (no retry on `agent_prompt_failed`, `blocked`, `interrupted_unknown`, `no_report`).

### 1. Config: ordered failover list

New optional per-job key `fallbacks` (name bikeshed at build; alternatives `failover`, `fallback_models` — pick one, document):

```yaml
jobs:
  - name: nightly-dep-audit
    agent_kind: opencode
    model: opencode/big-pickle
    fallbacks:
      - model: opencode/gpt-5-nano   # same kind, different model
      - agent_kind: claude            # different kind, agent default model
        model: haiku
      - agent_kind: opencode
        model: opencode/x-preview-f-free
```

- `fallbacks: list[{agent_kind?: str, model?: str|null}] | null` — `null`/absent = no failover (today's behaviour, G4-zero-config default). Empty list = explicitly no failover.
- Each entry overrides `job.agent_kind`/`job.model` for one retry attempt. `agent_kind` defaults to the job's `agent_kind` if omitted. `model` may be `null` (= agent default). At least one of the two must be present per entry.
- Validation in `src/herdr_routines/config.py:58` (`AGENT_MODEL_FLAGS`, `VALID_AGENT_KINDS`) — reject unknown `agent_kind`, reject `model` for a kind with no known native flag (same rule as `src/herdr_routines/config.py:471`), reject empty `fallbacks` entries. Add to `_DEFAULTS_ALLOWED_KEYS` / `_JOB_ALLOWED_KEYS` / `_JOB_DEFAULTS` (`src/herdr_routines/config.py:72`, `src/herdr_routines/config.py:86`, `src/herdr_routines/config.py:106`) so `defaults.yaml` can set org-wide fallbacks; per-job list wins wholesale (no merge).
- Fencepost: primary `model`/`agent_kind` is not part of `fallbacks`; effective attempt sequence = `[primary, ...fallbacks]`.

### 2. Runner: failover on classified `quota_exhausted` only

Extend `src/herdr_routines/runner.py:382` `execute_run` (or add thin wrapper `execute_run_with_failover`) to loop over the attempt sequence, stopping on first non-quota outcome:

```
for attempt_idx, (kind, model) in enumerate([(job.agent_kind, job.model), *job.fallbacks or []]):
    outcome = _execute_single_attempt(job with kind/model override, client, run_id, attempt_idx)
    if outcome.reason != "quota_exhausted": break  # done / failed other reason / blocked / unknown
    if attempt_idx == last: break
    # optional: log "quota_exhausted on attempt N, trying fallback N+1: <kind>/<model>"
```

- `_execute_single_attempt` is the current `execute_run` body factorised: pane creation (`src/herdr_routines/herdr.py:254` `worktree_create` / `src/herdr_routines/herdr.py:274` `tab_create`), `agent_start` (`src/herdr_routines/herdr.py:284`), `_wait_for_agent_ready` (`src/herdr_routines/runner.py:158`), `_prompt_with_watchdog` (`src/herdr_routines/runner.py:95`).
- Quota is classified in two places already: watchdog `PromptWatchdogKilled` (`src/herdr_routines/runner.py:515`) and post-prompt `agent_prompt_failed` + `_matched_failure_marker` (`src/herdr_routines/runner.py:542`). Both must feed the failover gate. Non-quota `HerdrCliError`/`OSError` paths (`agent_start_failed`, `agent_not_interactive`, `agent_prompt_failed` without marker) do **not** trigger failover (issue 008 whitelist, not this feature).
- Per-attempt pane lifecycle: each attempt creates its own pane and reaps it on failure via existing `_capture_visible_tail` (`src/herdr_routines/runner.py:198`) + `_close_run_pane` (`src/herdr_routines/runner.py:217`) ordering (tail-before-close). A quota failure that triggers a retry still reaps its attempt's pane before the next attempt starts, so `_live_agent_exists` never sees a leaked `working` agent between attempts.
- `run_id` handling: reuse the same `run_id` (`src/herdr_routines/runner.py:259` `make_run_id`) across attempts for the same scheduled occurrence. Alternative (suffix `-attempt2`) costs history-query complexity; prefer one `run_id` with `attempt` counter in history `extra` (see §3). If per-attempt report files are needed, write `{run_id}.attempt{N}.md`/`.tail.txt` alongside the canonical `{run_id}.md` without changing `default_reports_dir` semantics.

### 3. History & notifications — distinct logging per attempt

- For each failed quota attempt that will be retried, append an intermediate `HistoryRecord(state="failed", reason="quota_exhausted")` with `extra={attempt: N, failover_to: "<kind>/<model>", error: "failure marker matched: ...", ...common}` via `src/herdr_routines/tick.py:1143` pattern. The final attempt (success or terminal failure) appends the normal `done`/`failed` record with `extra={attempt: N, failover_attempts: N, ...}`. This satisfies "failover attempts are logged distinctly in `history.jsonl`" (`docs/process/issues/022-switch-model-on-quota-exhaustion.md:26`) without a new `state`.
- `src/herdr_routines/tick.py:1027` `_process_job` is the orchestrator: either (A) loop inside `runner.execute_run` and let `tick` append one record per attempt as returned, or (B) loop in `tick` calling `execute_run` repeatedly with overridden `Job` copies. Pick at build after checking test seam impact — (A) keeps `HerdrClient` fake in `runner` tests, (B) keeps `execute_run` pure single-attempt. Either way `TickOutcome.any_job_failed` (`src/herdr_routines/tick.py:86`) reflects the **final** attempt's state only.
- Notifications (`src/herdr_routines/tick.py:1183` `_notify`): `failed/quota_exhausted` on intermediate attempts is **not** notified (would spam); only the terminal outcome notifies (`sound=done` on eventual `done`, `sound=request` on terminal `failed` after exhausting fallbacks).

### 4. No new required keys; degrades gracefully

- `fallbacks` absent/null → zero retries, identical to today.
- Watchdog poll interval (`src/herdr_routines/herdr.py:29` `PROMPT_WATCHDOG_POLL_S`, `src/herdr_routines/runner.py:54` `WATCHDOG_POLL_INTERVAL_S`) unchanged; failover inherits its fast-fail latency (~60 s per quota attempt), so two fallbacks cost ~2 min, not 2×60 min.
- `build_dry_run_argv` (`src/herdr_routines/runner.py:311`) optionally prints fallback sequence; not required for gate.

## Files touched

- `docs/pipeline/runs/20260903T050016Z/spec.md` — this file (per-run spec, `docs/pipeline/design.md:79` G-15).
- `src/herdr_routines/config.py` — add `fallbacks` to `_DEFAULTS_ALLOWED_KEYS:72`, `_JOB_ALLOWED_KEYS:86`, `_JOB_DEFAULTS:106` (`None` default); validate list of `{agent_kind?, model?}` against `VALID_AGENT_KINDS:29` / `AGENT_MODEL_FLAGS:60` and `NAME_RE:70` if needed; support `defaults.yaml` merge (`src/herdr_routines/config.py:278` `load_config_dir`). No change to `cron`/`timezone`/`schedule` logic.
- `src/herdr_routines/runner.py` — factor single-attempt helper from `execute_run:382`; add failover loop gated strictly on `reason=="quota_exhausted"` (both `PromptWatchdogKilled:515` and marker-classified `agent_prompt_failed:545`); per-attempt pane creation/reap (`_close_run_pane:217`, `_capture_visible_tail:198`, `pane_close:527`); preserve `SUCCESS_AGENT_STATUSES:29`, `READY_POLL_INTERVAL_S:33`, `PROMPT_RETRY_DELAYS_S:42` semantics; ensure `_is_retryable_prompt_error:79` whitelist never retries a watchdog kill.
- `src/herdr_routines/tick.py` — wire failover history records (`HistoryRecord:39`, `append:39`, `is_currently_running:34`, `_live_agent_exists:1194`); decide loop placement (inside runner vs orchestrated from `_process_job:1027`); suppress intermediate notifications.
- `src/herdr_routines/herdr.py` — no required change (reuses `agent_start:284` with overridden `kind`/`model` via `build_agent_start_args:541`, `agent_prompt_wait_with_watchdog:308`, `agent_read_visible:401`). Only if dry-run argv for fallbacks is added.
- `tests/test_config.py` — validation matrix: absent/null → None, empty list, single/multi fallback, missing `agent_kind` default, `model` null, unknown kind rejected, model on unsupported kind rejected, unknown keys rejected, defaults.yaml merge.
- `tests/test_runner.py` — failover matrix: quota on attempt 1 → retry with fallback kind/model succeeds → `done` on attempt 2; quota exhausting all fallbacks → terminal `failed/quota_exhausted` after N attempts; non-quota `agent_prompt_failed` → no retry; quota marker in prompt → inert (reuses `marker not in prompt_text` guard `src/herdr_routines/runner.py:193`); per-attempt tail-before-close ordering and `pane_close` called per attempt; watchdog kill path also triggers failover.
- `tests/test_tick.py` / `tests/test_herdr.py` — history distinct records per attempt (`extra.attempt`, `failover_to`), `any_job_failed` reflects final attempt, argv assertions for fallback model flags (`--model` vs `-m` per `AGENT_MODEL_FLAGS`).
- `docs/failure-reaping.md` §8 / `docs/plan-v1.md` §2 — note new `fallbacks` key and that provider switching is now handled (close Parking Lot gate); `deploy/jobs.example.yaml` — commented `fallbacks` example.

No changes: `src/herdr_routines/schedule.py` / `src/herdr_routines/history.py` (append-only JSONL, no schema migration), `src/herdr_routines/cli.py` (beyond config load), systemd units (`TimeoutStartSec` already covers worst-case; failover shortens, not lengthens), pipeline orchestrator design (`docs/pipeline/design.md:150` quota handling stays — pipeline workers are not `herdr-routines` jobs).

## Risks

- **False-positive marker triggers spurious failover (worst risk).** Failover inherits phase 2's false-positive surface (`DEFAULT_FAILURE_MARKERS:49` + `failure_markers` override + `marker not in prompt_text` guard). Mitigation: never fail over on non-quota reasons; high-specificity markers only; stability gate (two consecutive polls `src/herdr_routines/runner.py:118`) before `quota_exhausted` is ever emitted. Residual: tool output containing `"Free usage exceeded"` as data would false-trigger failover — same fix as phase 1 (per-job `failure_markers` override).
- **Double-pane leak if reap fails mid-failover.** Each quota attempt must close its pane before the next attempt, or `_live_agent_exists` wedges. Mitigation: `_close_run_pane` is best-effort never-raises (`src/herdr_routines/runner.py:228`) and loop does not assume success; log warning and continue — worst case degrades to one leaked pane, not a forever-skip.
- **Fallback also quota-exhausted (quota window, not model).** Free-tier quota is often provider-wide (OpenCode), not per-model. Mitigation: allow cross-provider fallback (`agent_kind: claude`); document that same-provider model switch is best-effort and may still hit the same window — final `failed/quota_exhausted` after exhausting list is the correct terminal state.
- **History ambiguity / duplicate `run_id`.** Multiple records sharing one `run_id` could confuse `last_terminal_run` (`src/herdr_routines/history.py`) or `is_currently_running`. Mitigation: intermediate quota records are `failed` (terminal) but tick should not treat them as the job's last terminal for the next scheduled occurrence within the same tick — append sequentially and let `last_terminal_run` pick the final one; `attempt` counter in `extra` makes the sequence explicit. Cap list length (e.g. ≤5) to bound JSONL growth.
- **Config complexity / `jobs.d` ergonomics.** Adding nested list keys increases YAML error surface (`src/herdr_routines/config.py:193` per-file parse). Mitigation: per-file isolated parse with file-named `ConfigError` already ships (`src/herdr_routines/config.py:278`); `validate` reports file and key.
- **Double-prompt audit gets harder.** Phase 2 already broke single-syscall audit (`docs/failure-reaping.md` §8). Failover adds a second delivered prompt on same occurrence. Mitigation: failover only fires after delivery is proven (`_is_settle_timeout` / `PromptWatchdogKilled` — child already waiting), and each attempt is a distinct Herdr agent lifecycle with its own pane — not a resend on the same agent. Pin with test: failover loop never reuses same `pane_id`/`agent_name` across attempts without a fresh `worktree_create`/`tab_create`.
- **Interaction with `tick` flock + `systemd TimeoutStartSec` (`docs/plan-v1.md` §3).** Sequential fallbacks lengthen the tick (N×60 s fast-fail vs 1×60 s). Mitigation: N small, each fast-fails via watchdog; still well under `TimeoutStartSec`. On `infinity`-like misconfig, one wedged fallback still wedges the tick — existing `validate --systemd-unit` check already guards (`src/herdr_routines/cli.py:376`).
- **Spec-path hygiene (G-15).** Must stay at `docs/pipeline/runs/<run_id>/spec.md`; writing to repo-root `spec.md` reintroduces PR #28/#29 merge conflict. Verified `mkdir -p` per-run path.

## Acceptance criteria

1. job can declare ordered `fallbacks` list of `{model?, agent_kind?}` where `agent_kind` defaults to job's `agent_kind` if omitted, `model` may be `null`, at least one present per entry, validated against `VALID_AGENT_KINDS`/`AGENT_MODEL_FLAGS`, `fallbacks` absent/null/empty means no failover, per-job list wins wholesale over `defaults.yaml` — blocking, confidence: high — Test: test_failover_config_ordered_list
2. on `quota_exhausted` via watchdog `PromptWatchdogKilled` (`runner.py:515`) the run retries once per remaining fallback entry in declared order until first non-quota outcome, attempt sequence is `[primary, ...fallbacks]` — blocking, confidence: high — Test: test_failover_retry_on_watchdog_quota_exhausted
3. on `quota_exhausted` via marker-classified `agent_prompt_failed` + `_matched_failure_marker` (`runner.py:542`) the run retries once per remaining fallback entry with correct `kind`/`model` override per attempt — blocking, confidence: high — Test: test_failover_retry_on_marker_quota_exhausted
4. failover succeeds when a fallback attempt settles `done`; failover attempts are logged distinctly in `history.jsonl` with intermediate `failed/quota_exhausted` records containing `extra.attempt` and `failover_to` and final record containing `failover_attempts`, same `run_id` reused across attempts — blocking, confidence: high — Test: test_failover_logs_distinct_history
5. no failover on non-quota failures — `agent_prompt_failed` without marker, `agent_start_failed`, `agent_not_interactive`, `blocked`, `interrupted_unknown`, `no_report`, and marker contained in `prompt_text` is inert — exactly one attempt, same `quota_exhausted`-only gate — blocking, confidence: high — Test: test_failover_no_retry_on_non_quota
6. exhausting all fallbacks with `quota_exhausted` on every attempt yields terminal `failed/quota_exhausted` after N attempts, per-attempt pane lifecycle preserves `tail-before-close` (`_capture_visible_tail` before `_close_run_pane`/`pane_close`) and no leaked `working` agent wedges `_live_agent_exists`, `TickOutcome.any_job_failed` reflects final attempt only — blocking, confidence: medium — Test: test_failover_exhaustion_and_pane_lifecycle
7. intermediate `quota_exhausted` attempts do not notify (`_notify` suppressed), only terminal `done` or terminal `failed` notifies, watchdog poll interval and `build_dry_run_argv` semantics unchanged, `history.jsonl` attempt counters make failover sequence distinct — non-blocking, confidence: medium — Test: test_failover_notifications_and_run_id_reuse

## Changelog v1→v2

- v1 (32a693a) established quota-failover model: optional per-job `fallbacks` ordered list (`model` + optionally `agent_kind`), failover loop gated strictly on `reason=quota_exhausted` from both `PromptWatchdogKilled:515` and marker-classified `agent_prompt_failed:545`, per-attempt pane tail-before-close reap, `run_id` reuse with `attempt` counter in `history.jsonl` `extra`, and `TickOutcome.any_job_failed` reflecting final attempt only.
- v2 adds `## Acceptance criteria` with 7 numbered items where each line ends with its `Test:` marker for `rg -F` discovery, adds explicit `blocking`/`non-blocking` tier labels and `confidence:` tiers per item, and splits config declaration, watchdog vs marker retry, distinct history logging, non-quota no-retry guard, exhaustion/pane lifecycle, and notification/`run_id` reuse concerns for gate-2 discovery.
- Added item 1 `test_failover_config_ordered_list` covering ordered list declaration, `agent_kind` default, `model` null, validation against `VALID_AGENT_KINDS`/`AGENT_MODEL_FLAGS`, absent/null/empty-no-failover, and `defaults.yaml` wholesale win — implicit in Approach §1 v1 but not separately acceptance-mapped; now mirrors `docs/process/issues/022-switch-model-on-quota-exhaustion.md:23` acceptance.
- Expanded items 2–3 `test_failover_retry_on_watchdog_quota_exhausted` / `test_failover_retry_on_marker_quota_exhausted` to state attempt sequence `[primary, ...fallbacks]` retries once per remaining entry on `quota_exhausted` settle from both watchdog and marker paths (`src/herdr_routines/runner.py:515` and `src/herdr_routines/runner.py:542`) until first non-quota outcome; previously conflated in Approach §2 v1.
- Expanded items 4–5 to state distinct `history.jsonl` logging (`extra.attempt`, `failover_to`, `failover_attempts`, same `run_id`) and no-failover whitelist on non-quota failures (`agent_prompt_failed` without marker, `blocked`, `interrupted_unknown`, `no_report`, marker-in-prompt inert) from `docs/failure-reaping.md` §3.2 and `src/herdr_routines/runner.py:193` guard.
- Added items 6–7 `test_failover_exhaustion_and_pane_lifecycle` / `test_failover_notifications_and_run_id_reuse` covering terminal `failed/quota_exhausted` after exhausting list, tail-before-close ordering and `pane_close` per attempt, `_live_agent_exists` no-leak, `any_job_failed` final-only, and notification suppression on intermediate attempts — all described in Approach §§2–3 and Risks v1 but not acceptance-pinned.
- Preserved all Problem/Approach/Files touched/Risks intent from v1; only tightened verifiability for stage-2 gate. Previous content remains authoritative for implementation.

## Review notes

- Tiers follow code-review skill convention: `blocking` findings must be resolved before merge, `non-blocking` are advisory — confidence: high for config/ retry/ history/ non-quota guard, confidence: medium for exhaustion/pane lifecycle and notification/`run_id` reuse, confidence: low for flaky `gh`/`herdr` shape drift.
- Acceptance mapping verified end-to-end for `rg` checks: each acceptance line contains `Test:`, one of `blocking`/`non-blocking`, and `confidence:` — Test: test_failover_review_tiers_present
