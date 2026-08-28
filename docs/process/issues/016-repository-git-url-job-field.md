---
id: "016"
title: "repository: <git-url> job field"
status: open
priority: medium
area: config
---

## Description

A job field that has herdr-routines own the clone lifecycle: idempotent
clone-if-missing (likely under `~/.local/state/herdr-routines/repos/<name>`),
pulled / kept up to date on each run, rather than requiring `repo:` to
already exist on the host.

Mainly for standing the tool up on a new host — or pointing a job at a repo
not yet cloned there — without a manual `git clone` step; also makes
`jobs.yaml` describe a job fully portably.

## Acceptance

- A job with `repository: <git-url>` and no existing checkout is cloned on
  first run to a deterministic path; subsequent runs `fetch` + fast-forward.
- A job with an explicit `repo:` path keeps working unchanged.
- Clone/pull failure is a clean run failure with a clear reason, not a
  partial checkout the agent then runs against.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Later §. Trigger ("a second host,
  or such a job") — the hp-server migration (paused 2026-08-26) is that
  second host; the migration involved a manual clone this field would remove.
  Related to issue 017 (Docker image) — same "new host" motivation, distinct
  scope (this is the clone lifecycle, 017 is the whole runtime).
