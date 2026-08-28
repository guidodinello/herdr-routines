---
id: "017"
title: "Docker image for trivial multi-host setup"
status: blocked
priority: low
area: infra
gate: an unanswered question — does Herdr itself run cleanly inside a container? — must be checked against Herdr's docs before this is worth designing
---

## Description

Bundle `herdr-routines` so standing it up on a new machine is "run the
image," not `uv sync` + copy/edit `jobs.yaml` + install the systemd units +
separately install and configure Herdr itself.

Real open question before committing, not just packaging effort: **does Herdr
run cleanly inside a container?** This tool doesn't run agents directly — it
drives Herdr, which manages real terminal panes/PTYs for the
`opencode`/`claude` processes it spawns. If Herdr needs host-level TTY/session
semantics that don't survive containerization, a Docker image would only wrap
the thin Python scheduler half and leave the fiddly half (Herdr install + its
auth, `gh`/git SSH auth, model-provider auth) still manual.

## Acceptance

To be written once the Herdr-in-a-container question is answered.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Later §. Kept `blocked` — gated
  on an unanswered technical question, not on elapsed real-run time. Trigger:
  a second host (same as issue 016), once the containerization question is
  actually resolved.
