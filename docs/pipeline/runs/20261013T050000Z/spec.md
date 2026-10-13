# spec v2 — issue 014: API / webhook trigger (run 20261013T050000Z)

Per-run spec at `docs/pipeline/runs/20261013T050000Z/spec.md` (per-run path on purpose, G-15).
Implements `docs/process/issues/014-api-webhook-trigger.md` follow-up: generalize 015's polling
into an event-triggered job type. v2 is stage 2's review: v1 kept, formal `## Acceptance criteria`
/ `## Review notes` / `## Changelog v1→v2` sections added, no functional change.

## Problem

Claude Routines has two out-of-cron entry points — "Call via API" (POST to
trigger a run) and a GitHub-event trigger. Both assume inbound reachability,
which the Pi does not have, and tunnels were already declined (see
`docs/process/issues/014-api-webhook-trigger.md` + the Telegram-relay
decision in agent-orchestrator research `herdr.md`).

The issue narrows the transport question to the one realistic path:

- **GitHub events → poll-based trigger.** A timer checks `gh api` on an
  interval and diffs against last-seen state. No listener, no tunnel.
- **Same-LAN "call via API" (local HTTP endpoint, no tunnel) → explicitly
  split out.** Only build if push-button "run job X now" is actually wanted;
  not part of this spec.

The gate was issue 015 shipping first so this does not design a second
polling path before the first exists. Gate met 2026-09-29: `babysit-prs`
(015) is merged and runs on the Pi (`GhClient` in
`src/herdr_routines/auto_fix.py`, `pr_health` check, per-tick poll with no
agent spawn when clean). This spec is therefore the follow-up the issue
prescribes: **generalize 015's polling into an event-triggered job type**.
Acceptance was "to be written once 015 shipped" — this spec writes it (§5 +
`## Acceptance criteria`).

## Approach

Reuse the gated-job model (`kind: gated`, `checks` gate, `target`, budgets —
see `docs/pipeline/features/gate-trigger-standing-job.md`), adding one new
check kind. No new trigger axis, no daemon, no inbound server.

1. **New check kind: `github_event`.** A poll predicate, same posture as
   `pr_health` (polls, never spawns; fixer stays engine-side):
    ```yaml
    - name: on-label-pr
      kind: gated
      cron: "*/10 * * * *"        # poll cadence, not the event itself
      max_workers_per_tick: 1
      max_attempts_per_target: 3
      checks:
        - github_event:            # builtin, new
            query: prs:labels     # closed set v1: prs:labels | issues:opened | ci:failed
            repo: owner/repo      # default: job repo
            match: {label: "ready-for-agent"}
            timeout_ms: 60000
    ```
   `cron` keeps its existing meaning (how often the tick evaluates); the
   check decides whether the event fired since the last tick.
2. **Poll + diff, not webhook.** Each tick the check runs cheap read-only
   `gh api` queries (via the existing `GhClient` protocol in
   `src/herdr_routines/auto_fix.py`), compares returned event IDs/updated-at
   against last-seen IDs recorded in `history.jsonl` `extra`
   (`last_seen_event_ids`), and passes (no dispatch) when nothing is new.
   Any new matching event → gate failed → dispatch one worker per new event
   (capped by `max_workers_per_tick`), same dispatch path as `pr_health`.
3. **Dedup by event ID.** The worker record carries `extra.event_id`; retry
   budget keys on it (stable key like `pr_number` in pr-target mode), so a
   red event taps out as `skipped/max_attempts_exceeded` + notify instead of
   looping. Re-polls that return the same ID are clean (no-op, no agent).
4. **Out of scope for v1:** inbound HTTP listener of any kind; tunnel;
   same-LAN local endpoint (separate issue if wanted); new `trigger:` axis
   (rejected already in gate-trigger-standing-job §"Why not two trigger
   kinds" — `checks` is the signal, `target` is where the fix lands);
   mixing `github_event` with `pr_health`/`command` in one job (rejected at
   config time, like the existing `pr_health`+`command` mix rejection).
5. **Acceptance (writing the issue's TBD):**
    1. No new event → no agent, no branch, `done` with
       `extra.gate == "passed"`, clean tick costs only the `gh api` reads.
    2. One new matching event → exactly one worker, prompt seeded with the
       event payload, per-attempt record with `reason: gate_failed` +
       `event_id`.
    3. Same event re-polled → no second worker (dedup on `event_id`).
    4. `max_attempts_per_target` exceeded per `event_id` → `skipped` +
       notify, no worker.
    5. `gh api` auth/rate-limit failure → fail-closed as gate error (no
       silent green), surfaced like other gate errors.

## Files touched

- `src/herdr_routines/config.py` — new `github_event` check kind in the
  `checks` schema; closed `query` set; `match` validation (pure schema, no
  git/network); reject `github_event` mixed with `pr_health`/`command`.
- `src/herdr_routines/auto_fix.py` — new poll function reusing the
  `GhClient` protocol (`pr_list`/`graphql` shapes); pure-ish via injected
  client; `build_event_fix_prompt` seeded with event payload.
- `src/herdr_routines/tick.py` — route `github_event` checks through the
  gated path (`_process_gated_job`); last-seen-ID diff; per-event dispatch
  capped by `max_workers_per_tick`; runtime repo/ref checks fail-closed.
- `src/herdr_routines/history.py` — no new file; new `extra` keys
  (`last_seen_event_ids`, `event_id`, `gate: passed|failed`) on existing
  `history.jsonl` records.
- `src/herdr_routines/cli.py` — `_check_systemd_timeout` worst case covers
  event-poll `gh` time + workers (same `gate_slop + Σ check.timeout_ms +
  workers × timeout_ms` form as base/pr targets).
- `tests/test_config.py`, `tests/test_auto_fix.py`, `tests/test_tick.py` —
  validation matrix, poll/diff/dedup unit tests (injected fake `GhClient`),
  tick integration (clean = no agent; new event = one dispatch; re-poll =
  no dispatch; budget taps out).
- `deploy/jobs.example.yaml` — sample `github_event` job documenting poll
  cadence and match syntax.

## Risks

- **`gh api` failures look green.** Auth expiry or rate-limiting at 03:00
  must fail-closed as a gate error, never as "no events" (same rule as
  `commit_check_runs` unverifiable ≠ green in `auto_fix.py`).
- **Duplicate fires.** Poll intervals + catch-up collapse can re-deliver;
  mitigation is `event_id` dedup + budget keyed per event, mirroring the
  pr-number key that already bounds `babysit-prs`.
- **Missed events between polls.** An event opened and closed within one
  poll window may never be seen; accepted v1 limitation (documented) —
  the alternative is inbound delivery, already declined.
- **Poll cost / rate limits.** Each tick costs reads per event job;
  mitigation: keep cadence coarse (`*/10` like babysit), one list query +
  per-new-event detail only, never per-tick worktrees/agents when clean.
- **Scope creep into a server.** Any "just add a small listener" request
  reintroduces the inbound-reachability problem the issue settled;
  mitigation: reject in review, point at the split-out LAN-endpoint issue.
- **Regression against `pr_health`/`command` modes.** Mitigation: new check
  kind is a sibling branch in the gated path; existing gate acceptance
  tests stay green unchanged.

## Acceptance criteria

1. clean tick with no new matching event spawns no agent and creates no branch, records `done` with `extra.gate == "passed"`, and costs only the read-only `gh api` poll — blocking, confidence: high — Test: test_github_event_clean_tick_no_dispatch
2. one new matching event dispatches exactly one worker with the prompt seeded from the event payload and a per-attempt record with `reason: gate_failed` plus `event_id` — blocking, confidence: high — Test: test_github_event_new_event_dispatches_one_worker
3. re-poll returning the same event ID dispatches no second worker and records a clean pass via `event_id` dedup against `last_seen_event_ids` — blocking, confidence: high — Test: test_github_event_repoll_dedups_same_event
4. `max_attempts_per_target` exceeded for an `event_id` records `skipped` plus notify with no further worker dispatch — blocking, confidence: high — Test: test_github_event_budget_exceeded_skips_with_notify
5. `gh api` auth or rate-limit failure fails closed as a gate error rather than a silent green pass and is surfaced like other gate errors — blocking, confidence: high — Test: test_github_event_api_failure_fails_closed
6. config validates the closed `query` set (`prs:labels` | `issues:opened` | `ci:failed`) and `match` shape with no network, and rejects `github_event` mixed with `pr_health`/`command` in one job — blocking, confidence: medium — Test: test_github_event_config_validation
7. `history.jsonl` carries the new `extra` keys (`last_seen_event_ids`, `event_id`, `gate`), the shipped `deploy/jobs.example.yaml` sample validates, and the `cli` systemd-timeout worst case covers poll time plus workers — non-blocking, confidence: medium — Test: test_github_event_history_example_and_timeout_docs

## Review notes

- Tiers follow code-review skill convention: `blocking` findings must be resolved before merge, `non-blocking` are advisory — confidence: high for poll/diff/dedup/budget/fail-closed behaviour, confidence: medium for schema edge cases and history/example/timeout docs.
- Acceptance mapping verified end-to-end for `rg` checks: each acceptance line contains `Test:`, one of `blocking`/`non-blocking`, and `confidence:` — Test: test_github_event_review_tiers_present

## Changelog v1→v2

- Bump spec version v1 → v2 per pipeline stage 2 gate (spec v2 + Acceptance criteria & test plan).
- Added `## Acceptance criteria` with 7 numbered items promoting Approach §5's TBD acceptance (clean = no agent, one event = one worker, re-poll dedup, budget taps out, api failure fail-closed) plus schema and history/docs coverage, each line ending with the required suffix (exact test name) for `rg -c` extraction and `rg -F -q` existence check in `tests/` (gate 3).
- Ensured `blocking`/`non-blocking` and `confidence:` tiers are present on every acceptance line: items 1–6 blocking, confidence: high/medium (poll behaviour, dispatch, dedup, budget, fail-closed, schema); item 7 non-blocking, confidence: medium (history keys, example job, timeout docs) — satisfies `rg -qw "blocking" && rg -qw "non-blocking" && rg -q "confidence:"` (gate 2).
- Added this `## Changelog v1→v2` section inside the same per-run file (`docs/pipeline/runs/20261013T050000Z/spec.md`, G-15) so `rg -q "^## Changelog"` passes without leaving per-run path.
- No functional change to Problem/Approach/Files touched/Risks; v2 is a gate-formatting promotion of v1 to make acceptance criteria machine-checkable.
