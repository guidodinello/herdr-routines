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

- **Overnight feature-pipeline orchestrator (POC)** — `done`, 4 real
  dogfood runs so far. → [`004-overnight-feature-pipeline-poc.md`](docs/process/issues/004-overnight-feature-pipeline-poc.md)
- **Pipeline never gates on CI: a red PR passes every stage** — `done`,
  `high`. Gate 3 only runs `pytest`; nothing calls `gh pr checks` or reads
  `statusCheckRollup`, so a PR with failing `ruff format --check`/`ruff
  check` still gets `## Outcome: ok` (hit on PR #81). →
  [`034`](docs/process/issues/034-gate-ci-checks.md)
- **Gate 6 measures blocking-count, not reply coverage — unanswered threads
  pass** — `done`, `high`. Stage 6's contract is fix-and-reply-to-every-
  thread; the gate only checks for a `[blocking]`-tagged thread, so an
  unfixed, unanswered non-blocking thread still passes (PR #81). →
  [`035`](docs/process/issues/035-gate6-reply-coverage.md)
- **`babysit-prs` can never fix a pipeline PR: worktree collision on the
  retained branch** — `done`, `high`. Its per-PR worktree checkout collides
  with the orchestrator's deliberately-retained `auto/pipeline-*` worktree,
  so it can't touch the 20+ PRs it exists to catch. →
  [`036`](docs/process/issues/036-babysit-worktree-collision.md)
- **`pipeline-launch.sh` never inspects settle status: a blocked
  orchestrator looks like success for 8h** — `done`, `high`. `--wait` exits 0
  for `blocked` same as `idle`/`done`, and the pane-close cleanup trap
  destroys the diagnostic screen before anything captures it. →
  [`037`](docs/process/issues/037-launcher-blocked-settle.md)
- **Move the orchestrator's mechanical steps into code** — `done` (phase A,
  PR #137), `high`. Pre-flight now runs in code (`pipeline-prepare`); agents
  keep only the judgment stages. Supersedes issue 052 and the "Code-level
  pipeline gates" Parking Lot item. →
  [`054`](docs/process/issues/054-orchestrator-mechanical-steps-to-code.md)
- **Orchestrator stage loop and stage 4 in code** — `open`, `high`. Phases B
  and C of 054: the stage loop and stage 4 (push + PR) move out of the
  orchestrator session. →
  [`056`](docs/process/issues/056-orchestrator-stage-loop-in-code.md)

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
  disable-by-rename instead of editing a block in a monolith. `medium`. `done`
  (PR #61, 2026-08-31; Pi live config migrated to `jobs.d/`). →
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
- **Worktree GC, delete half** — `done` (PR #49). Human-invoked
  `gc --delete`/`--prune` acting on the dry-run's own output; never
  unattended. → [`012`](docs/process/issues/012-worktree-gc-delete-half.md)
- **Pi `/tmp` tmpfs hygiene** — `done` (PR #81). Age-based cleanup of leaked
  agent-runtime `.so` + pytest artifacts that filled the 2 GB RAM-backed
  `/tmp` and stalled agent starts. →
  [`027`](docs/process/issues/027-tmp-hygiene.md)
- **pick-feature: skip issues with an open pipeline PR** — so a later launch
  never re-picks the feature an in-flight run already has an open PR for
  (006 collision, 2026-08-31). `medium`. →
  [`028`](docs/process/issues/028-pick-feature-skip-open-prs.md)
- **Issue refinement job (Parking Lot → PR)** — automate promoting a Parking
  Lot idea into a refined issue via a bounded author→reviewer loop (product
  refinement vs the pipeline's implementation refinement), delivered as an
  open PR for the human to merge; nightly 22:00, `opencode` only. `medium`. →
  [`029`](docs/process/issues/029-issue-refinement-job.md)
- **Fetch+fast-forward every job's repo before every run** — `done` (PR
  #72). Generalized `ensure_repo` to sync plain `repo:` jobs too, not just
  `repository:`-managed ones (PR #69 had branched 2 days / 6 PRs behind
  `main`). → [`030`](docs/process/issues/030-sync-repo-before-every-run.md)
- **Pipeline stall watchdog** — `done` (PR #73). Automates G-4's manual
  "morning checklist" into a routine that detects a stalled/dead
  orchestrator past `deadline_epoch` and kills the orphaned worker. →
  [`031`](docs/process/issues/031-pipeline-stall-watchdog.md)
- **One-shot nudge before `no_report` failure** — give an agent that settled
  idle/done with no summary file one bounded follow-up prompt to write it,
  before the run is declared failed; `no_report` is 5 of 9 recent `fitted-*`
  job failures, and a whole-job retry (issue 008) can't safely apply since
  re-running risks duplicate side effects. `medium`. `done`. →
  [`032`](docs/process/issues/032-nudge-before-no-report-failure.md)
- **Capture a diagnostic tail on `blocked`, not just other failure paths** —
  `done` (PR #77). `blocked` settles now save a screen tail like every other
  failure path in `execute_run` (3 real `blocked` failures had left zero
  diagnostic evidence). →
  [`033`](docs/process/issues/033-capture-tail-on-blocked.md)

## Later

Curated into issue files 2026-08-27. Time-gated items were promoted to
`status: open`; items gated on an unmade design decision stay `blocked`
(noted below and in the file).

- **Autonomous task selection for the pipeline** — `done`. `pick-feature` +
  the `docs/process/issues/` structured layer shipped (PR #46); self-*scheduling*
  is out of scope. → [`013`](docs/process/issues/013-autonomous-task-selection.md)
- **Auto-fix pull requests (standing job)** — `done` (PR #50). Watches CI +
  review threads on `auto/*` PRs a routine opened, dispatches capped fix
  workers. → [`015`](docs/process/issues/015-auto-fix-pull-requests.md)
- **`repository: <git-url>` job field** — `done` (PR #68). herdr-routines
  owns the clone lifecycle (clone-if-missing, fetch+fast-forward each run).
  → [`016`](docs/process/issues/016-repository-git-url-job-field.md)
- **Auto-fix standing job: checks + target (unified gate model)** — `done`
  (PR #56). Generalizes the auto-fix job around one model — a job runs an
  agent, `checks` optionally gate it, any failure spawns the fix agent — so
  `babysit-prs` and a repo-hygiene lint/typecheck job are the same job
  shape. →
  [`025`](docs/process/issues/025-gate-trigger-standing-job.md)
- **Unify routines + pipeline into one gated-workflow model** — `done` (PR
  #79). First increment: the pipeline schedules as a dispatched job in
  `tick` instead of its own detached `systemd-run` launcher; stages stay
  prompt-hardcoded for now. →
  [`026`](docs/process/issues/026-pipeline-as-routine.md)
- **Model selection per job beyond claude/opencode** — `done`, `low`. Extend
  `model` to another `agent_kind` + a validate-time existence check. →
  [`018`](docs/process/issues/018-model-selection-per-job.md)
- **Log rotation** — `done`, `low`. Size/age rotation of `history.jsonl` +
  opt-in reports prune. → [`021`](docs/process/issues/021-log-rotation.md)
- **API / webhook trigger** — `open`, `low`. Unblocked 2026-09-29: its gate,
  issue 015 (babysit-prs, the gh-api-polling pattern this generalizes), has
  shipped; transport is settled as poll-based. →
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

- **Switch provider/model on quota exhaustion** — `done` (PR #65). Per-job
  `fallback_model` retried once on a classified `quota_exhausted` settle;
  free-tier OpenCode quota modals are the dominant real failure mode. →
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
- **Review `@me` PRs across repos** — `open`, `medium`. A cross-repo
  `kind: review` job: one batched `gh` search for open PRs authored by me
  (or `review-requested`) across all repos, one worktree per dispatched PR, one
  posted review each. Distinct from `babysit-prs` (issue 015), which only watches
  PRs a routine itself opened (`auto/*`); that `auto/` slice is excluded here too,
  so pipeline PRs aren't reviewed twice. The design's hard part is that a review
  has no self-clearing gate: a content marker plus a per-`owner/repo#N` attempt
  budget is what makes it a finite loop. 2026-08-30 brainstorm. →
  [`055`](docs/process/issues/055-review-me-prs-across-repos.md)
- **Audit skills as report→diff gate jobs** — idea, not designed. Turn fitted's
  audit skills (`type-health`, `ui-ux-review`, `accessibility-review`,
  `fix-ignores`, `discover-conventions`, `improve-codebase-architecture`,
  `api-gap-audit`) into scheduled jobs: run cheap check/report → next cycle
  diffs against last report → spawn a fix worker only for new/regressed
  findings. Maps onto issue 025's gate model ("all checks pass → free"). Its
  prerequisite, the 025 design, merged in PR #56, so this is ready to refine.
  2026-08-30 brainstorm.
- **Release/update strategy for herdr-routines + plugins** — `done` for the
  runner: it fast-forwards itself to CI-green `main` nightly (self-update
  timer, [`053`](docs/process/issues/053-runner-checkout-self-update.md)),
  with [`docs/process/pi-update-runbook.md`](docs/process/pi-update-runbook.md)
  as the manual fallback. Plugins, the herdr CLI and `jobs.yaml` stay
  explicit, never auto-mutated. 2026-08-30 brainstorm.
- **Code-level pipeline gates (prompt → enforcement)** — folded into
  [`054`](docs/process/issues/054-orchestrator-mechanical-steps-to-code.md),
  whose phase B runs each stage's gate in code instead of trusting the
  orchestrator prompt. First slice landed 2026-09-07 (PR #109): `tick`
  reconcile enforces the G-17 stage-independence gate
  (`validate_stage_sessions`). 2026-08-30 brainstorm.

House rule: anything a plan document explicitly defers ("out of scope", "v2 item", "deferred
to v1.5") gets a bullet here the day the plan lands, with its gate — so no deferred work lives
only inside `docs/`.
