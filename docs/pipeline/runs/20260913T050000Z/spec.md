# Spec — Pane/session retention policy (011) — 20260913T050000Z

Implements `docs/process/issues/011-pane-session-retention-policy.md`. Per-run spec at `docs/pipeline/runs/20260913T050000Z/spec.md` (G-15).

## Problem

Pane cleanup today is half-done and inconsistent:

1. **Success path captures nothing useful.** `src/herdr_routines/runner.py:267` `_capture_visible_tail()` (200-line `agent_read --source visible`) is called from every failure branch (`agent_start_failed`, `agent_not_interactive`, `agent_prompt_failed`/`quota_exhausted`, `unsettled_status_*`, and since issue 037 the pipeline launcher's `blocked` settle). A run that settles `idle`/`done` writes its `$ROUTINE_REPORT` (`runner.py:894-899` `RunOutcome(state="done")`), captures `session_id` (`runner.py:884` `_capture_session_id`), and closes its pane (`runner.py:885` `_close_run_pane`) with **no transcript persisted**. The run you most want to read after the fact — the one that worked, whose report you want to check against what the agent actually did — leaves no record beyond the self-written report.

   The post-prompt `agent_read --source recent-unwrapped --lines 200` at `runner.py:794-799` does run on the success path, but it is the alternate-screen-unreliable path (`docs/plan-v1.md:93-96` — rows that scrolled away never enter Herdr scrollback). For the success case where the final screen is just a summary tail, it is not a deliberate choice and is not documented as the transcript policy.

2. **"Transcript" vs "visible tail" is undecided.** `_capture_visible_tail` reads the last screenful (`--source visible`), not the session. Correct for failure (you want what it was stuck on), close to useless for success (tail of a summary). The issue explicitly requires a deliberate decision: bounded screen read, fuller scrollback read, or nothing — not just calling the existing helper on one more branch.

3. **No documented retention window.** The original open decision (immediate vs keep-for-a-week vs manual) still spans two artifact families: `reports/{run_id}.tail.txt` and `reports/{run_id}.md`. `docs/process/issues/021-log-rotation.md` owns the pruning *mechanism*; this issue must settle the *policy* and let 021 implement it, consistently between routine jobs (`runner.py:execute_run` / `tick.py:_process_job` / `tick.py:_process_base_target` / `tick.py:_dispatch_fix_worker`) and pipeline workers (`docs/pipeline/design.md` G-16 per-stage close, `scripts/pipeline-launch.sh:139`).

`execute_run` already closes its own pane on every settled terminal path and captures `session_id` first (PR #42, `runner.py:313` `_close_run_pane`, `runner.py:327` `_capture_session_id`, `history.jsonl:RunOutcome.session_id`) so a human can `herdr agent start ... -s <session_id>` resume-and-inspect. What is missing is the **bounded transcript capture to the run-history artifact before that close on the success path**, and a **stated retention window** shared by both families.

Do NOT widen into "capture everything always" — the bound must be stated. A 108 MB `.venv` per worktree is already the dominant on-disk cost (issue 044); an unbounded per-run transcript would be a second one.

## Approach

Three deliberate decisions, one ordering invariant, one doc artifact.

### 1. Success-path capture: bounded visible tail, same bound as failure (decision)

Capture a **bounded visible tail** on `idle`/`done` success, before pane close, with the **same bound as failure paths**: `200` lines via `agent_read --source visible` (same primitive as `_capture_visible_tail`). Write to `reports/{run_id}.tail.txt` (same file as failure diagnostic, `runner.py:267`).

- Why visible, not fuller scrollback: after settle the agent is `idle`/`done` so `recent-unwrapped` is no longer rejected (`herdr.py:408` `agent_read_visible` vs `herdr.py:395` `agent_read`), but the alternate-screen caveat means scrollback is still unreliable — "full transcript" is not recoverable via `agent read` for Claude/OpenCode TUI agents (`docs/plan-v1.md:93`). Promising a scrollback transcript would be an unbounded-fidelity lie. A bounded visible read is honest, cheap, and consistent with the failure-path contract reviewers already understand.
- Why not nothing: the success tail still has diagnostic value (prompt echo, final file list, last error line before the agent summarized). With a stated bound it costs ~15–20 KB per run and closes the "no record beyond the report" gap.
- Bound stated: **200 lines, no truncation beyond Herdr's own line wrapping** (`--lines 200`). Same as `runner.py:275` and `runner.py:797` and `herdr.py:408` default. Documented as the canonical tail bound for both success and failure in this spec. Future tuning requires a spec change, not a silent constant drift.

Placement in `runner.py:execute_run`: insert the capture immediately before `_capture_session_id` / `_close_run_pane` in the success arm (`runner.py:882-899`, the `if not report_written` gate and the final `return RunOutcome(state="done")`). The existing `runner.py:794-799` `agent_read recent-unwrapped` block is replaced/consolidated so success produces **exactly one** `reports/{run_id}.tail.txt` write via `agent_read_visible --lines 200` (no dual-file confusion, no recent-unwrapped leftover). The nudge path (`runner.py:858-875` `_attempt_report_nudge`) stays before this capture so the tail reflects the nudge's final screen if the nudge ran.

For pipeline workers: same bound, same file (`reports/{run_id}.tail.txt` or `reports/pipeline-{run_id}.tail.txt` sibling to `reports/pipeline-{run_id}.md`), same ordering — capture tail before `pane_close` in `tick.py:_process_base_target` (`tick.py:896-906`) and `tick.py:_dispatch_fix_worker` (`tick.py:1199-1216`) and `scripts/pipeline-launch.sh:139`, mirroring `runner.py`. Per-stage G-16 close (`docs/pipeline/design.md:191-196`) already does capture-before-close for pipeline stage workers; make it explicit that the policy is identical.

### 2. Ordering invariant: capture → session_id → close (never reordered)

Every settled terminal path that closes a pane (failure, blocked-not-applicable, success) must obey: (1) capture tail to `reports/{run_id}.tail.txt` (best-effort, never raises, `visible --lines 200`), (2) capture `session_id` via `client.agent_session_id` (before close, since a closed pane's agent record won't answer `agent get` — `herdr.py:373`), (3) `pane_close`. The pipeline path adds (0) mirror `state.json`/`$PIPELINE_REPORT` before close (existing G-16 order). No pane is closed before its tail is on disk. Two deliberate exceptions: `blocked` (`runner.py:817-827`) captures tail but **skips close and session_id** (needs-human, pane stays open); `workspace == "root"` jobs never close a pane and never capture `session_id` (`runner.py:883` guard) — retention doc states both exceptions explicitly.

### 3. Retention policy (documented, consistent — mechanism deferred to 021)

Document in new `docs/process/pane-retention.md` (canonical) and reference from `docs/pipeline/design.md:191` (G-16) and `docs/plan-v1.md:430` layer 2 note:

- **Panes/sessions:** closed **immediately** after capture on every settled run that owns a pane (worktree mode, `workspace != "root"`), for both success and terminal failure. No week-long keep. A human resumes via `session_id` (`history.jsonl` + `state.json:agent_session.value`), not via a lingering pane. Exceptions: `blocked` pane stays open (needs-human); `root` workspace never closes (ambient workspace). Pipeline workers: same — per-stage close on gate-pass (G-16), orchestrator close at end, mirroring routine jobs. This is the "when a finished run's pane/session is cleaned up" answer: **on settle, after tail capture.**
- **Artifacts (`reports/{run_id}.md`, `reports/{run_id}.tail.txt`, `reports/pipeline-*.md` + sibling `.tail.txt`):** retained **14 days** by default (aligns with `src/herdr_routines/gc.py:25` `DEFAULT_OLDER_THAN_DAYS = 14` for `auto/*` branches). Pruning is explicit (`herdr-routines gc` / `021` rotation command), never automatic without opt-in — matches `docs/process/issues/021-log-rotation.md` acceptance ("nothing is deleted automatically without opt-in"). Tails and reports share the same window — one policy, not two. `history.jsonl` itself is append-only and small; rotation (if any) follows 021's size/age threshold and remains transparent to `history`/`ps`/`scheduled`.
- **Session-id retention:** `session_id` lives in `history.jsonl` / `state.json` indefinitely (a short string, not a transcript). It is the durable handle after the pane is gone.

Single PR, half-day. No new Herdr API, no new systemd unit.

## Files touched

- `docs/pipeline/runs/20260913T050000Z/spec.md` — this file (per-run spec, G-15).
- `src/herdr_routines/runner.py:267` `_capture_visible_tail`, `src/herdr_routines/runner.py:794-799` best-effort tail block, `src/herdr_routines/runner.py:882-899` success arm (pane-lifecycle v2) — add bounded visible-tail capture on `idle`/`done` before `_capture_session_id`/`_close_run_pane`; reconcile duplicate tail write so success produces exactly one `{run_id}.tail.txt`.
- `src/herdr_routines/herdr.py:408` `agent_read_visible` / `src/herdr_routines/herdr.py:395` `agent_read` — no API change; document why visible is the canonical bound.
- `src/herdr_routines/tick.py:896` `_process_base_target` tail block, `src/herdr_routines/tick.py:1199` `_dispatch_fix_worker` tail block, `src/herdr_routines/tick.py:1229` `_outcome_extra` if tail path/session_id needs history plumbing — mirror capture-before-close for gated/base workers.
- `scripts/pipeline-launch.sh:139` `TAIL_FILE` — confirm pipeline orchestrator path also captures `visible --lines 200` before pane close, consistent with routine jobs.
- `docs/pipeline/design.md:191` (G-16 per-stage close) and new `docs/process/pane-retention.md` + `docs/plan-v1.md:430` — document retention window (immediate pane close; 14-day artifact retention; session_id durable).
- `tests/test_runner.py`, `tests/test_tick.py` — add success-tail test (idle/done writes `{run_id}.tail.txt` with 200-line bound, pane closed after); keep failure-tail tests green; blocked stays-open test unchanged.

Not touched: `src/herdr_routines/schedule.py` (catch-up), `src/herdr_routines/history.py` format (tail is a file, not JSONL), `src/herdr_routines/gc.py` / issue 021 pruning mechanism (policy only), `src/herdr_routines/config.py` (no new job key; bound is a code constant + spec), `digest.py`.

## Acceptance criteria

1. [blocking] Routine success (`idle`/`done` with non-empty report) persists a bounded visible tail to `reports/{run_id}.tail.txt` before pane close — confidence: high — Test: test_success_captures_bounded_visible_tail_before_close
2. [blocking] Success tail is a single write via `agent_read_visible --lines 200` (visible source, 200-line bound); no leftover `recent-unwrapped` write and no dual-write on success — confidence: high — Test: test_success_tail_uses_visible_source_and_single_write
3. [blocking] Ordering invariant holds on success: `agent_read_visible` (tail) → `agent_session_id` → `pane_close`; pane is never closed before tail is on disk (failure paths already obey this) — confidence: high — Test: test_capture_before_session_id_before_close_invariant
4. [blocking] `blocked` settle still captures visible tail via `agent_read_visible` but deliberately skips `pane_close` and `agent_session_id` (pane stays open for human) — confidence: high — Test: test_blocked_captures_tail_but_leaves_pane_open
5. [blocking] `root` workspace jobs never call `pane_close` and never capture `session_id` on success, matching `runner.py:883` guard; tail capture remains best-effort — confidence: high — Test: test_root_mode_never_closes_pane
6. [non-blocking] Pipeline gated/base workers (`tick.py:_process_base_target`, `tick.py:_dispatch_fix_worker`) mirror the same capture-before-close with `agent_read_visible --lines 200` and single tail file — confidence: medium — Test: test_pipeline_workers_capture_tail_before_close
7. [non-blocking] Pipeline launcher (`scripts/pipeline-launch.sh`) captures `visible --lines 200` to sibling `${RUN_ID}.tail.txt` before pane close on non-`idle`/`done` settle — confidence: high — Test: test_pipeline_launcher_captures_visible_tail_on_failure
8. [blocking] Retention policy is documented in `docs/process/pane-retention.md` (and referenced from `docs/pipeline/design.md`) stating: panes close immediately after capture (exceptions: `blocked` stays open, `root` never closes), artifacts (`reports/{run_id}.md` + `.tail.txt` + pipeline siblings) retained 14 days (`gc.py:25` DEFAULT_OLDER_THAN_DAYS), pruning explicit/opt-in only, `session_id` retained indefinitely in `history.jsonl`/`state.json` — confidence: high — Test: test_retention_policy_documented

## Risks

- [blocking] **Success capture widens cost silently.** 200 lines × ~100 bytes ≈ 20 KB/run. With one daily job this is ~7 MB/year; even hourly it is ~170 MB/year — well under the 108 MB/worktree baseline. Risk is bounded by the stated constant; drift requires a spec change. — confidence: high
- [blocking] **Close-before-capture loses the tail.** If capture is reordered after `pane_close`, the agent record is gone and the tail is empty. Mitigated by the capture → session_id → close invariant; blocked is the only branch that skips close. — confidence: high
- [blocking] **Alternate-screen scrollback false promise.** Treating `recent-unwrapped` as "full transcript" would mislead; rows that scrolled away never entered Herdr scrollback (`docs/plan-v1.md:93`). Mitigated by choosing bounded visible read and documenting the limitation explicitly. — confidence: high
- [non-blocking] **Pipeline/routine divergence.** Pipeline workers have an extra state-mirror step before close; forgetting to capture tail in one code path (routine vs `tick.py:_dispatch_fix_worker` vs `scripts/pipeline-launch.sh`) reintroduces the gap. Mitigated by mirroring the same `agent_read_visible --lines 200 → {run_id}.tail.txt → session_id → close` sequence in all three places and testing each. — confidence: medium
- [non-blocking] **Tail overwrites recent-unwrapped vs visible race.** `runner.py:794` today writes `recent-unwrapped` then later success writes visible — two writes to same path. Mitigated by consolidating to one write on success (visible), removing the duplicate. — confidence: high
- [non-blocking] **Retention doc vs mechanism split (021).** Doc says 14 days but pruning is manual/explicit; without `gc` follow-through tails/reports still accumulate. Acceptable — this issue owns the policy, 021 owns the mechanism; empty retention window is worse than a delayed prune. — confidence: high
- [non-blocking] **Spec-path hygiene (G-15).** Must stay at `docs/pipeline/runs/<run_id>/spec.md`; writing to `docs/pipeline/spec.md` reintroduces PR #28/#29 shared-path merge conflict. — confidence: high

## Changelog v1->v2

- Added `## Acceptance criteria` (8 numbered items, each `Test: <name>`) with `[blocking]`/`[non-blocking]` tiers and `confidence:` per spec-review conventions; honesty fix: criteria do not claim fuller scrollback or unbounded transcripts, only bounded `visible --lines 200`.
- Added `## Changelog v1->v2` (this section).
- Fixed ordering invariant to state both exceptions explicitly: `blocked` (tail but no close/session_id) and `workspace == "root"` (never closes, never captures session_id per `runner.py:883`); v1 implied "every settled run closes."
- Pinned retention doc to canonical `docs/process/pane-retention.md` (referenced from `docs/pipeline/design.md`/`docs/plan-v1.md:430`); v1 said "or" between two locations — untestable.
- Clarified success-tail consolidation: `runner.py:794-799` `recent-unwrapped` block is removed/replaced (not "either/or fallback") so success produces exactly one `visible` tail file; pipeline sibling named `reports/pipeline-{run_id}.tail.txt` for consistency.
- No scope widening: bound stays 200 lines, no new job config key, no automatic pruning (policy only, mechanism stays in 021/`gc.py`).
