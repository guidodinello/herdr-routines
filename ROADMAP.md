# Roadmap

v1 (time-triggered jobs, YAML config, run history, systemd deployment, notifications via
`herdr notification show` — relayed off-box by the separately installed `herdr-push` plugin)
covers the core loop and is considered done — see [`docs/plan-v1.md`](docs/plan-v1.md) and the
README's Status section.

Items are grouped by horizon (**Now / Next / Later**) rather than version numbers: version
framing (v1.5/v2) buys nothing for a single-user tool with no external consumers. Each item
carries its **gate** — the condition that must hold before it's worth designing properly.
Promote items as gates clear; park brand-new ideas in the Parking Lot first.

All horizons are now curated into per-item files under
[`docs/process/issues/`](docs/process/issues/) — this document is a one-liner
index (pattern matches `~/projects/PENDING.md`); the full description, update
log, and acceptance criteria live in the issue file. Query the buildable
backlog with `grep -l "status: open" docs/process/issues/*.md`, or
`herdr-routines pick-feature`.

Most Next/Later items originally shared one gate — **the Pi deployment
running real jobs for a few weeks**. As of 2026-08-27 that gate is
considered met (several days of clean nightly runs) and those time-gated
items were promoted to `status: open`. What stays `blocked` is items gated on
an *unmade design decision*, not on elapsed time (each says so in its file).

## Now

In progress or ready to build; no real-run evidence required.

- **Overnight feature-pipeline orchestrator (POC)** — `in-progress`, 4 real
  dogfood runs so far. → [`004-overnight-feature-pipeline-poc.md`](docs/process/issues/004-overnight-feature-pipeline-poc.md)

Done (kept as `status: done` issue files for history): plugin manifest
([`001`](docs/process/issues/001-plugin-manifest.md), PR #29), worktree GC
dry-run ([`002`](docs/process/issues/002-worktree-gc-dry-run.md), PR #28),
status CLI table view ([`003`](docs/process/issues/003-status-cli-table-view.md),
PR #41/#43), failure reaping phase 2 watchdog
([`005`](docs/process/issues/005-failure-reaping-phase-2-watchdog.md), PR #47),
autonomous task selection / `pick-feature`
([`013`](docs/process/issues/013-autonomous-task-selection.md), PR #46),
Telegram plugin replacement
([`023`](docs/process/issues/023-replace-herdr-push-telegram.md), 2026-08-25).

## Next

Promoted to `status: open` on 2026-08-27 (the shared "few weeks of real runs"
gate is considered met). One-liner index; full detail in the issue file.

- **Split `jobs.yaml` into `jobs.d/<name>.yaml`** — directory-discovered, one
  file per job + sibling `defaults.yaml`; scripted single-job edits and
  disable-by-rename instead of editing a block in a monolith. `medium`. →
  [`006`](docs/process/issues/006-jobs-dir-per-file.md)
- **Approval path for `blocked` runs** — one actionable notification →
  approve the pending permission prompt from the phone; no auto-approve
  escape hatch. `low`. →
  [`007`](docs/process/issues/007-approval-path-blocked-runs.md)
- **Retries on failure** — opt-in, per-job, only for a declared whitelist of
  transient failure `reason`s; never for non-idempotent jobs. `low`. →
  [`008`](docs/process/issues/008-retries-on-failure.md)
- **Notification policy per job** — `always` / `on-failure` / `on-finding`;
  default to one terminal-state ping, not per-step noise. `low`. →
  [`009`](docs/process/issues/009-notification-policy-per-job.md)
- **Daily digest** — one morning summary of terminal states + report links.
  `low`. → [`010`](docs/process/issues/010-daily-digest.md)
- **Pane/session retention policy** — capture the transcript to the history
  log before closing the pane; document the retention window. `low`. →
  [`011`](docs/process/issues/011-pane-session-retention-policy.md)
- **Worktree GC, delete half** — human-invoked `gc --delete` acting on the
  dry-run's output; never unattended. `medium`. →
  [`012`](docs/process/issues/012-worktree-gc-delete-half.md)

## Later

Curated into issue files 2026-08-27. Time-gated items were promoted to
`status: open`; items gated on an unmade design decision stay `blocked`
(noted below and in the file).

- **Autonomous task selection for the pipeline** — `done`. `pick-feature` +
  the `docs/process/issues/` structured layer shipped (PR #46); self-*scheduling*
  is out of scope. → [`013`](docs/process/issues/013-autonomous-task-selection.md)
- **Auto-fix pull requests (standing job)** — `open`, `medium`. Watch CI +
  review threads on `auto/*` PRs a routine opened, dispatch capped fix
  workers. → [`015`](docs/process/issues/015-auto-fix-pull-requests.md)
- **`repository: <git-url>` job field** — `open`, `medium`. herdr-routines
  owns the clone lifecycle (clone-if-missing, fast-forward each run). →
  [`016`](docs/process/issues/016-repository-git-url-job-field.md)
- **Model selection per job beyond claude/opencode** — `open`, `low`. Extend
  `model` to another `agent_kind` + a validate-time existence check. →
  [`018`](docs/process/issues/018-model-selection-per-job.md)
- **Log rotation** — `open`, `low`. Size/age rotation of `history.jsonl` +
  opt-in reports prune. → [`021`](docs/process/issues/021-log-rotation.md)
- **API / webhook trigger** — `blocked` on issue 015 shipping first (it
  builds the gh-api-polling pattern this would generalize); transport is
  otherwise settled as poll-based. →
  [`014`](docs/process/issues/014-api-webhook-trigger.md)
- **Docker image for trivial multi-host setup** — `blocked`: PTY-in-container
  worry resolved; now gated on a secret-injection + image-architecture
  decision, and no live demand (hp migration paused). →
  [`017`](docs/process/issues/017-docker-image.md)
- **Concurrency beyond the single tick lock** — `blocked`: `history.jsonl`
  (2026-08-28) shows median 14s start delay, no starvation — gate working as
  intended. → [`019`](docs/process/issues/019-concurrency-beyond-tick-lock.md)
- **Web / TUI dashboard** — `blocked`: CLI inspection is enough; friction
  trigger not hit. → [`020`](docs/process/issues/020-web-tui-dashboard.md)

## Explicitly out of scope for now

- Connectors (MCP/skill config) — CLI agents already carry whatever they're configured with;
  no equivalent needed.
- A hosted/cloud environment equivalent to Claude's sandbox — this always runs on the Pi.

## Parking lot

Anything else noticed while actually running jobs — add a bullet here, promote to
Now/Next/Later once it's clear it's worth designing properly. Curated into issue
files 2026-08-27.

- **Switch provider/model on quota exhaustion** — `open`, `medium`. Per-job
  failover model list on a classified `quota_exhausted` settle; free-tier
  OpenCode quota modals are the dominant real failure mode. →
  [`022`](docs/process/issues/022-switch-model-on-quota-exhaustion.md)
- **Replace the herdr-push Telegram plugin** — `done`. `cokekitten/herdr-telegram-bridge`
  installed + configured on the Pi (2026-08-25); `herdr.push` removed. Residual
  follow-ups (laptop `herdr.push` cleanup, the `pane.agent_status_changed`
  race verification) are logged in the file. →
  [`023`](docs/process/issues/023-replace-herdr-push-telegram.md)
- **Spawn a session on the fly from Telegram** — `open`, `low`. Cold-start
  `/run <job>` mapping to `workspace create` + `agent start`; transport is
  settled by [`023`]. →
  [`024`](docs/process/issues/024-spawn-session-from-telegram.md)
- **Review `@me` PRs across repos** — idea, not designed. Scan open PRs authored
  by me (or `--review-requested`) across all my repos and code-review each one.
  Distinct from `babysit-prs` (issue 015), which only watches PRs a routine
  itself opened (`auto/*`). Gate: the jobs refactor (issue 006 / 025 design)
  — a poll-to-dispatch job shape. 2026-08-30 brainstorm.
- **Audit skills as report→diff gate jobs** — idea, not designed. Turn fitted's
  audit skills (`type-health`, `ui-ux-review`, `accessibility-review`,
  `fix-ignores`, `discover-conventions`, `improve-codebase-architecture`,
  `api-gap-audit`) into scheduled jobs: run cheap check/report → next cycle
  diffs against last report → spawn a fix worker only for new/regressed
  findings. Maps onto issue 025's gate model ("all checks pass → free"). Gate:
  025 design merged. 2026-08-30 brainstorm.
- **Release/update strategy for herdr-routines + plugins** — idea, not designed.
  Industry standard is explicit `update` commands, not auto-updates; but the
  one safe carve-out: herdr-routines' own changes already flow through PR+CI
  gate, so the Pi can fast-forward its checkout to released/CI-green commits
  (folds into issue 016's `repository:` clone+fast-forward). The manual
  fast-forward is runnable today — see
  [`docs/process/pi-update-runbook.md`](docs/process/pi-update-runbook.md).
  Plugins + herdr
  CLI + `jobs.yaml` stay explicit; never auto-mutate. Needs refinement. Gate:
  issue 016. 2026-08-30 brainstorm.

House rule: anything a plan document explicitly defers ("out of scope", "v2 item", "deferred
to v1.5") gets a bullet here the day the plan lands, with its gate — so no deferred work lives
only inside `docs/`.
