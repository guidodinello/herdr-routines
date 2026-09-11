# Spec — Approval path for blocked runs (007) — 20260911T000339Z

Per-run spec at `docs/pipeline/runs/20260911T000339Z/spec.md` (G-15). Implements `docs/process/issues/007-approval-path-blocked-runs.md`.

## Problem

A routine job that hits an agent permission prompt settles `blocked` (`runner.py:788`, `herdr.py:45` `AGENT_STATUS_BLOCKED`). Today `execute_run` records `failed`/`blocked` with a diagnostic tail (`runner.py:795` `_capture_visible_tail` via `agent_read_visible`) and deliberately leaves the pane open for manual SSH inspection (`runner.py:788-798`, `herdr.py:57-66` STICKY), and `tick.py:1445` maps it to a terminal `failed (blocked)` record + one `herdr notification show --sound request` gated as `failure` (issue 009 — always sent).

Two gaps remain:

1. **No actionable phone notification.** The blocked `failure` notification's body is just the reason string (`outcome.reason or "unknown"` at `tick.py:1449`). It does not surface the job identity + the permission prompt text, and there is no guarantee of exactly-one actionable ping. The `herdr-telegram-bridge` (`docs/process/issues/023-*.md`) already forwards `pane.agent_status_changed` `blocked` transitions to Telegram and supports reply-to-steer, but `execute_run` moved to pane-lifecycle v2 eager close on settle (`runner.py:854-856`, `herdr.py` `SETTLED_AGENT_STATUSES` deliberately excludes `blocked`). Issue 023's log explicitly flags the race: an eagerly-closed-then-reconciled pane hook is unreliable across real overnight runs. The explicit `notification show` fallback is the reliable channel, and it is not yet actionable.

2. **No wired phone-approval path for a scheduled run.** The bridge's reply-to-steer already delivers freeform text to the live pane (installed 2026-08-25, `herdr-push` verified working per `docs/plan-v1.md:224` and later replaced by the bridge). What is missing is recording and verifying that a blocked routine run can be unblocked from that path without an SSH session — i.e. that replying to the actionable notification actually feeds the waiting prompt and lets the agent settle to `idle`/`done`, and that the tick/history view reflects it. The alternative — adding a `permission_mode` / `--dangerously-skip-permissions` auto-approve escape hatch — is deliberately not in scope (`docs/plan-v1.md:218-228`, `docs/pipeline/spec.md` non-goals): scheduled + unattended + auto-approve turns one bad prompt into an unreviewable repo mutation, and worktree isolation does not contain it (object store + push). Revisit auto-approve only if this phone path proves too slow.

## Approach

Wire the existing `blocked` settle into one actionable, exactly-once notification that reuses the installed `herdr-telegram-bridge` (and the legacy `herdr-push` name as the same notification path) and verify the reply-to-steer unblock. No new config key, no auto-approve mode.

1. **Single actionable notification off the terminal record (tick-owned, not pane-hook).**
   - Keep `runner.py:788-798` leaving the `blocked` pane open and writing its visible tail (`agent_read_visible`, already on the failure path). Extract a short prompt excerpt from that tail for the notification body — best-effort, truncated (e.g. 300 chars), never failing the run if parsing fails.
   - `tick.py:_process_job` after `execute_run` is the exactly-once site (one terminal `HistoryRecord` per `run_id`; `blocked` is `failed` with `reason=blocked` at `tick.py:1425`). Replace the generic `body=outcome.reason` (`tick.py:1449`) with an actionable body for `reason == "blocked"` only: `job=<name> run=<run_id> pane=<pane_id> agent=<agent_name>` + prompt excerpt + one-line approval hint (`Reply to this message to approve — e.g. "yes" / "approve"`). Title stays `herdr-routines: <job> blocked` so the bridge's Telegram forward is human-scannable (bridge forwards title+body). Gate as `failure` kind via `_notify_gate` — so `terminal`/`on-finding`/`on-failure` all still notify (issue 009 hierarchy), never suppressed; `progress` irrelevant.
   - Exactly-once guarantee: one `HistoryRecord` with `state=failed reason=blocked` per `run_id` → one `_notify` call on that branch. No per-poll loop, no retry. If `notification_show` fails (`_notify` already best-effort at `tick.py:1870`), the terminal record remains; next tick does not re-notify for the same `run_id` because `decide`/`last_terminal_run` advances past it.
   - Pane-hook race avoided: do not rely on `pane.agent_status_changed` for the actionable content; the explicit `notification show` is the contract the bridge also forwards (issue 023 log fallback). The pane staying live (`STICKY_AGENT_STATUSES`, `issue 051` reap only on `agent_name_taken`) preserves the steer target.

2. **Phone approval = bridge reply-to-steer into the live blocked pane (no new code path).**
   - The bridge's existing reply-to-steer delivers the Telegram reply as keystrokes/text to the pane that produced the notification (verified installed 2026-08-25). The routine's agent, still `blocked` with pane open, consumes the steering text as its approval and settles (`idle`/`done` or next poll). Tick does not need to poll the approval itself; the next `herdr agent get` for that pane will show the transition. Document that `herdr-push` is the same logical path (notification → phone → reply) — feature accepts either transport, implementation wires one title/body contract both forward.
   - No new `herdr` CLI verb (`agent approve`) — if Herdr ever adds one, it would be an adapter-only addition in `herdr.py`, but v1 treats steer as the delivery mechanism and does not invent a wrapper. Validate against live `herdr 0.8.2` that a steered approval actually transitions a `blocked` agent (manual probe: start agent, trigger permission, reply via bridge, observe `agent get`).
   - Post-approval history: the blocked `failed` record is terminal; approval does not rewrite it. The next tick either starts the next scheduled occurrence (normal cron) or, if the operator wants immediate continuation, `herdr-routines run <job>` / the bridge's cold-start (`issue 024`) — out of scope for this run's `blocked` record.

3. **Explicit non-goal: no auto-approve.**
   - Do not add `permission_mode`, `allow_dangerous`, or any `agent start --dangerously-skip-permissions`-style flag to `config.py:Job` or `herdr.py:build_agent_start_args`. `config.py` validation should reject such a key if ever introduced elsewhere (add a negative test). Rationale cited in `docs/plan-v1.md:218-228`.

## Files touched

- `docs/pipeline/runs/20260911T000339Z/spec.md` — this file (per-run spec, G-15).
- `src/herdr_routines/runner.py:788-798` — blocked branch: keep pane open + `_capture_visible_tail` as today; optionally extract prompt excerpt helper (pure, testable) for notification body. No change to `STICKY`/`SETTLED` semantics.
- `src/herdr_routines/tick.py:1445-1452` — blocked-aware `_notify` body: when `outcome.reason == "blocked"` build actionable title/body (job, run_id, pane_id, prompt excerpt) and gate as `failure`. Keep `herdr.py` adapter call `client.notification_show` (`tick.py:1870`/_notify) — no new transport code; the bridge forwards `herdr notification show` already.
- `src/herdr_routines/herdr.py` — no new method required for v1; if a prompt-excerpt parser is reused across tests, keep it in `runner.py` (pure). Any future `agent steer` wrapper would live here, out of scope.
- `tests/test_runner.py` / `tests/test_tick.py` — blocked-notification assertions on a fake `HerdrClient`: exactly one `notification_show` per blocked settle, body contains job + prompt excerpt, gated as `failure` under `terminal`/`on-finding`/`on-failure`; no `permission_mode` key accepted.

Not touched: `config.py` schema (no `permission_mode`), `history.jsonl` format, `schedule.py`, `gated`/`pipeline` dispatch, `herdr.py` `LIVE`/`STICKY` sets, `deploy/systemd` units.

## Risks

- **Exactly-once vs pane-hook double-ping.** The bridge's `pane.agent_status_changed` hook can emit a second Telegram message for the same `blocked` transition alongside the explicit `notification show`. The spec avoids deduplicating in the bridge; instead the actionable contract is the explicit `notification show` title/body (the hook's message is at most duplicate, never the sole actionable one). If duplication proves noisy, follow-up is a bridge-config suppress of the pane hook for `rt-*`, not code in this repo. Confidence: high — tick's one-record→one-notify is already single-site.

- **Prompt excerpt brittleness.** Permission prompts differ by `agent_kind` (claude vs opencode) and by tool. Extraction from the visible tail is heuristic and may capture the wrong line or be truncated by the alternate-screen limit (`docs/plan-v1.md:93`). Mitigation: body always includes `job/run_id/pane_id/agent` even when excerpt is empty; extraction never fails the run; keep markers agent-agnostic and truncate to a fixed width.

- **Reply-to-steer shape mismatch.** A `blocked` prompt may expect `y`/`yes` vs `approve` vs an enter key. The notification hint suggests a generic `yes`/`approve`; the real shape is whatever the agent's permission UI accepts when steered. Verify live against both `claude` and `opencode` blocked prompts; if Herdr exposes a typed approval endpoint, add `herdr.py` adapter coverage there. Confidence: medium — bridge steer is verified for general input, not specifically for permission approval.

- **Auto-approve creep.** Adding `permission_mode = "auto"` (as shepherd does, `docs/plan-v1.md:566`) is the path of least resistance for "fixes blocked quickly." This spec deliberately rejects it; the review gate must enforce that no `skip-permissions` flag or `build_agent_start_args` bypass is introduced. Add a negative config test. Confidence: high — `config.py` is the single gate.

- **Pane-leak / wedge interaction (issue 051).** A `blocked` pane deliberately stays live and sticky (`herdr.py:64-66`). If never approved, `sticky_agent_pane` will force-close it only on the next run's `agent_name_taken` collision (`runner.py:645`), which is the intended GC. The actionable notification must therefore be reliable enough that an operator sees it before the next scheduled occurrence wedges. Confidence: high — existing reap behavior already handles the unapproved case.

- **Notification gating suppression.** Issue 009's `on-failure` vs `terminal` distinction could suppress a `blocked` ping if misclassified as `finding`/`success`. This spec pins blocked as `failure`, which is notified under every policy (`_notify_gate` at `tick.py:1881`). Pin with a test matrix over all four policies. Confidence: high.

## Acceptance criteria

1. Blocked settle emits one actionable notification with job + prompt — a `failed`/`blocked` run triggers exactly one `herdr notification show` whose body identifies the job (and run/pane) and includes the permission prompt excerpt. Test: test_blocked_emits_single_actionable_notification
2. Phone reply unblocks without SSH — replying to the actionable notification via the bridge (or herdr-push path) steers the live blocked pane and the agent settles without a manual SSH session; verified against live Herdr. Test: test_blocked_steer_unblocks_agent (manual/live probe)
3. No auto-approve mode added — config rejects `permission_mode`/`skip-permissions` and `build_agent_start_args` never passes a bypass flag. Test: test_no_auto_approve_mode
