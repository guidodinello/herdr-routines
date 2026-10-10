---
id: "057"
title: "Audit skills as report→diff gate jobs: schedule an audit, fix only new/regressed findings"
status: done
priority: medium
area: pipeline
gate: phase A (criteria 1-16) is the buildable ticket and closes this issue; phase B (17-22) is listed here for 058 to be filed from, exactly as 054/056 split the orchestrator work
---

## Description

The bullet: turn fitted's audit skills (`type-health`, `ui-ux-review`,
`accessibility-review`, `fix-ignores`, `discover-conventions`,
`improve-codebase-architecture`, `api-gap-audit`) into scheduled jobs — run a cheap
check/report, then on the next cycle diff against the last report and spawn a fix
worker only for findings that are new or regressed. It maps onto issue 025's gate
model ("all checks pass → free"), and 025 has shipped (PR #56), so this is ready
to refine rather than to re-gate.

Today the same audit run by hand is a one-shot: an agent loads `type-health`,
writes a report, and the report rots. Nothing re-runs it, nothing notices that
finding #7 came back, and nothing carries the result into a PR. The value being
promoted is not "run the audit" (a plain `kind: routine` job already does that —
`deploy/jobs.d/nightly-dep-audit.yaml` is the shape) but **"run the audit, then act
only on the delta."**

### The one hard problem: a report is not a check

025's gate is deliberately thin. `run_checks` (`auto_fix.py:549`) reduces a check to
`exit_code == 0`, aggregates, and hands a boolean to the tick: all pass → `done`, no
agent, free; any fail → one fix worker. An audit skill does not have an exit code for
"there are 14 implicit-any returns." It produces a **document**. So the piece 025
does not supply is the converter from a report to a boolean, and the only honest
converter for a recurring audit is a **diff against the previous cycle's report** —
which is exactly what the bullet says, and exactly what the bullet left undesigned.

That diff is where this issue lives, because it introduces state 025 has no notion
of. Three things have to be true or the job is a nightly PR machine:

1. **Findings need stable identity.** A diff is over *keys*. Key a finding by its
   text and the first human who rewords a finding re-arms the gate, and the next
   cycle opens a duplicate PR for debt the previous PR may already have fixed. Key
   it by its line number and the same thing happens every time an unrelated edit
   shifts the line. The identity rule is load-bearing, not a detail.
2. **The retry budget has to move.** 025's base-target budget is
   `attempt_count_for_gate_branch` (`auto_fix.py:684`), keyed on `gate_branch`,
   which is `auto/<job>-<run_id>` (`runner.build_branch_name:376`) — a **new key
   every cron fire**. By design: for `ruff`, each night's red is a fresh occurrence.
   For an audit finding, the finding is the *same* occurrence every week until it is
   fixed, so a per-occurrence budget can never stop re-dispatching it. The budget
   has to be keyed per finding and survive across cycles, which means a persisted
   ledger.
3. **The first cycle must not be a 200-PR storm.** With no history, every existing
   finding is "new," so a codebase with a real audit backlog would try to open one
   PR per finding on the very first tick. A baseline-adoption step is the difference
   between this feature being usable and being a liability.

Everything else here is machinery that follows from those three.

## Design

Ship as two PRs. Phase A is the whole diff/ledger half and dispatches no agent at
all, so the first real run is a readable ledger plus a report and the operator can
look at their own debt before anything opens a PR. Phase B adds the agent-driven
audit and the fix dispatch.

### Scope — phase A is this issue

**This issue is done when phase A's criteria 1–16 pass.** That is a self-contained
ticket: config validation, the manifest contract, the prompt placeholder, and the
entire diff/ledger half, with tests that need no herdr server, no `gh`, and no git.
Phase B (criteria 17–22) is deliberately *not* part of it and is filed as its own
issue — 058 by convention — when phase A lands and has run, because phase B's
prompt and worktree work is only meaningful against a ledger that exists.

That is the same split 054/056 used, and it exists for a reason: phase B is the
part that opens PRs on the operator's behalf, so it wants a real ledger from a real
run behind it before anyone wires it up. Do not start phase B in the same PR as
phase A.

### A note on the source references

The `file.py:123` citations throughout this issue are **annotations, not
contract** — they point at the right function so a reader can find it, and they are
expected to drift as `tick.py`, `config.py` and `auto_fix.py` change (in
particular if issue 055 lands first). Where a line number looks load-bearing, the
symbol next to it is the thing to rely on: `auto_fix.run_checks`,
`auto_fix.attempt_count_for_gate_branch`, `runner.build_branch_name`,
`runner.substitute_prompt`, `tick._process_gated_job`, `tick._process_job`,
`tick._notify_gate`, `config.default_repos_dir`, `reports_prune.prune_reports`,
`cli._check_systemd_timeout`. No acceptance criterion is written against a line
number.

### Why a new `kind`, and not a new check kind

`kind: audit` — one more value of the `kind` literal, one more branch in
`_process_job` (`tick.py:1281`) beside `pipeline` and `gated`.

The alternative is a new check kind (`checks: [{skill: type-health}]`). Rejected
because `run_checks` is synchronous and shell-shaped: it takes `(argv, timeout_s)`
and reads a return code. Running an audit skill needs a worktree, a pane, a prompt,
`start_timeout_ms` polling, a settle watchdog and a cleanup path — all of which
live in `tick._process_base_target` and none of which fit inside a function whose
testability comes from having no dispatch machinery at all. A new `kind` reuses
that machinery as-is and leaves `run_checks` pure.

### Config

```yaml
- name: audit-type-health
  enabled: false # ships off: it opens PRs on the operator's behalf
  cron: "0 4 * * 1" # Mondays 04:00 — clear of the 02:00 pipeline, ahead of the day
  kind: audit
  repo: /home/guido/.local/state/herdr-routines/repos/fitted
  base: development
  audit:
    skill: type-health # agent-driven: the audit agent runs this opencode skill
    command: null # alternative — a cheap synchronous command that writes the manifest
    timeout_ms: 1800000
  max_findings_per_dispatch: 10 # cap on the finding table handed to one worker
  max_attempts_per_target: 3 # per finding ID, across cycles (NOT per occurrence)
  ledger_retention_days: 90
  adopt_baseline: true # first cycle records debt and opens nothing
  agent_kind: opencode
  model: null
  prompt: "" # empty = engine-injected audit prompt
  fix_prompt: "" # empty = engine-injected fix prompt
  timeout_ms: 2700000
  start_timeout_ms: 120000
  catch_up_minutes: 720 # weekly cron: a long catch-up window is the point
  on_missed: log
  notify_policy: on-finding # silent on a clean night — the property being asked for.
                    # Existing issue 009 enum (always|terminal|on-finding|on-failure),
                    # already valid for every kind; not a new config value.
```

Config validation, all rejected at load time with the same message shape as the
existing `kind: pipeline` and `kind: gated` rules:

- `audit` is required and must be a mapping with **exactly one** of `skill` /
  `command`. Both is ambiguous about which manifest wins; neither is not an audit.
  `audit.timeout_ms` must be a positive int.
- `checks` is **not applicable** and is rejected. The audit *is* the gate input; two
  independent gate inputs would make the single `gate:` verdict ambiguous, and
  nothing needs the combination yet.
- `workspace` must be `worktree`. `root` would run an audit agent — and then a fix
  agent — in the operator's live checkout.
- `target` is not applicable: audit is always base-target. Absent is fine; an
  explicit `target: base` is accepted (uniform with 025's records); anything else
  is rejected.
- `max_workers_per_tick` is rejected rather than ignored, because silently
  ignoring a dispatch cap is a trap. The audit dispatches at most one worker per
  tick, always — see the fix phase.
- `max_findings_per_dispatch` ≥ 1, `max_attempts_per_target` ≥ 1,
  `ledger_retention_days` ≥ 1.
- `fix_prompt` is per-job and **not inheritable** via `defaults.yaml`, joining the
  existing non-inheritable set with `kind` and `checks`.

### The findings manifest — the contract everything else rests on

`<reports_dir>/<run_id>-findings.json`, substituted into the audit prompt as
`$ROUTINE_FINDINGS` (a new placeholder alongside `$ROUTINE_REPORT` in
`runner.substitute_prompt:388`):

```json
{
  "version": 1,
  "check": "type-health",
  "findings": [
    {
      "id": "optional-stable-id",
      "kind": "implicit-any-return",
      "severity": "high",
      "location": "app/Services/Invoice.php:212",
      "summary": "…"
    }
  ]
}
```

`load_findings_manifest(path)` validates the whole thing and returns
`list[Finding] | None`. **`None` means unverifiable and is never `[]`** — the same
posture as `RealGhClient.commit_check_runs` ("callers must treat that as
unverifiable, not as green") and 055's `review_not_posted`. `None` is returned for:
file missing, unparseable JSON, not a dict, `version != 1`, `findings` not a list,
or any element that is not a dict / is missing a required string field / has a
`severity` outside `{low, medium, high}`. A manifest is rejected whole rather than
partially parsed because a silently dropped finding reads downstream as "clean" —
the exact quiet-success failure this system keeps guarding against. An
over-long or empty skill-supplied `id` is also `None`.

The one thing that is *not* fatal: two findings that derive the **same** ID. They
collapse to one entry at the higher severity, and the count is reported in
`extra["duplicate_ids"]`. Rejecting a whole report because two findings share a
coarse fallback key would be too brittle — see the consequence below.

### Finding identity

If a finding supplies `id`, it is used verbatim (trimmed, non-empty, ≤ 128 chars).
Otherwise it is derived:

```python
finding_id(check, kind, location) = sha1(f"{check}|{kind}|{normalize_location(location)}")[:12]
normalize_location(s)  # strip whitespace; drop a trailing ":<line>" and/or ":<line>:<col>"
```

**Line numbers are dropped on purpose, and the consequence is stated rather than
hidden.** Two findings of the same kind in the same file therefore share one ID and
are dispatched as one. That is deliberate under-dispatch: the fix worker is handed
the file and the kind and re-runs the audit to enumerate every instance, so a
collapsed finding is fixed completely. The opposite choice is not available — key on
the line and the fixer's own edit moves the finding from 212 to 215, the ID changes,
the diff sees "new," and the next cycle opens a second PR for a finding the first PR
may already have fixed. Under-dispatch costs one extra re-run inside a worker that
is already running; over-dispatch costs a human a duplicate PR every week.

**Supplying `id` is the recommended path** and is what the skills in this bullet
should grow: a structural anchor (rule id, symbol name) is stable where a line
number is not. `severity` is what makes *regression* detectable at all — a skill
that emits a constant severity degrades the feature to new/resolved detection
without breaking anything, which is a documented degradation, not an error.

### The ledger

`<state>/findings/<job-name>.json`, where `<state>` is `$HERDR_PLUGIN_STATE_DIR`
else `~/.local/state/herdr-routines` — the same pattern as
`config.default_repos_dir():344`. **Explicitly not under `reports_dir`**, because
`reports_prune.prune_reports:104` deletes by mtime and would eat the ledger, taking
the entire regression signal with it. One ledger per job; jobs never share one.

```json
{
  "version": 1,
  "job": "audit-type-health",
  "updated": "2026-09-30T04:00:00Z",
  "runs": 3,
  "entries": {
    "3f9a1c2b7d04": {
      "kind": "implicit-any-return",
      "location": "app/Services/Invoice.php",
      "severity": "high",
      "state": "open",
      "first_seen": "2026-09-30T04:00:00Z",
      "last_seen": "2026-09-30T04:00:00Z",
      "resolved_at": null,
      "attempts": 1,
      "last_dispatched_run": "audit-type-health-20260930T040000Z"
    }
  }
}
```

`load_ledger` returns `Ledger | None`; `None` means corrupt, which is **not** the
same as absent. Absent is a cold start. Corrupt is `failed (ledger_corrupt)`, no
audit dispatched, no findings diffed, notify `failure`, and the bad file is first
copied to `<job>.json.corrupt-<ts>` so it can be inspected. It is deliberately not
recovered automatically: treating corruption as "no ledger" makes the job
re-baseline forever and quietly never fix anything, and treating it as "no
findings" skips fixing entirely. A human looking at a `ledger_corrupt` ping is the
only safe answer.

`save_ledger` is atomic — write `<job>.json.tmp`, then `os.replace` — so a crash
mid-write leaves the previous ledger intact rather than a truncated SSOT.

### The diff

Two pure functions in the new module, no herdr, no gh, no git, no I/O beyond the two
files they are handed. This is where the bulk of the tests live, and it is the
reason phase A can ship without touching an agent.

```python
diff_findings(findings, ledger, *, check) -> FindingDiff   # classify only
apply_diff(ledger, diff, *, now, run_id, dispatched_ids) -> Ledger
```

`FindingDiff` carries `new`, `regressed`, `unchanged` (each a tuple of `Finding`)
and `resolved` (a tuple of IDs). Classification:

| Ledger state for the ID | Now | Classified |
|---|---|---|
| absent | present | `new` |
| `resolved` | present | `new` — the finding came back; `first_seen` is preserved |
| `open` | present, severity rank higher | `regressed` |
| `open` | present, same or lower severity | `unchanged` (ledger records the new severity) |
| `open` | absent | `resolved` |

Severity rank is `{"low": 0, "medium": 1, "high": 2}`; a *decrease* is not a
regression, it is a finding getting better. Ordering is total —
`sorted(key=(-severity_rank, location, kind, id))` — so `max_findings_per_dispatch`
drops the least severe rather than an arbitrary set, and two ticks over the same
manifest pick the same findings.

### Cold start: `adopt_baseline`

With no ledger and `adopt_baseline: true` (the default), every finding is recorded
as `state: "open"`, `attempts: 0`, and **nothing is dispatched**. The tick records
`done` with `extra.gate = "baseline"` and `extra.adopted = N`, and notifies under
`finding`: the first ping is "here is your current debt, nothing was opened," and
the report is the artifact to read before cycle 2 starts opening PRs. With
`adopt_baseline: false` the first cycle dispatches immediately, bounded by the cap
and the budget — for an operator who has already triaged by hand.

This is a deliberate divergence from 025, where the first failing tick dispatches
immediately, and the reason is the difference in what a "finding" is. `ruff` red is
red now and its fix is one command; an audit backlog is a heterogeneous human
judgment queue. Dispatching 200 of those unattended is not a first run, it is the
failure mode. The baseline step is this job's admission control, and it is the
single most important behavioral difference from 025.

### Write-ahead ordering

The ledger is written **after** the diff and **before** the fix worker is
dispatched, with `attempts += 1` and `last_dispatched_run = run_id` applied to
exactly the IDs in the dispatch set. So a crash — or a dispatch that raises — still
consumes the budget. Over-counting costs one retry; under-counting costs a duplicate
PR. A finding whose dispatch failed is left `open` with an incremented attempt: it
is retried next cycle until the budget is spent and then lands in the suppressed
set, which is the correct terminal state for "we tried and it did not land."

### The three phases

**Phase 1 — audit.** Create `<repo>/.worktrees/audit-<run_id>` detached at
`job.base`, reusing the force-remove-then-add sequence `_process_base_target`
already runs at `tick.py:631-657`.

- `audit.command` → `run_checks` with one `GateCheck(kind="command", …)` at
  `cwd=wt_path`. **Its exit code is not the gate.** For an audit, non-zero is the
  healthy case, so the code is demoted from "the verdict" to "did the tool run":
  `run_checks` maps a missing binary to `127` (`auto_fix.py:583`), so `127` is
  `failed (audit_command_failed)` and *every other* exit code is a successful audit
  whose manifest is the answer. This is a real divergence from 025, where non-zero
  dispatches, and it is the clearest expression of "a report is not a check."
- `audit.skill` → dispatch an audit agent in the worktree, named
  `rt-<job>-au<h8>` with `h8 = sha1(run_id)[:8]`, through the same
  `_wait_for_agent_ready` / `_prompt_with_watchdog` / `_capture_visible_tail` /
  `_close_run_pane` path `_process_base_target` uses. The injected prompt names the
  skill, carries `$ROUTINE_REPORT` (human Markdown) **and** `$ROUTINE_FINDINGS`
  (the manifest), and states that it must not commit, push, or open a PR.

The audit worktree is removed before phase 3 begins. Sequential add/remove rather
than two live worktrees keeps issue 036's collision class from arising at all.

**Phase 2 — diff.** Load and validate the manifest, load the ledger, `diff_findings`,
`apply_diff`, save the ledger. Pure and cheap.

**Phase 3 — fix.** Dispatch set = `(new ∪ regressed)[:max_findings_per_dispatch]`,
minus any whose `attempts >= max_attempts_per_target` (those are recorded as
`suppressed`, listed in the report, and counted in `extra`). If the set is empty
after suppression, no worker. Otherwise **exactly one** fix worker, on
`auto/<job>-<run_id>` via `build_branch_name`, in
`<repo>/.worktrees/audit-fix-<run_id>`, with an engine-injected prompt that is
`auto_fix.build_base_fix_prompt`'s sibling (`auto_fix.py:634`) carrying the finding
table instead of gate output, plus the `check` and the audit command so the worker
can re-derive the full instance list behind a collapsed derived ID.

One worker per tick, not one per finding, for two reasons. `cli._check_systemd_timeout`'s
base-target arm (`cli.py:870`) is `start + GATE_SLOP_S + Σ check.timeout_ms +
timeout_ms` — a single unscaled `timeout_ms`. A per-finding worker would need
`max_workers × timeout_ms` and that arm would have to grow. And one worker means one
branch and one PR per cycle, which is a thing a human can actually review.

### History, report, and notification

Per-tick terminal record: `gate` ∈ `"baseline" | "passed" | "fix_dispatched" |
"suppressed" | "failed"` (a new value set beside 025's `"passed" | "failed"`),
`target: "base"`, `check`, `manifest_path`, `ledger_path`, `report_path`,
`report_written`, `gate_branch` (only when a worker ran), `duplicate_ids`, and the
counts `findings_total`, `new`, `regressed`, `unchanged`, `resolved`, `dispatched`,
`suppressed`, `adopted`. `suppressed_ids` is capped at the first 20 so a
pathological ledger cannot bloat a history line.

**Per-finding attempt detail lives in the ledger, not in history.** History is the
run log; the ledger is the finding ledger. Per-finding rows in `history.jsonl` would
grow it without bound, and `history` is already rotated and pruned (issue 021).

`reports/<run_id>.md` is the aggregate: the counts, then one line per finding with
its ID, severity, location, and disposition (`new` / `regressed` / `suppressed` /
`unchanged`), then the resolved ones. Every finding appears, including the ones the
job deliberately did not act on — that list is the only record of known debt the
operator does not owe a PR for.

Notification mapping, through `_notify_gate` (`tick.py:2100`) so issue 009's
hierarchy is honored unchanged: `baseline` / `fix_dispatched` / `suppressed` →
`finding`; `passed` → `success`; `failed` → `failure`. With
`notify_policy: on-finding` — which the shipped example sets — a clean night is
**silent**, which is the property the bullet is really asking for.

`prune_ledger(ledger, *, now, retention_days)` drops only `state: resolved` entries
whose `resolved_at` is older than `ledger_retention_days`. Open entries are never
pruned: pruning them is what would destroy the "it came back" regression signal.

### Files touched

`src/herdr_routines/config.py` (`VALID_JOB_KINDS:86` += `audit`, the `Job.kind`
Literal:305, a frozen `AuditSpec`, the new `Job` fields, and the `kind: audit`
validation block), **new** `src/herdr_routines/findings.py` (the pure half:
`Finding`, `FindingDiff`, `Ledger`, `normalize_location`, `finding_id`,
`load_findings_manifest`, `diff_findings`, `apply_diff`, `load_ledger`,
`save_ledger`, `prune_ledger` — no herdr, gh, or git imports),
`src/herdr_routines/tick.py` (`_process_audit_job` as a sibling of
`_process_gated_job:168` reusing its registered / stale / live-agent / `decide`
preamble verbatim, plus the `kind: audit` branch at `_process_job:1281`, the audit
and fix phase functions, and the prompt builders),
`src/herdr_routines/runner.py` (`$ROUTINE_FINDINGS` in `substitute_prompt:388`),
`src/herdr_routines/auto_fix.py` (`build_audit_fix_prompt`, a sibling of
`build_base_fix_prompt:634`), `src/herdr_routines/cli.py` (the audit arm in
`_check_systemd_timeout:819`; `status`/`digest` read `job.kind` already, so adding
a kind there is a string table), **new** `deploy/jobs.d/audit-type-health.yaml`,
**new** `tests/test_findings.py` and `tests/test_kind_audit.py`, and `ROADMAP.md`.

### Ordering against issue 055

[Issue 055](055-review-me-prs-across-repos.md) (open) adds `kind: review`; this adds
`kind: audit`. Resolved, because it reads like an unresolved question and isn't:

- **There is no implementation dependency, in either direction.** Neither issue
  reads, imports, or requires anything the other introduces. 055 is about
  discovering PRs and posting a review; this is about running a check in a worktree
  and acting on a finding. A worker can implement either one without the other's PR
  existing. This is a shared-*surface* conflict, not a dependency.
- **The shared surface is three lines**, all additive: `VALID_JOB_KINDS`
  (`config.py:86`), the `Job.kind` `Literal` (`config.py:305`), and one `if` in
  `_process_job`'s branch chain (`tick.py:1281`). Whichever lands second adds its
  value and its branch after the other's; no rebase of *work* is needed, only of
  those three lines.
- **Therefore: do not wait for 055, and do not require 057 to land first.** 055's
  claim to be "the fourth value of the `kind` literal" is only true if it lands
  first; if 057 lands first, that sentence in 055 is what gets edited. A conflict
  there is bookkeeping, not a design disagreement, and is not a reason to hold
  either issue.

### Non-goals

- No per-finding PRs. One worker, one branch, one PR per cycle.
- No suppression or dismissal UI. `state: resolved` is the whole lifecycle; there is
  no "accepted risk" state. A finding whose fix a human declines to make stays
  `open`, keeps appearing in the report, and stops consuming budget once the cap is
  reached — that is the intended pressure, not a bug.
- No cross-job ledger merging. `type-health` and `ui-ux-review` noticing the same
  file is two independent signals and stays two.
- Never diff two stored reports instead of re-running the audit. A diff of two
  reports is only as fresh as the older one; the audit re-runs every cycle.
- No `fitted`-specific skill invocation in the engine. The engine names the skill;
  the skill lives in the target repo's own opencode config. The same job file works
  against any repo with a comparable skill.
- `audit.command`'s exit code is never the verdict.
- Not a replacement for `babysit-prs` (015). Audit fix PRs are `auto/` branches and
  land in its lap; it owns their CI and thread fixes. The audit worktree is removed
  after the worker settles, so there is no retained-branch collision (issue 036).

### Follow-up, not in this issue

**Hardening the seven audit skills to emit a stable `id`** is its own piece of work
and is deliberately not here. The engine's derived-ID fallback is correct and safe on
its own — it under-dispatches, never over-dispatches — so nothing is blocked on it.
But that fallback collapses same-kind findings within one file into a single
dispatch, so weekly PRs will be coarser and slightly more expensive to land than
they need to be. The skills (`type-health`, `ui-ux-review`, `accessibility-review`,
`fix-ignores`, `discover-conventions`, `improve-codebase-architecture`,
`api-gap-audit`) live in the target repo, not here, so this issue can specify the
manifest they must emit but cannot change what they emit. Track the skill-side work
  separately, and only after this has a ledger holding real findings — until then
  nobody knows which skills actually need an `id`, because a skill whose findings are
  all high-severity and unique per file never notices the collapse.
- **2026-10-01 (refine review, pass 3, `confidence: medium` — cap reached, not
  consensus):** two notes remain open and are carried to the PR body rather than
  fixed here, because resolving either one properly needs another author/reviewer
  pass and the loop is capped at three. (a) Criterion 9 tests write-ahead ordering
  via "a dispatch that raises still leaves the incremented ledger on disk", which
  cannot be exercised while criterion 16 forbids phase A from dispatching; the
  layering between the pure `apply_diff` and a tick-level integration test is not
  spelled out. (b) The new `gate` value set and the ~12 history `extra` fields are
  specified in Design but have no numbered criterion asserting the record shape.
  A third note — that the Scope section's criterion ranges had gone stale against the
  renumbering — was a plain internal inconsistency and is fixed in this revision.

## Acceptance criteria

**Phase A — config, manifest, and the pure diff/ledger half (no agent, no PR)**

1. `kind: audit` loads with exactly one of `audit.skill` / `audit.command`, positive
   `audit.timeout_ms`, `max_findings_per_dispatch` ≥ 1, `max_attempts_per_target` ≥ 1,
   `ledger_retention_days` ≥ 1, `workspace: worktree`; and `checks`, `workspace: root`,
   `target` other than `base`, and `max_workers_per_tick` are each rejected at load
   with the existing message shape. `fix_prompt` is not inherited via `defaults.yaml`.
   Test: `test_audit_job_config_validation`
2. `normalize_location` drops `:line` and `:line:col`; `finding_id` is unchanged by a
   line shift and differs across `check`, `kind`, and file. Test:
   `test_finding_id_ignores_line_numbers`
3. A skill-supplied `id` is used verbatim; two findings colliding on a derived ID
   collapse to one entry at the higher severity and the collapse is counted in
   `duplicate_ids` rather than failing the manifest. Test:
   `test_derived_id_collision_collapses_to_highest_severity`
4. A manifest that is missing, unparseable, `version != 1`, or contains a malformed
   finding returns `None` — never `[]` — and the tick records
   `failed (findings_manifest_invalid)` with zero dispatches. Test:
   `test_manifest_validation_fails_closed`
5. `diff_findings` classifies all five rows of the table above, including a
   `resolved` finding reappearing as `new` with its original `first_seen`, and a
   severity *decrease* as `unchanged`. Test: `test_diff_classifies_new_regressed_resolved`
6. `diff_findings` output is totally ordered by `(-severity, location, kind, id)`, so
   a cap drops the least severe and two ticks over one manifest pick the same set.
   Test: `test_diff_orders_by_severity_for_the_cap`
7. A first cycle with no ledger and `adopt_baseline: true` records every finding as
   `open` / `attempts: 0` and dispatches nothing; the record is `done` with
   `gate: "baseline"` and `adopted: N`. The same cycle with `adopt_baseline: false`
   produces a dispatch set. Test: `test_cold_start_adopts_baseline_without_dispatch`
8. An unparseable ledger is `failed (ledger_corrupt)` with zero dispatches, no
   silent re-baseline, and the bad file copied aside before the failure is recorded.
   Test: `test_corrupt_ledger_fails_closed_without_rebaseline`
9. The ledger is written before dispatch: `apply_diff` increments `attempts` for
   exactly the dispatched IDs, leaves suppressed ones untouched, and a dispatch that
   raises still leaves the incremented ledger on disk. Test:
   `test_ledger_write_ahead_consumes_budget_on_failed_dispatch`
10. A finding at `attempts >= max_attempts_per_target` is excluded from the dispatch
    set and reported as `suppressed`; the tick is `done` with `gate: "suppressed"`,
    never `failed`, and the finding is not re-dispatched on the next cycle. Test:
    `test_suppressed_finding_stops_redispatch`
11. `prune_ledger` removes only `state: resolved` entries older than
    `ledger_retention_days`; open entries are never pruned. Test:
    `test_ledger_prunes_only_resolved_entries`
12. An empty diff is `done` with `gate: "passed"`, zero dispatches, and **no
    notification** under `notify_policy: on-finding`. Test:
    `test_clean_audit_night_is_silent`
13. `save_ledger` is atomic (a pre-existing ledger survives a mid-write exception) and
    resolves under the state dir, not `reports_dir` — so the mtime-based
    `reports_prune` can never reach it. Test:
    `test_ledger_write_is_atomic_and_outside_reports_dir`
14. `substitute_prompt` resolves a new `$ROUTINE_FINDINGS` placeholder to the
    manifest path alongside `$ROUTINE_REPORT` / `$ROUTINE_JOB` / `$ROUTINE_RUN_ID`,
    and a prompt carrying none of them is passed through unchanged. It is a pure
    function, so this belongs in phase A and needs no agent. Test:
    `test_substitute_prompt_resolves_findings_path`
15. The shipped `deploy/jobs.d/audit-type-health.yaml` loads through `load_config_dir`
    and asserts `kind: audit`, `enabled: false`, and exactly one of skill/command.
    Test: `test_shipped_audit_job_config_validates`
16. **The phase seam.** A tick that runs in phase A computes and *records* the full
    dispatch set — every new, regressed and suppressed finding with its ID and its
    disposition, in the report and in `extra` — and dispatches **no agent, starts no
    pane, and creates no branch**, whatever the diff contains. This is what keeps
    phase A honest: a worker who ships early-dispatching code has not split the
    issue, and a reader can see from one phase-A report exactly what phase B would
    have done. Test: `test_phase_a_records_dispatch_set_without_dispatching`

**Phase B — agent audit and fix dispatch (issue 058, filed from the text below)**

*Criteria 17–22 are **not** met by this issue and are not part of its scope. They
are carried here so 058 can be filed from this text without re-deriving the design,
and this file closes at phase A with them unmet — the same arrangement 054 used for
the criteria that became 056. When 058 is filed, add a log line here pointing at
it.*

17. With `audit.command`, exit `127` is `failed (audit_command_failed)`; **every other
    exit code, non-zero included, is a successful audit** whose manifest is the
    verdict. Test: `test_audit_command_exit_code_is_not_the_gate`
18. With `audit.skill`, an audit agent starts in a fresh `audit-<run_id>` worktree at
    `base`; the injected prompt names the skill, carries both `$ROUTINE_REPORT` and
    `$ROUTINE_FINDINGS` and the no-commit clause; and the worktree is gone before the
    fix phase begins. Test: `test_audit_skill_dispatch_uses_its_own_worktree`
19. Exactly one fix worker is dispatched per tick with the capped finding table on
    `auto/<job>-<run_id>`, and `_check_systemd_timeout`'s audit arm is
    `start + GATE_SLOP_S + audit.timeout_ms + timeout_ms`. Test:
    `test_audit_dispatches_one_fix_worker_and_budgets_it`
20. The fix prompt carries each dispatched finding's ID, severity, location and
    summary, plus the `check` and the audit command so the worker can enumerate every
    instance behind a collapsed derived ID; a job-level `fix_prompt` overrides it.
    Test: `test_audit_fix_prompt_carries_findings_and_recheck_command`
21. A live `rt-<job>-au<h8>` audit agent or `rt-<job>-gate-<h8>` fix agent blocks a
    second one across ticks, matched by prefix; the name is ≤ 32 chars for a 24-char
    job name. Test: `test_audit_live_agent_prefix_guard`
22. `kind: audit` changes nothing for existing jobs: the whole of
    `tests/test_kind_gated.py`, `tests/test_auto_fix.py` and `tests/test_tick.py` pass
    unmodified, and no shipped job's config or dispatch path is altered. Test:
    `test_audit_does_not_change_existing_job_dispatch` (plus the three existing suites)

## Why these tests

- 1 is the config surface, and the four rejections are the ones that would otherwise
  be silently ignored — a job with a `max_workers_per_tick` that does nothing is the
  kind of thing that bites a month later.
- 2, 3 pin finding identity, which is the whole design. If a line shift changes an
  ID, this job opens duplicate PRs forever, and no other test would catch it.
- 4, 8 pin the two fail-closed paths. A malformed manifest read as "no findings" and
  a corrupt ledger read as "no ledger" are both silent successes, and both are the
  failure this system has been repeatedly bitten by.
- 5, 6 pin the classification and the ordering, which together decide what the job
  believes is new. They are pure functions, so they get the most tests for the least
  ceremony.
- 7, 10 pin the two brakes — the baseline adoption and the budget — that are the
  difference between a scheduled audit and a PR storm. 10 also pins that suppression
  is a `done`, not a `failed`: a permanently-unfixable finding must not turn a weekly
  job into a weekly alarm.
- 9, 13 pin the crash cases. Budget consumed on intent rather than on success, and
  an atomic write, are what make the ledger trustworthy enough to be the regression
  signal.
- 11 pins that pruning cannot eat the regression memory.
- 12 pins the property the bullet is actually asking for: a clean night costs zero
  and says nothing.
- 14 is in phase A on purpose: the placeholder is a pure string substitution, and
  leaving it to phase B would put a testable unit behind a gate it does not need.
- 16 pins the seam itself. Every other phase-A criterion could be satisfied by code
  that also dispatches, which would quietly make the split a fiction; this one
  cannot.
- 17 is the acceptance criterion that most directly encodes "a report is not a
  check." It is the one that would be written wrong by someone pattern-matching on
  025.
- 18, 19, 20, 21 pin the agent mechanics: worktree lifetime, the one-worker budget,
  the prompt's ability to act, and the cross-tick double-dispatch guard (issue 036c's
  lesson).
- 22 is the regression budget. The new code touches four shared modules; no existing
  job's behavior may move.

## Log

- **2026-10-01:** refined from the ROADMAP Parking Lot bullet
  "**Audit skills as report→diff gate jobs**" (2026-08-30 brainstorm), selected by
  `herdr-routines refine-issue` under issue 029. The bullet's stated prerequisite —
  issue 025's unified gate design, merged in PR #56 — has shipped, so this is
  designed rather than re-gated. The bullet's open question ("idea, not designed")
  is the report→boolean converter, and the answer is a diff, which is what forces
  the three things 025 has no notion of: stable finding identity, a per-finding
  budget that survives across cycles (025's is per-occurrence by construction), and
  a baseline-adoption step so the first tick is a report and not a storm. Those three
  are the design; the rest is plumbing. `kind: audit` is a new dispatch branch rather
  than a new check kind so `run_checks` stays pure; the one deliberate divergence
  from 025 is that a non-zero `audit.command` is a successful audit. Ships in two
  phases, and the first one moves no agent.
- **2026-10-01 (refine review, pass 1, `confidence: medium`):** five notes applied.
  (a) Scope made explicit — this issue is *done* at phase A, and phase B is to be
  filed as 058 once A has run, following the 054/056 split; recorded as a `gate:`
  field, the way 054 points at 056. (b) Source citations
  demoted to annotations with the symbol list given as the thing to rely on, since
  the line numbers drift the moment 055 lands. (c) The 055 ordering note resolved
  rather than deferred: no implementation dependency in either direction, three
  additive touch points, and explicitly no reason to hold either issue. (d) A
  `$ROUTINE_FINDINGS` criterion added — and moved *into phase A* rather than left to
  phase B, because `substitute_prompt` is a pure function that had no business
  waiting behind an agent. Criteria and the "Why these tests" numbering renumbered
  to match.
- **2026-10-01 (refine review, pass 2, `confidence: medium`):** five more notes
  applied. (a) The `gate:` line's criterion range was stale after the renumber —
  corrected to 1–16. (b) A real hole: nothing asserted the phase seam, so a worker
  could satisfy every phase-A criterion with code that already dispatches. Added
  criterion 16, which requires a phase-A tick to record the full dispatch set and
  dispatch nothing. (c) `notify_policy: on-finding` cited against issue 009's
  existing enum so it is visibly not a new config value. (d) Phase B's criteria are
  now explicitly marked as unmet-by-this-issue, carried forward for 058 to be filed
  from — matching how 054 left the criteria that became 056, rather than leaving this
  file closing with criteria it looks like it skipped. (e) Added a "Follow-up, not in
  this issue" note giving the skill-side `id` hardening an explicit owner and a
  reason to wait for real ledger data before scoping it.
