---
id: "089"
title: "opencode 2's one-time \"what's new\" dialog can stall a job after an upgrade"
status: done
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
- **2026-11-24**: done. Reproduced on the laptop (opencode 2.1.4, herdr 0.9.5, an isolated
  XDG state dir, a throwaway workspace, empty auth). The TUI keeps the last-seen release in
  `$XDG_STATE_HOME/opencode/latest/tui/highlights.json` (`{"seen":"X.Y.Z"}`; the Pi has
  `{"seen":"2.4.1"}` there, the same layout). With no file the first launch shows nothing and
  records the version; with `seen` older than the running version it shows "What's new in
  2.1.4" and rewrites `seen` immediately. While the dialog was up herdr reported `idle`
  and `interactive_ready: true`, so the ready-wait cannot see it, and a prompt sent through
  `herdr agent prompt` was swallowed (input box empty, nothing sent). Fix:
  `HerdrClient.agent_start` raises `seen` to the running version (`opencode --version`) before
  starting an opencode agent. Re-run with `seen` 2.0.0: the helper rewrote it to 2.1.4 and
  the TUI opened with no dialog. Not done: a real upgrade (2.1.4 to 2.4.1) was not run; the
  stale `seen` stands in for it, which is exactly the state an upgrade leaves. The
  `latest` directory name could not be traced to a channel setting in the binary; it is
  the layout both hosts have.
