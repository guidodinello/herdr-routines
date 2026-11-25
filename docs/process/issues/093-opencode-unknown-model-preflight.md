---
id: "093"
title: "Fail a start clearly when an opencode model id is not in the catalog"
status: done
priority: low
area: infra
---

## Description

`opencode api session.create` accepts a model id that does not exist and answers 200; the
TUI then shows `<id> (unavailable)` and the failure appears only at the first prompt, as an
opaque error or a timeout. Found on the Pi on 2026-11-24: `opencode/glm-5.3-flash-free` had
been dropped from the 2.4.1 catalog while three job files still named it.

`pipeline_prepare` already checks the pipeline's stage models against `opencode models`
before claiming an issue. Routine jobs, `fallback_model` retries and Telegram `/run` do
not.

## Acceptance

- A start on an opencode model that is not in the catalog fails at start with a clear
  `agent_start_failed` message naming the model (or `validate` reports it), using
  `opencode models` or `GET /api/model`.
- An unreachable catalog does not block starts (same "unverifiable" handling as
  `pipeline_prepare.opencode_models`).
- `scripts/ci-check.sh` is green.

## Log

- **2026-11-24**: filed as a follow-up of PR #286.
- **2026-11-24**: done. `HerdrClient.agent_start` checks an opencode model against
  `opencode models` (one listing per start, ~1 s on the laptop, 927 ids) after the service
  check and before `session.create`/`session.switchModel`, so every start path (routine,
  `fallback_model` retry, Telegram `/run`, pipeline stages) fails `agent_start_failed`
  naming the model. A failed, timed-out or empty listing is "unverifiable" and starts
  anyway; `pipeline_prepare.opencode_models()` now shares `herdr.list_opencode_models`.
  `validate` deliberately does not report it: the catalog is per host and `validate`
  also runs where the job will not. A missing primary model fails the start and does not
  trigger `fallback_model` (quota-only); a missing fallback fails the retry at start.
