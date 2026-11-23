---
id: "088"
title: "retry_attempts is ignored by audit, gated and pipeline jobs, and retries never wait"
status: open
priority: medium
area: runner
---

## Description

Issue 008 added `retry_attempts` / `retry_on`, but only `routine_flow.py` reads them
(the loop at `routine_flow.py:256`). `audit_flow`, `gated_base_flow`, `gated_pr_flow`
and `pipeline_flow` all emit `repo_sync_failed` or `clone_failed`, which are in
`VALID_RETRY_REASONS`, yet they never retry. `config._build_retry` accepts the keys on
any job kind without a warning, so a job of one of those kinds can set
`retry_attempts: 2` and get no retries.

Real case: `audit-type-health` (kind `audit`, weekly) failed on 2026-11-16 with
`repo_sync_failed` / `Could not resolve host: github.com`. The Pi still drops some DNS
lookups (see the 2026-10-16 note on the NetworkManager `timeout:1,attempts:2` setting).
One lost lookup cost a whole week of the audit.

A second gap: the routine retry runs straight after the failed attempt, with no delay.
A network outage that lasts longer than one attempt (seconds, for a git fetch) fails
every attempt, so even routines get little out of `retry_on: [repo_sync_failed]`.

## Acceptance

- For a job of each kind that emits a retry-eligible reason (audit, gated base,
  gated PR, pipeline), a failure whose reason is in its `retry_on` is retried up to
  `retry_attempts` times. Each attempt is a separate `history.jsonl` record, as for
  routines.
- Or, for a kind where a retry is unsafe, `validate` rejects `retry_attempts` on that
  kind with a `ConfigError` saying why, so the key is never silently ignored.
- Retries of `clone_failed` / `repo_sync_failed` wait between attempts (a fixed or
  growing delay, bounded so a tick cannot hog the tick lock past its budget). The
  `validate` timeout check accounts for the delay.
- A test for each kind fails the first sync and passes the second, and asserts two
  history records.
- `scripts/ci-check.sh` is green.

## Log

- **2026-11-22**: filed after `audit-type-health` lost its 2026-11-16 run to one DNS
  failure. No job on the Pi sets `retry_attempts` today.
