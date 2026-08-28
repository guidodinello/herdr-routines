---
id: "017"
title: "Docker image for trivial multi-host setup"
status: blocked
priority: low
area: infra
gate: a design decision on secret injection (gh/git SSH, opencode/claude/model-provider auth) and image architecture (single image vs. compose) — not yet made
---

## Description

Bundle `herdr-routines` so standing it up on a new machine is "run the
image," not `uv sync` + copy/edit `jobs.yaml` + install the systemd units +
separately install and configure Herdr itself.

This tool doesn't run agents directly — it drives Herdr, which manages real
terminal panes/PTYs for the `opencode`/`claude` processes it spawns. The
original worry was whether that survives containerization; that part is
**resolved** (see log) — the PTYs are not the blocker. What remains is a
design decision, not a technical unknown:

- **Secret injection** — gh/git SSH keys, `opencode`/`claude`/model-provider
  auth all have to reach the container without being baked into the image.
- **Image architecture** — one image running the herdr server + scheduler +
  agent CLIs, or a compose stack.

Until those are decided there's no spec to implement, so this stays
`blocked`. Related to issue 016 (`repository:` field) — same "new host"
motivation, distinct scope.

## Acceptance

To be written once the secret-injection + architecture decisions are made.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Later §.
- **2026-08-28**: PTY question resolved from the herdr research notes —
  Herdr has a working headless server mode (`herdr server`, smoke-tested) and
  its panes are `forkpty` PTYs, which work fine inside containers. Gate
  reworded to the real open question: secret injection + image architecture.
  Trigger unchanged: a second host (the hp-server migration is paused as of
  2026-08-26, so no live demand).
