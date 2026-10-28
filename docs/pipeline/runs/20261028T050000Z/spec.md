# Spec v1 — 20261028T050000Z

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
