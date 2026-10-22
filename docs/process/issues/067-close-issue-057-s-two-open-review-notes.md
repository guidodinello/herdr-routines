---
id: "067"
title: "Close issue 057's two open review notes: the write-ahead layering and the audit record shape"
status: open
priority: low
area: infra
---

## Description

Issue 057's refine loop hit its three-pass cap with `confidence: medium` and two
notes still open (057's Log, 2026-10-01, pass 3). They were carried to that PR's
body instead of being fixed, because fixing either one properly needed another
author/reviewer pass. They are:

> **(a)** Criterion 9 tests write-ahead ordering via "a dispatch that raises still
> leaves the incremented ledger on disk", which cannot be exercised while criterion
> 16 forbids phase A from dispatching; the layering between the pure `apply_diff`
> and a tick-level integration test is not spelled out.
>
> **(b)** The new `gate` value set and the ~12 history `extra` fields are specified
> in Design but have no numbered criterion asserting the record shape.

Both notes are about *specification and coverage*, not about wrong code. What
changed since 057 was refined is that the rest of the design shipped, so the
parts the notes complained about can finally be written down and pinned:

- **Phase A** landed in PR #156 — the config, the manifest contract and the whole
  diff/ledger half (`findings.py`).
- **Phase B** landed in PR #158 (issue 058, now `done`) — the audit runs and the
  fix worker is dispatched, so the tick *does* dispatch now. That is exactly what
  made note (a)'s tick-level half untestable in phase A, and exactly what makes it
  testable today.

What is still missing is the two things the notes asked for.

### Note (a): the write-ahead ordering has no layering, and the ordering itself is untested

The code does the right thing. `tick._diff_and_fix` writes the ledger
(`save_ledger`) after the diff and **before** it calls `_dispatch_audit_fix`, and
only writes the hand-off (`mark_handed_off`) after the worker settles. But the
tests do not say so:

- `tests/test_findings.py::test_ledger_write_ahead_consumes_budget_on_failed_dispatch`
  is a **pure** test of `apply_diff` — it knows nothing about dispatch, agents or
  failures, and its name claims the tick-level property. The claim in its last
  paragraph ("that the tick saves it *before* dispatching is pinned through the
  real tick by `test_audit_dispatch.py::test_failed_fix_dispatch_still_consumes_budget`")
  points at a test that asserts the *post-mortem* ledger state.
- `tests/test_audit_dispatch.py::test_failed_fix_dispatch_still_consumes_budget`
  asserts that after a failed agent start the ledger on disk has `attempts == 1`.
  That proves the budget was spent on a failure. It does **not** prove the
  *ordering*: a refactor that moved `save_ledger` to after the dispatch (still
  writing it unconditionally) would pass that test and would silently reintroduce
  the bug the design exists to prevent — a crash between the dispatch and the
  write loses the attempt, and the next cycle opens a duplicate PR for a finding
  the previous PR may already have fixed.

So the ordering itself has no test. That is the gap note (a) names, and it is
one line of production code away from a silent regression.

### Note (b): the record's shape is nowhere stated and nowhere tested

`gate` and the `extra` keys exist in three places that are not connected by any
test: 057's Design prose, `history.HistoryExtra` (the TypedDict — a *write-side*
schema, and `total=False`), and the code that builds the payload
(`tick._diff_and_fix` and `tick._record_audit_failure`). The runtime shape —

- which `gate` values exist, and which `state` each one pairs with;
- which `extra` keys are on the diff path, which are only on the dispatch path,
  which are only on the pre-verdict failure path;
- that `suppressed` is a count while `suppressed_ids` is a capped list;

— is asserted nowhere. Individual tests read individual keys (`rec.extra["gate"]`,
`rec.extra["adopted"]`, `rec.extra["dispatched_ids"]`), but nothing says the set
is closed, nothing says a key is *absent* where it should be, and nothing ties
the `gate` value set to the notification mapping the tick indexes.

Two concrete failure modes follow:

1. `_AUDIT_GATE_NOTIFY_KIND` (`tick.py:424`) is indexed directly
   (`_AUDIT_GATE_NOTIFY_KIND[gate]`). A sixth `gate` value without a mapping
   entry is a `KeyError` at 04:00 on a cron fire, not a test failure.
2. `gate` is a **shared** key with the gated-job family:
   `auto_fix.consecutive_base_gate_failures` reads it and counts a record as a
   failure only when `gate == "failed"` *and* a `gate_branch` is present. An audit
   record's `gate` therefore has to stay clear of that coupling, and nothing says
   so.

### What this issue is, and is not

It is **test-only**: pin the two properties, name them, and record where each
one lives. The production code already satisfies both, so no behaviour changes
and no record's contents change. It also **does not** reopen 057: 057's
acceptance criteria were phase A's and they shipped; the two notes were review
notes on the *refine*, not unmet criteria. 057 stays `done`, and gets a Log
entry pointing here.

It is also **not** issue 064 (`Enforce the HistoryExtra schema when writing
history records`), which owns the write-side constructor and is blocked on 060.
064 makes mypy check the *declared* keys; this issue pins the *runtime* shape —
which key is present on which path — and does not depend on 064.

## Design

### Part 1 — the write-ahead ordering, in exactly two layers

The property is one sentence: **the attempt is spent before the worker is
dispatched, and only the dispatched findings spend it.** It has two owners, and
each owns the half it can actually see.

**Layer 1 — pure (`findings.apply_diff`): spending the budget.**

`apply_diff` is the only place that increments `attempts`. Its contract:

- `dispatched_ids` is the *complete* set of IDs that spend an attempt; each of
  them gets `attempts + 1` and `last_dispatched_run = run_id`.
- Every other entry is untouched: its `attempts` stays, and its
  `last_dispatched_run` is preserved (not blanked).
- The same call also sets `queued` (from its `queued_ids` argument): a finding
  that is owed a fix stays queued, and the queue is what layer 2 hands off after
  the worker settles. A dispatched finding is queued at that moment — it is
  either new/regressed (so the tick queued it) or a finding that was already
  queued from an earlier cycle — and it is cleared only by the hand-off.
- The increment lands in the returned `Ledger`. Nothing in this layer touches
  the disk — `apply_diff` has no I/O beyond the objects it is handed, and it
  knows nothing about dispatch, agents, failures or ticks.

**Layer 2 — the tick (`tick._diff_and_fix`): the ordering.**

The ledger layer 1 returned is written to disk **before** `_dispatch_audit_fix`
is called, and the hand-off (`mark_handed_off`) is written only **after** the
worker settles. The invariant to pin: *at the moment the fix worker is
dispatched, the on-disk ledger already shows the spent attempt.*

The layering is also a boundary on where each claim may be tested:

- A layer-1 test may not assert anything about a dispatch (there is none), and
  may not assert anything about what is on disk (the layer does not write).
- A layer-2 test may not re-derive the budget arithmetic (the tick must not
  compute `attempts`; it only chooses the dispatch set and hands it to layer 1).

Two named tests, one per layer:

- `tests/test_findings.py::test_apply_diff_spends_budget_for_exactly_the_dispatched_ids`
  — the rename of today's `test_ledger_write_ahead_consumes_budget_on_failed_dispatch`,
  which currently wears the layer-2 claim in its name. It asserts the layer-1
  contract above, plus that the result survives a `save_ledger` → `load_ledger`
  round trip.
- `tests/test_kind_audit.py::test_ledger_is_on_disk_before_the_fix_worker_is_dispatched`
  — new. It replaces `tick._dispatch_audit_fix` with a recorder that reads
  `findings.ledger_path(job.name)` **from disk at the moment it is called** and
  captures the dispatched entry. The assertions are on that captured state:
  `attempts == 1`, `last_dispatched_run == run_id`, `queued is True` (the
  hand-off has not happened yet — that is the post-settle write).

The third assertion of the same property — the budget is spent even when the
dispatch *fails* — stays in
`tests/test_audit_dispatch.py::test_failed_fix_dispatch_still_consumes_budget`,
which already covers it from the failure side. Its comment, the pure test's
comment and the new test's comment must all name the same layering, so a reader
landing on any one of them learns where the other two are.

### Part 2 — the audit record's shape

The shape is specified here as a contract, because it is what the test pins.

**The `gate` value set is closed and has five members:**

```
baseline | passed | fix_dispatched | suppressed | failed
```

**`gate` is the diff verdict, not the run outcome.** The run outcome is `state`.
The two are independent, and one pairing looks like a bug but is not:

| path | `state` | `gate` |
|---|---|---|
| cold start, `adopt_baseline: true`, findings present | `done` | `baseline` |
| clean night (no findings, or nothing to do) | `done` | `passed` |
| everything at its attempt cap | `done` | `suppressed` |
| a fix worker ran and settled | `done` | `fix_dispatched` |
| a fix worker was dispatched and **failed** | `failed` | `fix_dispatched` |
| failure **before** a verdict (manifest invalid, ledger corrupt, audit command missing, worktree failure) | `failed` | `failed` |

The fifth row is the one to get right: the verdict *was* reached (the diff
succeeded, the budget was spent, the finding is queued for the next cycle), so
the verdict stays `fix_dispatched` and the failure is carried by `state` plus
`reason`. Turning it into `gate == "failed"` would claim there was no verdict —
and `state == "failed"` with `gate == "failed"` is reserved for a run that
never got one. The notification for that path is still a `failure` notification
(`_AUDIT_GATE_NOTIFY_KIND["failed"]`), which is a separate axis from the
recorded verdict.

**The gate set and the notification mapping are the same set.**
`_AUDIT_GATE_NOTIFY_KIND` (in `tick.py`) must have exactly these five keys, and
the tick's direct index of that mapping is why: a gate value without a mapping
entry is a `KeyError` on a cron fire. The mapping is the only place the set
exists as data, so it is the mechanical anchor of the test: its key set is
asserted to be exactly the five literals above, and the scenarios the tick
drives are asserted to produce exactly those five `gate` values and no others.

**The `extra` keys, per path.** Every audit terminal record carries:

| keys | on |
|---|---|---|
| `gate`, `target`, `manifest_path`, `ledger_path`, `report_path`, `report_written` | every path |
| `reason` | every failure path |
| `check`, `duplicate_ids`, `findings_total`, `new`, `regressed`, `unchanged`, `resolved`, `dispatched`, `suppressed`, `adopted`, `dispatched_ids`, `suppressed_ids` | the diff path only (a verdict was reached) |
| `gate_branch`, `branch`, `agent_name`, `pane_id`, `final_agent_status`, `session_id`, `fix_report_path`, `fix_report_written` | the dispatch path only (a worker ran) |
| `error` | a failure path, `str` or `None` |

`target` is `"base"` for every audit record (`job.target or "base"`). The
pre-verdict failure path carries no counts and no `check`: there is no diff, so
there is nothing to count. The dispatch-only keys are absent from a record where
no worker ran — `gate_branch` in particular, because its presence is what
`auto_fix.consecutive_base_gate_failures` reads to decide whether a record
spent the gated-job retry budget.

**The counts are internally consistent.** On the diff path:

- `findings_total == new + regressed + unchanged` — the manifest's findings are
  partitioned by the diff into exactly those three buckets.
- `dispatched == len(dispatched_ids)`, and `dispatched_ids` is bounded by
  `max_findings_per_dispatch` (it is *not* capped).
- `suppressed` is the **true count** of findings at their attempt cap, while
  `suppressed_ids` is the **list**, capped at `tick._AUDIT_SUPPRESSED_ID_CAP`
  (20). With more than 20 suppressed findings the two disagree on purpose:
  `suppressed == 25`, `len(suppressed_ids) == 20`.
- `adopted == findings_total` on a baseline adoption, and `adopted == 0` on
  every other path.
- `duplicate_ids` is the list of derived IDs that more than one finding
  collapsed onto, and it is empty unless the manifest actually collapsed
  something.

**One divergence from 057's Design text, pinned in the code's favour.** 057
says "the count is reported in `extra["duplicate_ids"]`". The shipped code
records the **list** of collapsed IDs, not a count. The test pins the shipped
behaviour (a list of IDs); 057's Design prose is left alone and the divergence
is recorded in 057's Log entry. Same class of thing for the report: the
aggregate report prints `- duplicate_ids: …` only when the list is non-empty.

**Cross-family: an audit record never spends the gated-job retry budget.**
`auto_fix.consecutive_base_gate_failures(history_path, job_name)` counts a
record only when `extra["target"] == "base"` and `extra["gate"] == "failed"`
and `extra["gate_branch"]` is present. An audit record never satisfies all
three — the pre-verdict failure path has no `gate_branch`, and the dispatch
failure path has `gate == "fix_dispatched"`. The audit's own budget is the
ledger's `max_attempts_per_target`, and the two must not be charged twice.

### Part 3 — closing the notes in 057

057's Log gets one entry naming both notes, this issue, and the tests that now
carry each claim — so the next reader of 057 sees that the notes were closed
rather than re-reading them as open debt. 057's `status` stays `done` and its
acceptance criteria are untouched.

## Acceptance

1. **Layer 1 (pure).** `tests/test_findings.py` has
   `test_apply_diff_spends_budget_for_exactly_the_dispatched_ids` (the rename
   of `test_ledger_write_ahead_consumes_budget_on_failed_dispatch`, which no
   longer exists under the old name). It asserts that each dispatched id's
   `attempts` is +1 with `last_dispatched_run == run_id`; that every
   non-dispatched id — including one already at its attempt cap, i.e.
   suppressed — keeps its `attempts` *and* its previous `last_dispatched_run`;
   that `queued` is set for the ids the caller queued and preserved for the
   others; and that the result survives a `save_ledger` → `load_ledger` round
   trip.
2. **Layer 2 (ordering).** `tests/test_kind_audit.py` has
   `test_ledger_is_on_disk_before_the_fix_worker_is_dispatched`. It replaces
   `tick._dispatch_audit_fix` with a recorder that reads the ledger from disk
   **when it is called**, and asserts the captured on-disk entry already has
   `attempts == 1`, `last_dispatched_run == run_id` and `queued is True` for
   the dispatched finding. The PR states how the test was verified to fail when
   the ordering is broken (moving the `save_ledger` call below the dispatch is
   enough) — reading the code is not verification.
3. **The failure side stays.** `tests/test_audit_dispatch.py::test_failed_fix_dispatch_still_consumes_budget`
   is still green, and the comments in all three tests name the same layering
   (which layer owns which claim) instead of pointing at each other loosely.
4. **The gate set is closed.** A test asserts two things: that
   `tick._AUDIT_GATE_NOTIFY_KIND`'s key set is exactly
   `{"baseline", "passed", "fix_dispatched", "suppressed", "failed"}`, and that
   the scenarios the tick drives (criterion 5) produce exactly those five
   `gate` values and no others. The first half is the mechanical anchor — the
   mapping is the only place the set exists as data, and the tick indexes it
   directly, so a sixth gate value without a mapping entry is a `KeyError` on a
   cron fire. The second half is what makes it a closed set rather than a
   documented one: a new gate value that no scenario reaches is caught by the
   first half, and a scenario that writes a sixth value is caught by the
   second.
5. **Verdict vs outcome.** A test drives every row of the table in Part 2 and
   asserts the `state`/`gate` pairing, including `failed` + `fix_dispatched`
   for a dispatch that failed after a verdict, and `failed` + `failed` for a
   pre-verdict failure (manifest invalid and ledger corrupt are enough to
   cover both reasons).
6. **The key sets are per path.** A test asserts the required keys are present
   and the forbidden keys are absent for each path: the pre-verdict failure
   carries no `check` and no counts; a no-dispatch record carries none of the
   dispatch-only keys (in particular no `gate_branch`); a dispatch record
   carries all of them; a failed dispatch carries `reason` and `error` *in
   addition to* the dispatch keys. A key that is neither required nor allowed
   on a path fails the test — for the audit paths as they are today. Adding a
   key later is a deliberate act that updates this test; the open-ended
   *on-disk* shape (old records carrying keys no writer emits any more) is
   064's business and is not what this test asserts.
7. **The counts agree.** A test asserts `findings_total == new + regressed +
   unchanged`, `dispatched == len(dispatched_ids)`, `adopted == findings_total`
   on a baseline and `adopted == 0` elsewhere, and that `duplicate_ids` is
   empty unless the manifest actually collapsed two findings onto one derived
   id.
8. **The suppressed cap.** With more than `tick._AUDIT_SUPPRESSED_ID_CAP` (20)
   findings at their attempt cap, the record's `suppressed` count is the full
   number and `suppressed_ids` holds exactly 20 ids. `dispatched_ids` is not
   capped this way — it is bounded by `max_findings_per_dispatch`.
9. **No cross-family charge.** A test asserts that an audit job's records never
   move `auto_fix.consecutive_base_gate_failures`: after a failed audit
   dispatch and a failed audit command, the counter for that job name is 0.
10. **No behaviour change.** The diff to `src/` is empty, or limited to
    comments and a docstring; no history record's contents change, and no
    notification changes. (If a production change turns out to be needed to
    make a property observable, it must be a pure refactor with byte-identical
    records — and it is not a change to 064's write-side constructor, which
    stays 064's.)
11. **The notes are closed in the file that raised them.** 057's Log gets an
    entry naming both notes, this issue, and the tests that carry each claim.
    057's `status` stays `done` and its acceptance criteria are unchanged. The
    ROADMAP Parking Lot bullet for this idea carries the pointer to this file.
12. `scripts/ci-check.sh` is green (ruff format, ruff check, `uv run mypy`,
    `uv run pytest -q`), and the three touched test modules pass on their own.

## Log

- **2026-10-21**: filed from the ROADMAP Parking Lot, selected by
  `herdr-routines refine-issue` under issue 029. Both notes are now buildable:
  phase B (issue 058) has landed, so the tick dispatches and the ordering half
  of criterion 9 can finally be tested at the tick level, and the record shape
  exists in code and needs a test that pins it. Test-only — the production code
  already does both things right; what is missing is the specification of which
  layer owns which claim and any test that would notice if the ordering or the
  shape moved.
- **2026-10-21 (refine review, pass 1, `confidence: high` — consensus):** three
  non-blocking notes applied. (a) Criterion 4 had no mechanical source for "the
  set of `gate` values the audit path can write" — the notification mapping is
  the only place the set exists as data, so it is now the test's anchor, and the
  driven scenarios are asserted to produce exactly those five values and no
  others. (b) Layer 1's contract now names `queued`, which the layer-2 test's
  `queued is True` assertion silently depended on. (c) Criterion 6's
  closed-key assertion is scoped to the audit paths as they are today, and says
  plainly that the open-ended *on-disk* shape is 064's business.
