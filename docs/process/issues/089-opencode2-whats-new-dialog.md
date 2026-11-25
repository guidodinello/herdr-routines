---
id: "089"
title: "opencode 2's one-time \"what's new\" dialog can stall a job after an upgrade"
status: open
priority: low
area: infra
---

## Description

The first launch of an opencode 2 version shows a "what's new" dialog that blocks input
until Enter is pressed (seen on the Pi on 2026-11-24 when it moved to 2.4.1; dismissed by
hand since). The next opencode upgrade will show it again. A job started in a fresh pane
then types its prompt into the dialog and waits until `timeout_ms`, with no obvious
failure reason.

## Acceptance

- After an opencode upgrade, the first job run does not stall on the dialog: either the
  start dismisses it, or the stall is reported as its own failure reason.
- Verified against a real opencode 2 upgrade, or against the dialog's stored "seen"
  state, not only a unit test with a faked screen.
- `scripts/ci-check.sh` is green.

## Log

- **2026-11-24**: filed as a follow-up of PR #286 (opencode 2 model selection).
