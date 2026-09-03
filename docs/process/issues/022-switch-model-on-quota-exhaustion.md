---
id: "022"
title: "Switch provider/model on quota exhaustion"
status: done
priority: medium
area: pipeline
---

## Description

React to `reason=quota_exhausted` (`docs/failure-reaping.md` §3.2) with a
per-job failover model list, or degrade to a smaller tier for the rest of the
window, instead of just ending the run.

Free-tier OpenCode quota modals have been the single most common real failure
mode on the Pi (observed 2026-08-22/23, and the reason failure-reaping phase
1 exists). A job that could fall back from
`opencode/<free-model>` to a second free model — or to `claude` — would
survive the window instead of dead-ending.

## Acceptance

- A job can declare an ordered failover list (`model` + optionally
  `agent_kind`); on a classified `quota_exhausted` settle, the run retries
  once per remaining entry.
- Failover attempts are logged distinctly in `history.jsonl`.
- No failover on non-quota failures (that's issue 008's territory, with its
  own reason whitelist).

## Log

- **2026-08-27**: curated from `ROADMAP.md` Parking Lot §. Gate ("quota
  failures recurring after failure-reaping phase 1 ships") — phase 1 shipped
  (PR #25) and quota modals have continued to be the dominant failure class,
  so the gate is met. Closely related to issues 005 (watchdog) and 008
  (retries).
