---
id: "058"
title: "Audit jobs phase B: run the audit and dispatch one fix worker (issue 057 criteria 17-22)"
status: done
priority: medium
area: pipeline
gate: issue 057 phase A (PR #156) merged; the diff/ledger half and the record-only tick exist
---

## Description

[Issue 057](057-audit-skills-as-report-diff-gate-jobs.md) split `kind: audit` in two.
Phase A (PR #156) shipped the config, the findings-manifest contract, the pure
diff/ledger half (`findings.py`) and a tick that *records* what it would dispatch.
It runs no audit, so nothing ever writes the manifest and an enabled job fails
every cycle with `findings_manifest_invalid`.

This is phase B, filed from 057's criteria 17-22: run the audit itself, then
dispatch exactly one fix worker for the capped set of new/regressed/queued
findings. The design is 057's "The three phases" section; it is not restated
here. What follows is only what phase B decides that 057 left open.

## Decisions beyond 057

- **Three report paths, not one.** 057 hands the audit agent `$ROUTINE_REPORT`
  and also calls `reports/<run_id>.md` the engine's aggregate. Those would be the
  same file. The audit agent writes `reports/<run_id>-audit.md`, the fix worker
  writes `reports/<run_id>-fix.md`, and the engine owns `reports/<run_id>.md`.
- **The engine prompt carries the manifest schema.** No audit skill emits the
  manifest today. The injected audit prompt names the skill and spells out the
  JSON shape, so a skill runs unmodified; a skill-supplied `id` stays the
  recommended follow-up (057, "Follow-up").
- **`audit.command` gets the paths twice.** `$ROUTINE_FINDINGS` / `$ROUTINE_REPORT`
  are substituted into the command string and also exported as environment
  variables of the same name, so a script can read either.
- **The systemd arm counts two agent starts for a skill audit**
  (`2 × start + slop + audit.timeout_ms + timeout_ms`): the audit agent and the fix
  worker each wait up to `start_timeout_ms`. A command audit counts one.
- **The ledger is written before anything that can fail on the dispatch path**
  (repo sync, worktree add, pane, agent start), so every dispatch failure has
  already consumed its attempt. Criterion 9 of 057 could only test this against a
  fake raise; phase B tests it through the real tick.
- **The live-agent guard matches truncated names.** For a 24-char job,
  `build_gate_worker_agent_name` truncates to `rt-<job>-gate` with no hash, so the
  guard matches the prefixes `rt-<job>-au` and `rt-<job>-gate` (each cut to 32
  chars), not a regex that requires hex. Audit jobs only: the shared
  `_WORKER_NAME_SUFFIX_RE` that drives reboot reaping for every job is unchanged.

## Acceptance criteria

1. With `audit.command`, exit `127` is `failed (audit_command_failed)`; **every
   other exit code, non-zero included, is a successful audit** whose manifest is
   the verdict. Test: `test_audit_command_exit_code_is_not_the_gate`
2. With `audit.skill`, an audit agent starts in a fresh `audit-<run_id>` worktree
   at `base`; the injected prompt names the skill, carries both `$ROUTINE_REPORT`
   and `$ROUTINE_FINDINGS` and the no-commit clause; and the worktree is gone
   before the fix phase begins. Test: `test_audit_skill_dispatch_uses_its_own_worktree`
3. Exactly one fix worker is dispatched per tick with the capped finding table on
   `auto/<job>-<run_id>`, and `_check_systemd_timeout`'s audit arm budgets it.
   Test: `test_audit_dispatches_one_fix_worker_and_budgets_it`
4. The fix prompt carries each dispatched finding's ID, severity, location and
   summary, plus the `check` and the audit command so the worker can enumerate
   every instance behind a collapsed derived ID; a job-level `fix_prompt`
   overrides it. Test: `test_audit_fix_prompt_carries_findings_and_recheck_command`
5. A live `rt-<job>-au<h>` audit agent or `rt-<job>-gate-<h>` fix agent blocks a
   second one across ticks, matched by prefix; the name is ≤ 32 chars for a
   24-char job name. Test: `test_audit_live_agent_prefix_guard`
6. `kind: audit` changes nothing for existing jobs: `tests/test_kind_gated.py`,
   `tests/test_auto_fix.py` and `tests/test_tick.py` pass unmodified. Test:
   `test_audit_does_not_change_existing_job_dispatch` (plus the three suites)
7. A fix dispatch that fails after the ledger write (agent start raises) leaves
   the incremented attempt and `last_dispatched_run` on disk. Test:
   `test_failed_fix_dispatch_still_consumes_budget`

## Log

- **2026-10-10:** filed from issue 057's phase B criteria (17-22) after PR #156
  merged phase A. Criterion 7 replaces 057's criterion-9 test, which asserted
  write-ahead against a fake raise that never touched the ledger.
