# spec v1 — issue 077: move `gated` jobs' `github_event` checks onto `trigger.event`

Run: 20261101T050000Z · Issue: `docs/process/issues/077-gated-github-event-checks-as-event-triggers.md` · Related: 014, 075, 076 (golden-test pattern), 070.

## Problem

Two mechanisms answer "did a matching event happen?":

1. **014's gated `github_event` check** — polled only when the job's cron fires (`tick._gated_cycle` → `_process_event_target`, tick.py:3388-3401). Own watermark (`last_seen_event_ids` in history extras, read back by `_last_seen_event_ids`), per-event attempt budget (`attempt_count_for_event` vs `max_attempts_per_target`, tick.py:1400), head-ref validation, fail-closed poll errors, `issues:opened` baseline adoption.
2. **075's `trigger.event`** — polled every tick (`_poll_event_trigger`, tick.py:3136). Fires the job via `dispatch_occurrence`; watermark written *before* dispatch (at-most-once); retries left to the job's `retry_on`/`fallback_model`; no per-event budget.

`config.py:1605-1608` rejects `trigger.event` on a gated job that has a `github_event` check, because both would write the same per-job watermark. The gated check is the special-casing 071's dispatch split exists to remove. Note: `checks` cannot mix kinds (config.py:1541-1544), so any gated job with a `github_event` check has that check as its *only* check — the migration target is always "the check *is* the gate" and is clean.

## Approach

1. **Golden test first (076's pattern).** Commit `test_gated_event_check_behaviour_unchanged_after_migration` on unmodified code; verify it passes before touching anything. It pins: poll → dedup against watermark → per-fire behavior (dispatch, over-cap deferral, `max_attempts_exceeded` skip with notification, head-ref skip vs retry, poll failure fail-closed, `issues:opened` first-poll baseline adoption) and the history-record shapes each step writes.
2. **Config sugar, not file migration (076's `kind: review` precedent).** A gated job whose checks are all `github_event` loads unchanged; `load_config` normalizes it — `job.trigger_event` is set from the check's shape (`query`/`repo`/`match`), and the cron-path checks no longer carry the predicate. No one-time migration of the Pi's file is needed (and the committed `deploy/jobs.d/babysit-prs.yaml` uses `pr_health`, untouched). A job that explicitly declares *both* `trigger.event` and a `github_event` check is a load error (it used to be implicitly rejected at config.py:1605 — now spell the conflict out); the blanket 075 rejection is removed.
3. **Single mechanism in the tick.** `_gated_cycle` loses its `github_event` branch; the moved job's event firing goes through `_poll_event_trigger`. The 014-specific logic that must survive the move lifts out of `_process_event_target` into the trigger path:
   - **Per-event attempt budget (retained — this is the issue's decisive question).** Before dispatching an event, the trigger path checks `attempt_count_for_event(history, job, event_id)` and records `skipped / max_attempts_exceeded` (same extras, same notification policy hook) when it is exhausted. Vanilla jobs that declare `trigger.event` directly keep 075's at-most-once semantics; only migrated gated jobs get the budget. Why: a gated job is an unattended fix workflow whose individual dispatch plausibly fails transiently (dirty worktree, bad head ref) — at-most-once would drop fixes silently, which is exactly the failure class 014's budget exists to prevent. A routine job firing on an event can afford at-most-once because its `retry_on`/`fallback_model` own retry policy.
   - **070's dirty-worktree rule retained.** The fired event still flows into the same pr-target fix path with the event's enriched context (PR number, head ref) so `worktree_reuse_not_clean` refusal and `_dirty_worktree_already_reported` behave as before.
   - Fail-closed poll errors, `issues:opened` baseline adoption, and the "event clears and comes back fires again" watermark rule carry over unchanged.
4. **AC2 — `test_single_event_watermark_per_job`:** for any job (declared or sugar-loaded) exactly one history path writes `last_seen_event_ids`; the config-level dual-writer conflict is gone because the dual writer is, not because a validation error masks it.
5. Docs/examples touch-ups where the two mechanisms are described (`deploy/jobs.example.yaml`, `ROADMAP.md` G-98 area, issue-014/075 references), plus removal of the stale "cannot be combined" validation text.

## Files touched

- `src/herdr_routines/config.py` — normalizing sugar for gated `github_event` checks; drop the rejection at :1605-1608; new conflict validation (sugar + explicit `trigger.event`).
- `src/herdr_routines/tick.py` — `_gated_cycle` github_event branch removed; budget/baseline/head-ref/fail-closed logic moved into `_poll_event_trigger`; `_process_event_target` retired or reduced to what the trigger path reuses.
- `tests/test_github_event.py` and/or a new `tests/test_gated_event_migration.py` — the two named tests; existing tests pinned to the 014 record shapes updated in step with (and only after) the golden test passes.
- `deploy/jobs.example.yaml`, `ROADMAP.md` — doc alignment.

## Risks

- **Record-shape drift breaks readers, not just readers-of-tests.** `_last_seen_event_ids` reads the latest terminal record carrying the key; `attempt_count_for_event` scans history; `status`/notifications key off `gate`/`reason`/`phase`. The crossed-out 014 path wrote `gate=passed/baseline/failed` with a `run_id`; the 075 path writes `trigger=event, phase=event_poll`, often without a run_id. The golden test pins the externally observable shapes; acceptance criterion 1 means the *after* code must write shapes the same consumers understand, so the lifted logic writes 014-shaped records from the trigger path rather than thinning them into 075's quieter shapes.
- **First tick after upgrade.** Watermarks written by the old path must be read by the new one without refiring or dropping events (`issues:opened` baseline adoption re-arms only when no watermark exists — an existing watermark must prevent a spurious re-baseline).
- **Timing change is accepted, stated.** The poll cadence moves from the job's cron to every tick: events fire sooner. Same events, same skips, same budget (AC1's terms) — but notification cadence for `notify_policy: on-finding`-style jobs rises. Accepted as the point of the unification; noted so it isn't mistaken for a bug.
- `validate`'s tick-timeout budget (one poll with `check.timeout_ms` per tick per such job) must still hold for sugar-loaded jobs on every-tick cadence.
