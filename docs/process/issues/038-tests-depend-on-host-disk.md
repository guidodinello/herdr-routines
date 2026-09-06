---
id: "038"
title: "Test suite reads real host disk state: 6 tests fail on a machine with a full /tmp"
status: done
priority: medium
area: runner
---

## Description

Six tests fail on any host whose `/tmp` filesystem is ≥95% full, and pass
everywhere else. They are green on CI and red on a developer laptop, which is the
worst possible arrangement: CI cannot be trusted as the gate for this class of bug.

```
FAILED tests/test_runner.py::test_execute_run_agent_start_failure_short_circuits
FAILED tests/test_runner.py::test_execute_run_unready_agent_fails_without_prompting
FAILED tests/test_runner.py::test_execute_run_ready_polling_survives_cli_errors
FAILED tests/test_runner.py::test_execute_run_readiness_polling_survives_oserror
FAILED tests/test_runner.py::test_agent_not_interactive_reaps_pane
FAILED tests/test_tick.py::test_failure_flagged_when_a_due_job_actually_fails

AssertionError: assert 'tmp_full' == 'agent_not_interactive'
```

### Cause

Issue 027 (PR #81) added `diagnose_tmp()`, which shells out to a real
`df -h /tmp` and sets `tmp_full=True` at ≥95% usage. `execute_run` calls it via
`_best_effort_tmp_diagnosis` on both agent-start failure paths and *overrides the
failure reason* when the disk is full:

```python
reason="tmp_full" if diagnosis and diagnosis.get("tmp_full") else "agent_start_failed",
```

That behaviour is correct in production — a full disk really is the more useful
diagnosis. The bug is that **nothing stubs it in tests**, so `execute_run`'s
reported reason silently depends on the ambient state of the machine running
pytest. On a host at 98% every one of these assertions flips.

### Why CI did not catch this

It is the question worth answering, because "we run tests in CI" is exactly the
gate that should have caught it. GitHub-hosted runners start with roughly 14 GB
free on `/`, so `df` reports well under 95%, `tmp_full` is always `False`, and the
tests pass. CI is not a defence against a test that depends on ambient host state
when the CI environment happens to satisfy the hidden assumption. The only signal
is running the suite somewhere else — which is how this surfaced.

### The coverage gap underneath it

`tmp_full` is asserted in exactly one place, `test_tmp_hygiene.py`, and only
against `diagnose_tmp()` called directly with `subprocess.run` patched. **No test
exercises `execute_run`'s mapping from a full-disk diagnosis to
`reason="tmp_full"`** — the integration that actually shipped. Had that test
existed it would have had to stub the diagnosis, and the ambient dependency would
have been obvious at review time.

## Design

1. Add `tests/conftest.py` with an **autouse** fixture that patches
   `herdr_routines.runner._best_effort_tmp_diagnosis` to return a deterministic
   not-full diagnosis. That is the single seam through which `execute_run`
   consults the host; stubbing it makes the suite hermetic by default rather than
   fixing six call sites and waiting for the seventh.
   `diagnose_tmp()`'s own unit tests call it directly and are unaffected.
2. Add the missing integration coverage: `execute_run` must report `tmp_full` when
   the diagnosis says the disk is full, and the underlying reason when it does not.
   These opt out of the autouse stub by patching explicitly.

Deliberately **not** changing production behaviour — `diagnose_tmp` reading real
`df` is the point of issue 027. Only the tests change.

## Acceptance criteria

1. The full suite passes on a host whose `/tmp` is ≥95% full. Test: `test_execute_run_failure_reason_is_not_host_disk_dependent`
2. `execute_run` reports `tmp_full` when the diagnosis reports a full disk. Test: `test_execute_run_maps_full_disk_to_tmp_full_reason`
3. `execute_run` reports the underlying reason when the disk is not full. Test: `test_execute_run_keeps_underlying_reason_when_disk_not_full`
