# Roadmap

v1 (time-triggered jobs, YAML config, run history, systemd deployment, Telegram-ready
notifications via `herdr-push`) covers the core loop and is considered done — see
[`docs/plan-v1.md`](docs/plan-v1.md) and the README's Status section. Everything below is
future work, deliberately not scoped in detail yet — add to this list as things come up while
actually using it, rather than trying to design it all up front.

## Candidate features

- **API/webhook trigger** — Claude Routines supports "Call via API" (POST to trigger a run) and
  a GitHub-event trigger. Both assume inbound reachability, which the Pi doesn't have without a
  tunnel (declined earlier for the Telegram relay — see `../agent-orchestrator-research/herdr.md`).
  A GitHub-event-style trigger would likely start as **poll-based** (a timer checks `gh api` on a
  short interval and diffs) rather than a true webhook. A same-LAN "call via API" (a small local
  HTTP endpoint, no tunnel needed) is more plausible short-term than the GitHub-event case.
- **Auto-fix pull requests** — Claude Routines has a "Behavior" toggle: "Watch CI and review
  comments on PRs this routine opens, and let Claude push fixes." This is basically the
  `babysit-prs` skill pattern, but as a standing job instead of something invoked manually.
  Worth revisiting once a couple of scheduled review-style jobs have run for real.
- **Smarter notifications** — Routines frames its notification toggle as "only when there's
  something worth telling you," not a ping on every run. v1's notification story is whatever
  `herdr-push` sends by default; worth deciding what "worth telling you" means for a given job
  (e.g. only notify on failure, or on a non-trivial finding) rather than notifying on every run.
- **Pane/session retention policy** — decided to punt on this deliberately (see conversation
  2026-08-21/22): capture the full transcript to the run-history log as soon as a run finishes,
  then close the pane — but the actual cleanup timing/policy (immediate vs. keep-for-a-week vs.
  manual) still needs a few real runs to get a feel for before locking in.
- **Model selection per job, beyond claude/opencode** — `model` is now wired through to
  `agent_start` for `agent_kind: claude`/`opencode` (the only two kinds with a pinned-down native
  flag, see `AGENT_MODEL_FLAGS` in `config.py`). Extending it to the other agent kinds, or adding
  a model-catalog/existence check, is still open.

## Explicitly out of scope for now

- Connectors (MCP/skill config) — CLI agents already carry whatever they're configured with;
  no equivalent needed.
- A hosted/cloud environment equivalent to Claude's sandbox — this always runs on the Pi.

## Parking lot

Anything else noticed while actually running jobs — add a bullet here, promote to "Candidate
features" once it's clear it's worth designing properly.
