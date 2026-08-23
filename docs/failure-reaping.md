# Failure reaping & quota-exhaustion handling

Status: proposed (phase 1 approved 2026-08-23; phase 2 designed, not scheduled)
Related: [plan-v1.md](plan-v1.md) §4 (state machine), §6 (report contract), §7 (test tiers)

## 1. Problem

Two incidents (2026-08-22, 2026-08-23), same shape. An opencode/big-pickle agent hits
OpenCode's free-tier limit ("Free usage exceeded, subscribe to Go"), renders a modal, and sits
in an API-retry loop — it never settles. Consequences, in order:

1. `runner._prompt_with_retry` blocks inside `herdr agent prompt --wait` until `timeout_ms`
   elapses (90 min for fitted-implementer, 60 min for fitted-pr-review), then raises
   (`error.code == "timeout"` — deliberately non-retryable).
2. `execute_run` returns `RunOutcome(state="failed", reason="agent_prompt_failed")`
   **leaving the working agent and its pane/workspace alive** (runner.py, prompt-failed path).
3. Because ticks run sequentially under the flock (plan-v1 §3), the next job in the same tick
   starts up to one full timeout late.
4. Every subsequent tick skips the job forever: `_live_agent_exists` sees
   `agent_status == "working"` ∈ LIVE_AGENT_STATUSES → `skipped (agent_name_live)`
   (tick.py). Nothing self-heals: the start-of-run stale-pane reap only touches *settled*
   agents (SETTLED_AGENT_STATUSES), by design.
5. No diagnostic tail is written on any failure path (the `.tail.txt` write lives after the
   settle-success check), so postmortem requires manual `herdr agent read` against a pane that
   the wedge itself encourages you to delete.

Manual recovery (2026-08-23): `herdr agent send-keys <name> esc`, then close the run's
workspaces. Worktrees were clean — the agents died before doing any work — but nothing in the
toolchain made that discoverable.

## 2. Goals / non-goals

Goals (phase 1, this spec):

- G1 — a failed run never leaves its agent or pane behind; every post-start failure path
  deterministically reaps the pane it created.
- G2 — quota exhaustion is classified as a first-class failure reason
  (`quota_exhausted`), visible in history.jsonl, notifications, and reports.
- G3 — failure paths leave diagnostic evidence (visible-screen tail) before reaping.
- G4 — zero config changes required on the Pi (`jobs.yaml` stays valid as-is).

Non-goals (deferred, see §8 and the Parking Lot):

- Mid-run fast-fail watchdog (phase 2).
- Automatic provider/model switching on quota exhaustion.
- Reaping `blocked` or `interrupted_unknown` runs (both deliberate human-follow-up states,
  plan-v1 §4; herdr.py SETTLED_AGENT_STATUSES comments).
- Same-day retry after quota failure (catch_up_minutes governs missed *schedules*, not failed
  terminal runs; a rolling-window quota makes same-day retries near-certain to re-fail).

## 3. Design

### 3.1 Reap-on-failure (G1)

New private helper in `runner.py`:

```python
def _reap_failed_run_pane(client, *, job_name, pane_id) -> None
```

Best-effort, never raises (mirrors the existing stale-reap try/except at the top of
`execute_run`): logs a warning on any exception. Calls `client.pane_close(pane_id)` — the pane
was created by this very run, so closing it is ours to do; sibling tabs/workspaces are
untouched (same reasoning as the existing single-pane stale reap).

Call sites — the reap fires on every `RunOutcome` returned *after* `agent_start` succeeded,
except the two preserved states:

| Outcome path                         | reason                                         | Reap? | Rationale                                                                                   |
| ------------------------------------ | ---------------------------------------------- | ----- | ------------------------------------------------------------------------------------------- |
| `agent_start_failed`                 | agent_start_failed                             | yes   | our pane, run dead; also stops leaking empty workspaces                                      |
| readiness poll exhausted             | agent_not_interactive                          | yes   | half-started agent in our pane                                                               |
| prompt raise (incl. settle-timeout)  | agent_prompt_failed / quota_exhausted          | yes   | the wedge case                                                                               |
| settled `blocked`                    | blocked                                        | **no** | answerable from bed via herdr-push (ROADMAP Next)                                            |
| settled `unknown`                    | unsettled_status_unknown → interrupted_unknown | **no** | evidence preservation (plan-v1 §4 bucket)                                                    |
| settled other (defensive)            | unsettled_status_<status>                      | yes   | herdr claims it is still working → would wedge                                               |
| `no_report` / `done`                 | —                                              | no    | existing retention policy unchanged (ROADMAP Next: retention policy)                         |

Ordering: tail capture (§3.3) happens *before* the close, obviously; the close happens before
returning the outcome so even an immediately-following tick sees no live agent.

### 3.2 Quota classification (G2)

After the prompt-failed catch (and before reaping), read the screen once via the new
`agent_read_visible` (§3.3) and scan for failure markers:

- New module constant in `runner.py`:
  `DEFAULT_FAILURE_MARKERS = ("Free usage exceeded",)`
- Effective markers: `job.failure_markers` (config, §3.4) falling back to the default tuple.
- On match: `reason="quota_exhausted"`, `error=f"failure marker matched: {marker!r}"`.
  Otherwise reason stays `agent_prompt_failed` with the existing error string.

False-positive guard (important — the visible screen contains the *echo* of our own prompt,
which is how the modal was observed interleaved with prompt text on 08-23): a marker that
appears verbatim in the job's substituted prompt is skipped for that scan
(`if marker in prompt_text: continue`). Documented in the schema notes: don't author prompts
containing marker phrases.

Classification applies to the `agent_prompt_failed` path only. It intentionally does *not*
inspect screens on healthy runs — no polling, no extra reads on the success path.

### 3.3 Visible-screen reader + failure tails (G3)

`herdr.HerdrClient.agent_read_visible(target, *, lines=200) -> str` — identical contract to
the existing `agent_read` (raw runner call, `""` on any failure) but `--source visible`:
`recent-unwrapped` is rejected by herdr while an agent is unsettled (`agent_not_idle`,
observed live 2026-08-23), and failure-path agents are definitionally unsettled.

In `runner.py`, a best-effort `_capture_visible_tail(...)` reads the screen through it, writes
`{run_id}.tail.txt` next to the report when non-empty (OSError-swallowed), and returns the
text so §3.2 can scan without a second read. The success path keeps its existing
`recent-unwrapped` tail write untouched — settled agents can read scrollback, and their
behavior is already verified.

### 3.4 Config: `failure_markers` (G2/G4)

- `Job.failure_markers: tuple[str, ...] | None` — default `None` (= use
  DEFAULT_FAILURE_MARKERS).
- Added to `_DEFAULTS_ALLOWED_KEYS`, `_JOB_ALLOWED_KEYS`, `_JOB_DEFAULTS`; validated as a
  list of non-empty strings when present.
- `jobs.yaml` on the Pi needs no change; the key exists so a future job can carry
  provider-specific markers (and so the §8 provider-switch hook has somewhere to hang).
- Schema documented in plan-v1.md §2's annotated example and `deploy/jobs.example.yaml`.

## 4. Files touched (phase 1)

- `src/herdr_routines/herdr.py` — add `agent_read_visible`.
- `src/herdr_routines/runner.py` — `_reap_failed_run_pane`, `_capture_visible_tail`,
  `_matched_failure_marker`, DEFAULT_FAILURE_MARKERS, reap call sites, marker-skip guard.
- `src/herdr_routines/config.py` — `failure_markers` plumbing/validation.
- `tests/test_herdr.py` — argv assertion for `--source visible`; ""-on-error contract.
- `tests/test_runner.py` — reap matrix (one test per row of §3.1's table, including the two
  negative rows); tail-before-close ordering; close-failure-swallowed (never-raises);
  quota classification incl. marker-in-prompt skip; regression test replaying the 08-23 shape
  (settle timeout + quota screen → pane_close called exactly once, nothing after it);
  ScriptedClient extended with `agent_read_visible` + a scripted screen.
- `tests/test_config.py` — `failure_markers` validation (absent → None; bad types rejected;
  per-job override beats defaults).
- `docs/failure-reaping.md` — this file.
- `docs/plan-v1.md` §2 — annotated-schema line for the new key.
- `deploy/jobs.example.yaml` — commented example of the new key.

No changes: `tick.py` (skip logic becomes correct once runs stop leaking agents),
`history.py`, `schedule.py`, `cli.py`, systemd units (TimeoutStartSec=9600 unchanged),
README (has no key-level config reference; plan-v1 §2 is the schema home).

## 5. Edge cases

- **Close race** — the agent settles between screen-read and `pane_close`: closing a settled
  own-run pane is still correct (the old stale-reap would do exactly that next run).
- **pane_close itself fails** — warning logged, outcome unaffected; worst case degrades to
  today's behavior instead of improving on it. Manual recipe stays in §7.
- **Server down at tail/reap time** — HerdrCliError is swallowed inside
  `agent_read_visible` (returns "") and the best-effort reap wrapper respectively; the
  failure record still lands (execute_run's never-raises contract, plan-v1 §4).
- **Marker in prompt** (see §3.2) — that marker is inert for the run; remaining markers still
  apply. With the default set this requires authoring a prompt containing "Free usage
  exceeded", which the schema notes warn against.
- **quota_exhausted is a plain failure** — notification (sound=request), non-zero tick exit,
  systemd marks the unit failed, same as every failed run. No special-casing.

## 6. Testing notes (per plan-v1 §7 tiers)

Tier-2 fakes assert exact argv (`["herdr","agent","read",<name>,"--source","visible",
"--lines","200"]`). Tier-3 runner tests use the existing FakeHerdrClient pattern, extended
with a scripted screen-read return so classification tests are deterministic. The regression
test pins the incident: prompt → HerdrCliError(code=timeout) → outcome failed/quota-exhausted
→ `pane_close` called once → no further `agent` calls after the close.

## 7. Rollout & ops

1. PR from `failure-reaping`, stacked on `roadmap-now-next-later` (#24 carries the roadmap
   structure this spec references); squash-merge per repo ruleset. Because #24 merges by
   squash, this PR opens against the #24 branch and must be rebased onto `main` after #24
   lands (standard stacked-on-squash dance).
2. Pi deploy: `git -C ~/projects/herdr-routines pull --ff-only && uv sync && uv run
   herdr-routines validate` (validate re-checks jobs.yaml against the unit's TimeoutStartSec).
3. Evidence: one real overnight cycle; history.jsonl should show terminal records with
   `reason` populated and no `skipped (agent_name_live)` streaks the morning after.
4. Manual recovery recipe (pre-phase-2, kept for reference): `herdr agent list` →
   `herdr agent send-keys <name> esc` → `herdr workspace close <id>`; worktrees under
   `~/.herdr/worktrees/<repo>/auto-*` are safe to remove via `git worktree remove` from the
   primary clone when their branches hold nothing unmerged.

## 8. Phase 2 sketch (built only if the dead wait hurts in practice)

Replace the blocking `subprocess.run` inside `agent_prompt_wait` with Popen + a poll loop
(~30 s) over `agent_read_visible` + marker scan; on match, terminate the child, reap, return
`quota_exhausted` within minutes instead of at `timeout_ms`. Cost: the wait loop stops being a
single auditable syscall-shaped call (the double-prompt safety analysis in
`_is_retryable_prompt_error` gets harder to keep provably correct). Trigger to build: a
phase-1 deployment where a quota wedge again delays a subsequent job past tolerance.

Provider/model switching on quota exhaustion lives in the ROADMAP Parking Lot (gate: quota
failures recurring after phase 1 ships).

## Appendix: 2026-08-23 incident timeline

- 05:30:14 fitted-implementer starts; prompt attempt 2/3 at 05:30:28 (early EmptyResponse
  retry); blocks on settle-wait against the quota modal.
- 07:00:28 implementer fails `agent_prompt_failed` (90-min timeout). PID survives.
  Same tick: fitted-pr-review reaps yesterday's *settled* pane (wC:p1) — the mechanism working
  exactly as designed, and exactly not far enough — starts, retries at 07:00:42, hits the same
  modal.
- 08:00:42 pr-review fails `agent_prompt_failed` (60-min timeout). PID survives.
- 08:00→17:20 every five minutes: both jobs `skipped (agent_name_live)` (~110 skipped ticks).
- 17:26 manual recovery (esc ×2, close wE/wF); worktrees clean; ticks return to `not due`.
