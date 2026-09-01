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
- **2026-08-31**: shipped a scoped v1, not the full ordered failover list —
  `fallback_model: str | None` (config.py, settable in `defaults.yaml` so a
  whole provider's job set shares one fallback, or per-job override). On a
  primary run settling `reason=quota_exhausted`, `tick._process_job` retries
  exactly once with `dataclasses.replace(job, model=job.fallback_model)`
  under a fresh run_id (`<job>-fallback-<ts>`); both attempts land as
  distinct `history.jsonl` records (`fallback_retry`/`primary_run_id` in
  `extra`), never for other failure reasons. No `agent_kind` override and no
  multi-entry chain — deliberately deferred (YAGNI) until a single fallback
  proves insufficient. Deployed on the Pi: `opencode` (Zen) primary,
  `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free` fallback in
  `~/.config/herdr-routines/jobs.d/defaults.yaml`, after the Pi's OpenRouter
  credential was wired up and verified live. Tests:
  `test_fallback_model_retried_once_after_quota_exhausted`,
  `test_no_fallback_retry_when_fallback_model_not_set` (tick), plus
  `config.py` validation tests. If quota exhaustion recurs on the fallback
  provider too, reopen for the ordered-list shape this issue originally
  described.
