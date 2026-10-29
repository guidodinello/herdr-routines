# Spec v2 — 20261028T050000Z

Feature: close issue 057's two open review notes — the write-ahead layering and
the audit record shape (`docs/process/issues/067-close-issue-057-s-two-open-review-notes.md`).

## Problem

Issue 057's refine loop capped out with two open review notes, both about
specification and coverage, not wrong code:

- **Note (a)** — the write-ahead ordering (ledger saved *before* the fix worker
  is dispatched, hand-off written only *after* the worker settles) has no test.
  `test_audit_dispatch.py::test_failed_fix_dispatch_still_consumes_budget`
  asserts post-mortem ledger state only; a refactor moving `save_ledger` below
  the dispatch would pass silently and reintroduce the lost-attempt /
  duplicate-PR bug. Meanwhile
  `test_findings.py::test_ledger_write_ahead_consumes_budget_on_failed_dispatch`
  is a *pure* `apply_diff` test wearing a tick-level claim in its name.
- **Note (b)** — the audit history record's runtime shape (`gate`, ~12 `extra`
  keys, which key on which path) is specified in Design prose but pinned by no
  numbered criterion and no test. A sixth `gate` value without a
  `_AUDIT_GATE_NOTIFY_KIND` entry is a `KeyError` on a cron fire, and the
  `gate == "failed"` + `gate_branch` coupling with
  `auto_fix.consecutive_base_gate_failures` is nowhere guarded.

Phase A (PR #156) and phase B (PR #158) have landed, so both notes are now
buildable. Production code already satisfies both properties.

## Approach

Test-only; the diff to `src/` is empty or comments/docstrings only (criterion 10).
Does not reopen 057 (stays `done`, gets a Log entry); does not overlap 064
(write-side `HistoryExtra` constructor stays 064's).

**Part 1 — write-ahead ordering, two layers:**

- Layer 1 (pure, `findings.apply_diff`): rename
  `test_ledger_write_ahead_consumes_budget_on_failed_dispatch` →
  `test_apply_diff_spends_budget_for_exactly_the_dispatched_ids`. Asserts:
  each dispatched id gets `attempts + 1` and
  `last_dispatched_run == run_id`; non-dispatched ids (incl. one at its attempt
  cap) keep `attempts` and their previous `last_dispatched_run`; `queued` is set
  for the ids the caller queued and preserved for others; result survives a
  `save_ledger` → `load_ledger` round trip. No dispatch or disk-I/O assertions
  (the layer has neither).
- Layer 2 (tick, `tick._diff_and_fix`): new
  `tests/test_kind_audit.py::test_ledger_is_on_disk_before_the_fix_worker_is_dispatched`.
  Replaces `_dispatch_audit_fix` with a recorder that reads the ledger from
  disk **at call time** and asserts the captured entry already has
  `attempts == 1`, `last_dispatched_run == run_id`, `queued is True`. Must be
  verified to fail when `save_ledger` is moved below the dispatch — reading the
  code is not verification.
- The failure side stays in
  `test_audit_dispatch.py::test_failed_fix_dispatch_still_consumes_budget`.
  All three tests' comments name the same layering so any one reader learns
  where the other two live.

**Part 2 — audit record shape (test pins this contract):**

- Gate set closed at five members: `baseline | passed | fix_dispatched |
  suppressed | failed`. Anchor: `_AUDIT_GATE_NOTIFY_KIND`'s key set is asserted
  to be exactly those five (the tick indexes it directly); driven scenarios
  produce exactly those five values and no others.
- Verdict vs outcome: drive every `state`/`gate` pairing from the Design table,
  including `failed` + `fix_dispatched` (dispatch failed after a verdict) and
  `failed` + `failed` for pre-verdict failure (manifest invalid, ledger
  corrupt).
- Key sets per path: every record → `gate, target, manifest_path, ledger_path,
  report_path, report_written`; failure paths add `reason`; diff path only →
  `check, duplicate_ids, findings_total, new, regressed, unchanged, resolved,
  dispatched, suppressed, adopted, dispatched_ids, suppressed_ids`; dispatch
  path only → `gate_branch, branch, agent_name, pane_id, final_agent_status,
  session_id, fix_report_path, fix_report_written`; `error` on failure paths.
  A failed dispatch carries `reason` + `error` *in addition to* dispatch keys.
  Closed-key scope: the audit paths as they are today (on-disk shape is 064's).
- Counts agree: `findings_total == new + regressed + unchanged`;
  `dispatched == len(dispatched_ids)` (bounded by `max_findings_per_dispatch`,
  not capped); `suppressed` is the true count while `suppressed_ids` is capped
  at `tick._AUDIT_SUPPRESSED_ID_CAP` (20); `adopted == findings_total` on
  baseline, `0` elsewhere; `duplicate_ids` empty unless the manifest collapsed
  findings (pins the shipped list behaviour, diverging from 057's "count"
  prose — divergence recorded in 057's Log).
- Cross-family: audit records never move
  `auto_fix.consecutive_base_gate_failures` (counter stays 0 after a failed
  audit dispatch and a failed audit command).

**Part 3 — close the notes in 057:** Log entry naming both notes, this issue,
and the tests carrying each claim; 057 `status` stays `done`, criteria
untouched. ROADMAP Parking Lot bullet carries the pointer to issue 067.

## Files touched

- `tests/test_findings.py` — rename + rewrite the layer-1 test.
- `tests/test_kind_audit.py` — new ordering test + gate-set / pairing / key-set
  / counts / cap / cross-family tests.
- `tests/test_audit_dispatch.py` — comments naming the layering (test stays).
- `docs/process/issues/057-*.md` — Log entry.
- ROADMAP Parking Lot bullet.

## Risks

- **Testing-implementation risk:** the layer-2 test reads the ledger at
  dispatch-call time via monkeypatching `_dispatch_audit_fix`; a tick refactor
  of that seam could churn the test. Accepted — that seam is the property.
- **False confidence from post-hoc assertions:** criterion 2's failure-mode
  verification (temporarily moving `save_ledger`) must actually be run and
  recorded in the PR, not just claimed.
- **Spec drift:** naming a key set "closed" invites test breakage when keys are
  added later; deliberate per criterion 6 — adding a key must update this test.
- **Ruff/mypy on test-only changes:** closed-key assertions must keep the
  touched modules under the `scripts/ci-check.sh` gate (ruff format, ruff
  check, mypy, pytest).

## Verification

`scripts/ci-check.sh` green; the three touched test modules pass on their own
(`uv run pytest tests/test_findings.py tests/test_kind_audit.py
tests/test_audit_dispatch.py -q`); `git diff src/` is empty or comments-only.

## Acceptance criteria

1. [blocking] Layer 1 (pure, `findings.apply_diff`):
   `tests/test_findings.py` has
   `test_apply_diff_spends_budget_for_exactly_the_dispatched_ids` (the rename
   of `test_ledger_write_ahead_consumes_budget_on_failed_dispatch`, which no
   longer exists under the old name). It asserts each dispatched id gets
   `attempts + 1` and `last_dispatched_run == run_id`; every non-dispatched
   id — including one already at its attempt cap — keeps its `attempts` and
   its previous `last_dispatched_run`; `queued` is set for the ids the caller
   queued and preserved for the others; and the result survives a
   `save_ledger` → `load_ledger` round trip, with no dispatch or disk-I/O
   assertions (the layer has neither). — confidence: high — Test:
   test_apply_diff_spends_budget_for_exactly_the_dispatched_ids
2. [blocking] Layer 2 (tick, `tick._diff_and_fix`):
   `tests/test_kind_audit.py` has the new replacement of
   `_dispatch_audit_fix` with a recorder that reads the ledger from disk
   **at call time** and asserts the captured entry already has
   `attempts == 1`, `last_dispatched_run == run_id` and `queued is True` for
   the dispatched finding. The PR states how the test was verified to fail
   when `save_ledger` is moved below the dispatch — reading the code is not
   verification. — confidence: high — Test:
   test_ledger_is_on_disk_before_the_fix_worker_is_dispatched
3. [blocking] The failure side stays intact with the layering named everywhere:
   `tests/test_audit_dispatch.py::test_failed_fix_dispatch_still_consumes_budget`
   stays green unchanged, and the comments in all three write-ahead tests
   name the same layering (pure arithmetic / tick ordering / failure side) so
   a reader landing on any one learns where the other two are. — confidence:
   high — Test: test_failed_fix_dispatch_still_consumes_budget
4. [blocking] The gate set is closed at five members
   (`baseline | passed | fix_dispatched | suppressed | failed`): a test
   asserts `_AUDIT_GATE_NOTIFY_KIND`'s key set is exactly those five and
   that the scenarios the tick drives produce exactly those five `gate`
   values and no others. — confidence: high — Test:
   test_audit_gate_set_is_closed
5. [blocking] Verdict vs outcome: a test drives every `state`/`gate` pairing
   from the Part 2 table, including `failed` + `fix_dispatched` (dispatch
   failed after a verdict was reached) and `failed` + `failed` (manifest
   invalid and ledger corrupt cover the pre-verdict reasons). — confidence:
   high — Test: test_audit_state_gate_pairings_match_the_design_table
6. [blocking] The key sets are per path (audit paths as they are today; the
   open-ended on-disk shape is 064's): every record → `gate, target,
   manifest_path, ledger_path, report_path, report_written`; failure paths
   add `reason` and `error`; the diff path alone → `check, duplicate_ids,
   findings_total, new, regressed, unchanged, resolved, dispatched,
   suppressed, adopted, dispatched_ids, suppressed_ids`; the dispatch path
   alone → `gate_branch, branch, agent_name, pane_id, final_agent_status,
   session_id, fix_report_path, fix_report_written`; a failed dispatch
   carries `reason` + `error` *in addition to* the dispatch keys; a key that
   is neither required nor allowed on a path fails the test. — confidence:
   high — Test: test_audit_extra_key_sets_are_per_path
7. [blocking] The counts agree on the diff path:
   `findings_total == new + regressed + unchanged`;
   `dispatched == len(dispatched_ids)` bounded by `max_findings_per_dispatch`
   (not capped); `adopted == findings_total` on a baseline adoption and
   `adopted == 0` elsewhere; `duplicate_ids` empty unless the manifest
   actually collapsed findings onto one derived id (pins the shipped list
   behaviour, diverging from 057's "count" prose — divergence recorded in
   057's Log). — confidence: high — Test: test_audit_counts_agree
8. [non-blocking] The suppressed cap: with more than
   `tick._AUDIT_SUPPRESSED_ID_CAP` (20) findings at their attempt cap, the
   record's `suppressed` count is the full number while `suppressed_ids`
   holds exactly 20 ids; `dispatched_ids` is bounded by
   `max_findings_per_dispatch`, not capped this way. — confidence: medium —
   Test: test_suppressed_count_is_true_while_ids_are_capped
9. [blocking] No cross-family charge: a test asserts audit records never move
   `auto_fix.consecutive_base_gate_failures` — after a failed audit dispatch
   and a failed audit command, the counter for that job name is 0 (the
   ledger's `max_attempts_per_target` is the budget, never charged twice). —
   confidence: high — Test:
   test_audit_records_never_move_consecutive_base_gate_failures
10. [blocking] No behaviour change: `git diff src/` is empty or limited to
    comments and a docstring; no history record's contents change and no
    notification changes (any needed production change would be a pure
    refactor with byte-identical records, clear of 064's write-side
    constructor); the shipped record contents stay pinned by the existing
    dispatch-set test, green and untouched. — confidence: high — Test:
    test_report_and_extra_record_the_full_dispatch_set
11. [non-blocking] The notes are closed in the file that raised them: 057's
    Log entry names both notes, this issue and the tests carrying each
    claim; 057's `status` stays `done` with its criteria untouched; the
    ROADMAP Parking Lot bullet carries the pointer to issue 067 (the
    Log-entry half is doc-only; the roadmap linkage guard is executable). —
    confidence: high — Test: test_every_issue_file_is_linked_from_roadmap

## Changelog v1→v2

- Added `## Acceptance criteria`: eleven numbered items mirroring issue 067's
  acceptance list, tagged `[blocking]`/`[non-blocking]` with confidence
  tiers, each ending `Test: <exact test name>`.
- Test names made contractual: the layer-1 rename
  (`test_apply_diff_spends_budget_for_exactly_the_dispatched_ids`), the new
  layer-2 ordering test
  (`test_ledger_is_on_disk_before_the_fix_worker_is_dispatched`), the
  gate-set anchor, the pairing / key-set / counts / cap / cross-family tests,
  and the two existing tests that carry claims unchanged
  (`test_failed_fix_dispatch_still_consumes_budget`,
  `test_report_and_extra_record_the_full_dispatch_set`,
  `test_every_issue_file_is_linked_from_roadmap`).
- Spec header bumped v1 → v2; Problem, Approach, Files touched, Risks and
  Verification unchanged in substance — the criteria pin what v1 described
  instead of altering it.

## Review

- blocking: 1–7 and 9–10 — the write-ahead layering, the closed gate set, the
  verdict/outcome pairing, the per-path key sets, the count agreement, the
  cross-family budget isolation and the no-behaviour-change guard gate the
  merge.
- non-blocking: 8 (cap fidelity past 20 suppressed findings) and 11 (paper
  closure in 057's Log and the ROADMAP) degrade fidelity of an edge and of
  bookkeeping, not correctness.
- CI gate: `scripts/ci-check.sh` green (ruff format, ruff check, mypy,
  pytest) and the three touched test modules pass on their own.
