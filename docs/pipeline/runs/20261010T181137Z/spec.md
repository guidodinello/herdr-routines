# Spec v1 — 20261010T181137Z — Issue 057 Phase A (criteria 1–16)

Issue: `docs/process/issues/057-audit-skills-as-report-diff-gate-jobs.md`.
Scope is Phase A only. Phase B (criteria 17–22) is explicitly out of scope; filed as 058 after Phase A lands and has run. Do not implement dispatch in this run.

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
- **Write-ahead over-counts** on failed dispatch (by design); suppressed set is
  terminal — a declined-as-risk finding stays `open` in the report but stops
  consuming budget. Intended pressure, may surprise.
- **Fail-closed paths must stay fail-closed:** manifest-`None`-as-`[]` or
  corrupt-ledger-as-absent are both silent successes this system has been
  bitten by; tests 4/8 pin them.
- **Shared surface with issue 055 (`kind: review`):** three additive lines
  (`VALID_JOB_KINDS`, `Job.kind` Literal, `_process_job` branch). No
  implementation dependency either direction; whoever lands second rebases
  bookkeeping only. Source `file.py:123` citations are annotations, will drift.
- **Known open notes from refine review (carried, not fixed):** (a) criterion 9
  wants a tick-level "dispatch raises yet ledger incremented" test while
  criterion 16 forbids Phase-A dispatch — layering between pure `apply_diff`
  test and integration test underspecified; (b) gate value set + ~12 history
  `extra` fields specified in Design with no numbered record-shape criterion.
- **Per-run spec path** (`docs/pipeline/runs/<run-id>/spec.md`) used
  deliberately to avoid PR #29-vs-#28 shared-path merge conflict (G-15).
