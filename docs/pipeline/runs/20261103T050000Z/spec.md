# Spec v1 — Issue 078: Verify gates for event-target fix workers

Run: `20261103T050000Z` · Issue: `docs/process/issues/078-verify-gates-for-event-fix-workers.md` (priority: low, area: scheduler)
Related: 070 (done — PR-path gates), 071 (gate registry), 077 (done — event trigger path exists).

## Problem

Issue 070 proved a PR-target fix worker's `done` with `_FIX_WORKER_GATES`
(`worktree_clean`, `pushed_or_replied` — tick.py:2941, applied by `_verify_fix_worker`
tick.py:2948). Event-target workers (`_process_event_target` tick.py:1236 →
`_dispatch_event_worker` tick.py:1632) got only 070's attempt-budget fix. Two gaps:

1. **`done` is still taken on the agent's word.** A settled `_WorkerSettled` is recorded
   `done` (tick.py:1547-1582) with no worktree push check. A worker that edits files and
   never commits/pushes/OPs is recorded `done`, its event joins the watermark, and the
   work is silently lost.
2. **The worker's own reason is overwritten.** The outcome record hardcodes
   `reason="gate_failed"` (tick.py:1562) for *every* settled/failure path, including
   `_WorkerFailed` outcomes that already carry a specific reason (`agent_start_failed`,
   `worktree_creation_failed`, …) — unlike the PR path, which keeps
   `worker_outcome.reason` (tick.py:2119).

Deferred in 070 because "done" depends on the event query: `issues:opened` has no head
branch, a label event may not require a push.

## Approach

Per query in the closed set `{prs:labels, issues:opened, ci:failed}` (config.py:282),
define what "done" means and map it to gates:

| Query | Shape | Gates | Required action |
| --- | --- | --- | --- |
| `prs:labels`, `ci:failed` | PR event, `head_ref` set, `is_issue=False` | `worktree_clean` + `pushed_or_replied` (existing, 070) | Remote head moved since dispatch, or reply on each given thread (none given → must push) |
| `issues:opened` | Issue, no head branch | `worktree_clean` + **new gate** `commented_or_linked` | New comment on the issue by a trusted author since run start, **or** a PR linked to/created against the issue since run start (the prompt for issue events tells the worker to branch and open a PR — either proves the work shipped) |

Gateway details:

- **Plumbing:** `_dispatch_event_worker` must return `worktree_path=str(wt_path)` on
  `_WorkerSettled` (the PR version already does, tick.py:2934) so `worktree_clean`
  reads the right tree. For `dispatch_head_sha`: `enrich_event_detail`
  (auto_fix.py:1669) already calls `gh.pr_view`, which returns `headRefOid` — capture it
  into `GithubEvent` (new `head_sha: str | None` field) and pass it as
  `dispatch_head_sha`. Missing SHA → the gate fails as `worker_verification_failed` (its
  existing behaviour, verify.py:227), never crashes.
- **New gate** `commented_or_linked` in `verify.py`: registered in `VERIFY_GATES`
  (verify.py:285), added to `VerifyGate`/`VALID_VERIFY_GATES` and the non-listable set
  in config.py:105-117 (needs a gh probe; a job cannot list it under `verify:`). Reads
  the issue's comment list and linked-PR state since `started_at` (whole-second
  truncation, same as 070's other probes). Probe failure = failed gate, never a pass.
- **Gate application in `_process_event_target`:** mirror the PR path (tick.py:2091-2117):
  only when the worker settles `done`, run the per-query gate set via
  `run_verify_gates`; first failed gate → record `failed` with the gate's reason
  (`worker_left_uncommitted_changes` / `worker_pushed_nothing` /
  `worker_verification_failed` etc.), keep `verify` results in `extra`. A
  gate-failure of `worker_left_uncommitted_changes` joins the dirty-worktree
  first-detection report/notification the same way the PR path does. A gated-failed
  event is *not* added to the `handled` watermark, so the next tick retries within
  `max_attempts_per_target` (the state is already `failed`, so
  `attempt_count_for_event` counts it — matches 070).
- **Reason preservation (AC2):** replace the hardcoded `reason="gate_failed"` with:
  gate reason for gate failures, `worker_outcome.reason` for `_WorkerFailed` outcomes,
  none for clean done — plus the `error` field as the PR path does (tick.py:2134-2137).
- **Job-shaped events from custom prompts:** a job overriding `prompt:` may not commit/push
  (e.g. comment triage). Gates still apply — a prompt that cannot pass its query's gate
  is a misconfiguration, consistent with 070's stance that prompts never stand in for
  the code check (070 AC4).

## Files touched

- `src/herdr_routines/tick.py` — `_process_event_target` (gate application, reason
  preservation, dirty-tree surfacing); `_dispatch_event_worker` (return
  `worktree_path`, accept/pass dispatch head SHA); small helper mapping query → gate
  tuple (e.g. `_EVENT_WORKER_GATES` next to `_FIX_WORKER_GATES`, tick.py:2941).
- `src/herdr_routines/verify.py` — new `commented_or_linked` gate + registration.
- `src/herdr_routines/auto_fix.py` — `GithubEvent.head_sha` field; capture `headRefOid`
  in `enrich_event_detail`.
- `src/herdr_routines/config.py` — extend `VerifyGate` literal + non-listable gate set
  with `commented_or_linked`.
- `tests/test_github_event.py` — the event-path tests live here: add
  `test_event_fix_worker_done_without_effect_recorded_failed` (AC1) and
  `test_event_worker_failure_reason_preserved` (AC2), plus a `prs:labels` done case
  that passes `pushed_or_replied` and an `issues:opened` case passing via linked PR and
  via comment.
- `tests/test_verify.py` — unit tests for `commented_or_linked` (pass/fail/probe-fail),
  following `test_pushed_or_replied_gate` (test_verify.py:473).
- `tests/test_config.py` — registry/literal pin test updates for the new gate name
  (non-listable set).

## Acceptance criteria (from the issue)

1. [blocking] An event worker that settles `done` without its query's required action is
   recorded `failed` with the gate's reason.
   `test_event_fix_worker_done_without_effect_recorded_failed`
2. [blocking] A failing event worker's record keeps its own reason.
   `test_event_worker_failure_reason_preserved`

## Risks

- **False `done`→`failed` flips on `issues:opened`:** the new gate's linked-PR probe is
  GitHub-shape-dependent (comments vs timeline vs search). Mitigate: accept either
  signal (comment *or* linked PR), pin the accepted shapes in tests against the real
  query shapes already used by `poll_github_event`/`enrich_event_detail`.
- **Whole-second timestamps: a comment posted in the run's first second** could compare
  earlier than `started_at` — 070 handles this with `replace(microsecond=0)`; reuse it.
- **Missing dispatch SHA burns an attempt:** if `pr_view` fails during enrichment the
  event dispatches SHA-less and `pushed_or_replied` fails as
  `worker_verification_failed` *after* an agent run — one wasted attempt per probe
  outage, retried within budget. Accepted for v1 (mirror of 070's rule that a check
  that could not run is a failed attempt); do not skip dispatch silently instead.
- **Custom-prompt jobs:** a job whose prompt legitimately cannot push (comment-only
  triage on a PR event) will now fail `pushed_or_replied`. Deliberate: 070's fidelity
  over convenience; per-job gate overrides are out of scope.
- **Scope guard:** no changes to `_process_pr_target`, the watermark semantics, or the
  attempt-budget accounting beyond what the state flip implies; 077's trigger path is
  already landed and is used as-is (`_fire_triggers` → `_process_event_target`).

## Verification

`uv run pytest tests/test_github_event.py tests/test_verify.py tests/test_config.py -q`,
then `uv run pytest -q` + `uv run ruff check .` + `uv run ruff format --check .` green.
