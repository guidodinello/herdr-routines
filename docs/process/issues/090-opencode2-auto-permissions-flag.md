---
id: "090"
title: "Should herdr-routines pass opencode 2's --auto?"
status: blocked
priority: low
area: infra
gate: decide whether herdr-routines may ever auto-approve permissions for an opencode agent
---

## Description

opencode 2.4.1's interactive command has `--auto` ("auto-approve permissions that are not
explicitly denied"). herdr-routines passes no such flag: `docs/plan-v1.md` (the approval
section) treats a skip-permissions escape hatch as a later decision to be made with
evidence, and `tests/test_herdr.py` asserts `build_agent_start_args` never emits one.
Permission prompts today surface as a `blocked` agent and go through the approval path
(issue 007).

PR #286 left `--auto` out of scope. This issue exists so the decision is made on purpose,
not by drift.

## Acceptance

- A decision is recorded here: keep the current approval path, or allow `--auto` for
  named jobs only (with the test and the plan text updated to match).
- If allowed: it is opt-in per job, and never reaches a job that can push or post.

## Log

- **2026-11-24**: filed as a follow-up of PR #286.
