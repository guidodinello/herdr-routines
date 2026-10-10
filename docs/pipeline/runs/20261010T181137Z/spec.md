# Spec v2 — 20261010T181137Z — Issue 057 Phase A (criteria 1–16)

Issue: `docs/process/issues/057-audit-skills-as-report-diff-gate-jobs.md`.
Scope is Phase A only. Phase B (criteria 17–22) is explicitly out of scope; filed as 058 after Phase A lands and has run. Do not implement dispatch in this run.

v1 → v2 changes are listed in [`## Changelog v1→v2`](#changelog-v1v2).

## Problem

Hand-run audit skills (`type-health`, `ui-ux-review`, `accessibility-review`,
`fix-ignores`, `discover-conventions`, `improve-codebase-architecture`,
`api-gap-audit`) are one-shots: agent writes a report, report rots, nothing
re-runs it, nothing notices a finding came back, nothing carries it into a PR.
Issue 025's gate (`auto_fix.run_checks`: `exit_code == 0` → boolean) shipped
(PR #56) but does not supply the report→boolean converter — an audit produces a
document, not an exit code. The honest converter is a diff against the previous
cycle's report. That diff introduces three things 025 has no notion of:

1. **Stable finding identity.** Key by text → reword re-arms gate, duplicate
   PRs. Key by line number → unrelated edits shift lines, duplicate PRs. The ID
   rule is load-bearing.
2. **Per-finding retry budget across cycles.** 025's budget
   (`auto_fix.attempt_count_for_gate_branch`, keyed on `gate_branch` =
   `auto/<job>-<run_id>` via `runner.build_branch_name`) is a new key every
   cron fire — correct for `ruff`, wrong for a weekly audit finding that is the
   *same* occurrence until fixed. Needs a persisted ledger.
3. **First-cycle storm.** With no history every existing finding is "new" — a
   real backlog opens N PRs on tick 1. Needs baseline adoption.

## Approach

Ship Phase A: config + manifest contract + pure diff/ledger half. Dispatches no
agent, starts no pane, creates no branch. First real run yields a readable
ledger + report the operator inspects before anything opens a PR.

- **New `kind: audit`** (not a new check kind): one more value of the `kind`
  literal + one branch in `tick._process_job` beside `pipeline`/`gated`.
  Rejected alternative (`checks: [{skill: ...}]`) because `run_checks` is
  synchronous/shell-shaped and audit needs worktree + pane + prompt +
  watchdog — that machinery lives in `tick._process_base_target`.
- **Config** (rejected at load with existing `pipeline`/`gated` message shape):
  `audit` mapping with exactly one of `skill`/`command`;
  `audit.timeout_ms` positive int; `max_findings_per_dispatch ≥ 1`,
  `max_attempts_per_target ≥ 1`, `ledger_retention_days ≥ 1`;
  `workspace: worktree` required; `checks`, `workspace: root`,
  `target` other than `base`, `max_workers_per_tick` rejected;
  `fix_prompt` not inheritable via `defaults.yaml`. Ships off
  (`enabled: false`, Mondays 04:00, `adopt_baseline: true`,
  `notify_policy: on-finding` — existing issue-009 enum, silent on clean night).
- **Findings manifest** `<reports_dir>/<run_id>-findings.json`
  (`version: 1`, `check`, `findings[{id?, kind, severity ∈ low/medium/high,
  location, summary}]`), new `$ROUTINE_FINDINGS` placeholder in
  `runner.substitute_prompt` alongside `$ROUTINE_REPORT`. `load_findings_manifest`
  returns `list[Finding] | None`; `None` (missing/unparseable/`version != 1`/
  malformed element/bad `id`) means unverifiable, never `[]` — tick records
  `failed (findings_manifest_invalid)`, zero dispatches. Duplicate derived IDs
  collapse to one entry at higher severity, counted in `extra["duplicate_ids"]`.
- **Identity:** supplied `id` used verbatim (trimmed, non-empty, ≤128 chars);
  else `sha1(f"{check}|{kind}|{normalize_location(location)}")[:12]` where
  normalize strips whitespace and trailing `:line` / `:line:col`. Deliberate
  under-dispatch: same-kind/same-file findings share one ID; fix worker gets
  file + kind + recheck command and enumerates instances. Supplying structural
  `id`s is the recommended skill-side follow-up (not this issue).
- **Ledger** `<state>/findings/<job-name>.json` (`$HERDR_PLUGIN_STATE_DIR` else
  `~/.local/state/herdr-routines`, cf. `config.default_repos_dir`) — explicitly
  not under `reports_dir` so `reports_prune.prune_reports` (mtime-based) can
  never eat it. Atomic `save_ledger` (write `.tmp` + `os.replace`). Corrupt →
  `failed (ledger_corrupt)`, zero dispatches, bad file copied to
  `<job>.json.corrupt-<ts>`, no silent re-baseline. Absent = cold start.
- **Diff** (pure, in new module; bulk of tests; no herdr/gh/git):
  `diff_findings(findings, ledger, *, check) → FindingDiff(new, regressed,
  unchanged, resolved)` + `apply_diff(ledger, diff, *, now, run_id,
  dispatched_ids)`. Table: absent→present = `new`; `resolved`→present = `new`
  (preserve `first_seen`); `open` + higher severity = `regressed`; same/lower =
  `unchanged`; `open` + absent = `resolved`. Total order
  `(-severity, location, kind, id)` so cap drops least severe deterministically.
- **Cold start:** no ledger + `adopt_baseline: true` → record all as
  `open`/`attempts: 0`, dispatch nothing, `done` with `gate: "baseline"`,
  `extra.adopted = N`, notify `finding`. `false` → immediate dispatch set
  (bounded by cap + budget).
- **Write-ahead:** ledger written after diff, before dispatch; `attempts += 1` +
  `last_dispatched_run = run_id` for exactly dispatched IDs. Crash/raise still
  consumes budget (over-count > under-count). Suppressed (`attempts >=
  max_attempts_per_target`) excluded, reported, `gate: "suppressed"`, never
  `failed`, never re-dispatched.
- **Phase seam (criterion 16):** Phase-A tick computes and *records* the full
  dispatch set (new/regressed/suppressed IDs + dispositions in report and
  history `extra`) and dispatches nothing. Gates recorded:
  `baseline | passed | fix_dispatched | suppressed | failed`; clean diff =
  `done`/`passed`, no notification under `on-finding`. Report lists every
  finding incl. deliberately-unacted-on debt. `prune_ledger` drops only
  `resolved` older than retention; open never pruned.
- **Tests 1–16**, no herdr server / `gh` / git (per issue's test names).

## Files touched

- `src/herdr_routines/config.py` — `VALID_JOB_KINDS` += `audit`, `Job.kind`
  Literal, frozen `AuditSpec`, new `Job` fields, `kind: audit` validation block.
- **New** `src/herdr_routines/findings.py` — `Finding`, `FindingDiff`,
  `Ledger`, `normalize_location`, `finding_id`, `load_findings_manifest`,
  `diff_findings`, `apply_diff`, `load_ledger`, `save_ledger`, `prune_ledger`
  (no herdr/gh/git imports).
- `src/herdr_routines/tick.py` — Phase-A portion only: `kind: audit` branch in
  `_process_job`, `_process_audit_job` record-only path (diff + ledger +
  report + history `extra`, no agent/pane/branch). Full agent/fix phases
  deferred to 058.
- `src/herdr_routines/runner.py` — `$ROUTINE_FINDINGS` in `substitute_prompt`.
- **New** `deploy/jobs.d/audit-type-health.yaml` — shipped example, disabled.
- **New** `tests/test_findings.py`, `tests/test_kind_audit.py`.
- `ROADMAP.md` — note Phase A landing.
- Deferred to Phase B / 058 (not touched here): `auto_fix.build_audit_fix_prompt`,
  `cli._check_systemd_timeout` audit arm, audit/fix worktree + pane wiring.

## Risks

- **Derived-ID collapse is coarse:** same kind + same file = one dispatch. Safe
  direction (under- not over-dispatch) but weekly PRs coarser until skills emit
  structural `id`s (tracked follow-up, needs real ledger data first).
  [blocking] confidence: medium
- **Write-ahead over-counts** on failed dispatch (by design); suppressed set is
  terminal — a declined-as-risk finding stays `open` in the report but stops
  consuming budget. Intended pressure, may surprise. [blocking] confidence: medium
- **Fail-closed paths must stay fail-closed:** manifest-`None`-as-`[]` or
  corrupt-ledger-as-absent are both silent successes this system has been
  bitten by; tests 4/8 pin them. [blocking] confidence: high
- **Shared surface with issue 055 (`kind: review`):** three additive lines
  (`VALID_JOB_KINDS`, `Job.kind` Literal, `_process_job` branch). No
  implementation dependency either direction; whoever lands second rebases
  bookkeeping only. Source `file.py:123` citations are annotations, will drift.
  [non-blocking] confidence: high
- **Known open notes from refine review (carried, not fixed):** (a) criterion 9
  wants a tick-level "dispatch raises yet ledger incremented" test while
  criterion 16 forbids Phase-A dispatch — layering between pure `apply_diff`
  test and integration test underspecified; (b) gate value set + ~12 history
  `extra` fields specified in Design with no numbered record-shape criterion.
  [non-blocking] confidence: medium
- **Per-run spec path** (`docs/pipeline/runs/<run-id>/spec.md`) used
  deliberately to avoid PR #29-vs-#28 shared-path merge conflict (G-15).
  [non-blocking] confidence: high

## Acceptance criteria

Phase A — config, manifest, and the pure diff/ledger half (no agent, no PR).
Severity tiers: `blocking` = the feature does not do what issue 057 Phase A
asked, or does it in a way the next stage cannot observe; `non-blocking` =
correctness of follow-on details that must not gate Phase A. Each item carries
a `confidence:` rating for the reviewer's certainty in the pin.

1. `kind: audit` loads with exactly one of `audit.skill` / `audit.command`,
   positive `audit.timeout_ms`, `max_findings_per_dispatch` ≥ 1,
   `max_attempts_per_target` ≥ 1, `ledger_retention_days` ≥ 1,
   `workspace: worktree`; `checks`, `workspace: root`, `target` other than
   `base`, and `max_workers_per_tick` are each rejected at load with the
   existing message shape, and `fix_prompt` is not inherited via
   `defaults.yaml`. blocking — the config surface; silently-ignored caps bite
   a month later. confidence: high. Test: `test_audit_job_config_validation`
2. `normalize_location` drops `:line` and `:line:col`; `finding_id` is unchanged
   by a line shift and differs across `check`, `kind`, and file. blocking —
   identity is the whole design; a line-sensitive ID opens duplicate PRs
   forever. confidence: high. Test: `test_finding_id_ignores_line_numbers`
3. A skill-supplied `id` is used verbatim; two findings colliding on a derived
   ID collapse to one entry at the higher severity and the collapse is counted
   in `duplicate_ids` rather than failing the manifest. blocking — pins
   deliberate under-dispatch over brittle whole-report rejection.
   confidence: high. Test: `test_derived_id_collision_collapses_to_highest_severity`
4. A manifest that is missing, unparseable, `version != 1`, or contains a
   malformed finding returns `None` — never `[]` — and the tick records
   `failed (findings_manifest_invalid)` with zero dispatches. blocking — a
   malformed manifest read as "no findings" is a silent success.
   confidence: high. Test: `test_manifest_validation_fails_closed`
5. `diff_findings` classifies all five table rows, including a `resolved`
   finding reappearing as `new` with its original `first_seen`, and a severity
   decrease as `unchanged`. blocking — decides what the job believes is new.
   confidence: high. Test: `test_diff_classifies_new_regressed_resolved`
6. `diff_findings` output is totally ordered by `(-severity, location, kind,
   id)`, so a cap drops the least severe and two ticks over one manifest pick
   the same set. blocking — makes the dispatch cap deterministic.
   confidence: high. Test: `test_diff_orders_by_severity_for_the_cap`
7. A first cycle with no ledger and `adopt_baseline: true` records every finding
   as `open` / `attempts: 0` and dispatches nothing; the record is `done` with
   `gate: "baseline"` and `adopted: N`, while `adopt_baseline: false` produces
   a dispatch set. blocking — the admission control that prevents a 200-PR
   first-cycle storm. confidence: high. Test: `test_cold_start_adopts_baseline_without_dispatch`
8. An unparseable ledger is `failed (ledger_corrupt)` with zero dispatches, no
   silent re-baseline, and the bad file copied aside before the failure is
   recorded. blocking — corruption read as "no ledger" re-baselines forever
   and quietly never fixes anything. confidence: high. Test: `test_corrupt_ledger_fails_closed_without_rebaseline`
9. The ledger is written before dispatch: `apply_diff` increments `attempts` for
   exactly the dispatched IDs, leaves suppressed ones untouched, and a dispatch
   that raises still leaves the incremented ledger on disk. blocking — budget
   consumed on intent rather than success is what makes the ledger trustworthy.
   confidence: medium. Test: `test_ledger_write_ahead_consumes_budget_on_failed_dispatch`
10. A finding at `attempts >= max_attempts_per_target` is excluded from the
    dispatch set and reported as `suppressed`; the tick is `done` with
    `gate: "suppressed"`, never `failed`, and the finding is not re-dispatched
    on the next cycle. blocking — suppression must be a `done`, not a weekly
    alarm. confidence: high. Test: `test_suppressed_finding_stops_redispatch`
11. `prune_ledger` removes only `state: resolved` entries older than
    `ledger_retention_days`; open entries are never pruned. blocking — pruning
    open entries destroys the "it came back" regression signal.
    confidence: high. Test: `test_ledger_prunes_only_resolved_entries`
12. An empty diff is `done` with `gate: "passed"`, zero dispatches, and no
    notification under `notify_policy: on-finding`. blocking — a clean night
    costs zero and says nothing, the property the bullet asks for.
    confidence: high. Test: `test_clean_audit_night_is_silent`
13. `save_ledger` is atomic (a pre-existing ledger survives a mid-write
    exception) and resolves under the state dir, not `reports_dir`, so the
    mtime-based `reports_prune` can never reach it. blocking — a torn write
    must not eat the regression signal. confidence: medium. Test: `test_ledger_write_is_atomic_and_outside_reports_dir`
14. `substitute_prompt` resolves a new `$ROUTINE_FINDINGS` placeholder to the
    manifest path alongside `$ROUTINE_REPORT` / `$ROUTINE_JOB` /
    `$ROUTINE_RUN_ID`, and a prompt carrying none of them passes through
    unchanged; pure function, no agent needed. non-blocking — string
    substitution that belongs in Phase A so Phase B needs no rework.
    confidence: high. Test: `test_substitute_prompt_resolves_findings_path`
15. The shipped `deploy/jobs.d/audit-type-health.yaml` loads through
    `load_config_dir` and asserts `kind: audit`, `enabled: false`, and exactly
    one of skill/command. blocking — the shipped example is the contract new
    operators copy. confidence: high. Test: `test_shipped_audit_job_config_validates`
16. The phase seam: a Phase-A tick computes and records the full dispatch set —
    every new, regressed and suppressed finding with its ID and disposition in
    the report and in `extra` — and dispatches no agent, starts no pane, and
    creates no branch, whatever the diff contains. blocking — without it every
    other criterion could pass with early-dispatch code and the split is
    fiction. confidence: high. Test: `test_phase_a_records_dispatch_set_without_dispatching`

## Changelog v1→v2

Stage 2 (independent spec review) over v1. No change to the scope decision
(Phase A criteria 1–16 only; Phase B 17–22 out of scope for 058), the problem
statement, or the approach — v1's design was verified against
`docs/process/issues/057-audit-skills-as-report-diff-gate-jobs.md` and kept.

Additions:

- `## Acceptance criteria` — 16 numbered items mirroring issue 057 Phase A
  criteria 1–16, each ending `Test: <exact function name>` using the issue's
  verbatim test names (`test_audit_job_config_validation` through
  `test_phase_a_records_dispatch_set_without_dispatching`), so stage 3 has no
  name to invent.
- Severity and certainty tiers — every acceptance item and every risk now
  carries a `blocking` / `non-blocking` label plus a `confidence: high|medium`
  rating; tier semantics defined at the top of `## Acceptance criteria`.
- This `## Changelog v1→v2` section.

Corrections to v1:

- Title `v1` → `v2` with a pointer to this changelog.
- `## Risks` items annotated with `[blocking]` / `[non-blocking]` and
  `confidence:` ratings (v1 had neither); no risk content changed.
- No files-touched, approach, or scope changes: review confirmed the Phase-A
  file list (`config.py`, new `findings.py`, `tick.py` record-only path,
  `runner.py` placeholder, shipped `audit-type-health.yaml`, two new test
  files, `ROADMAP.md`) matches the issue's Phase A contract.
