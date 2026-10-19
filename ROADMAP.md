# Roadmap

v1 (time-triggered jobs, YAML config, run history, systemd deployment, notifications via
`herdr notification show` — relayed off-box by the separately installed `herdr-push` plugin)
covers the core loop and is considered done — see [`docs/plan-v1.md`](docs/plan-v1.md) and the
README's Status section.

Items are grouped by horizon (**Now / Next / Later**) rather than version numbers: version
framing (v1.5/v2) buys nothing for a single-user tool with no external consumers. Promote
items as their gates clear; park brand-new ideas in the Parking Lot first.

All horizons are curated into per-item files under
[`docs/process/issues/`](docs/process/issues/) — this document is a one-liner
index (pattern matches `~/projects/PENDING.md`); the full description, update
log, and acceptance criteria live in the issue file.

**Status, priority and gate live only in each issue file's frontmatter**
(`status:`, `priority:`, `gate:`) — this index never repeats them, so it can't
drift (`tests/test_roadmap_index.py` enforces that, and that every issue file is
linked here). Query them directly, e.g. `rg -l "status: open" docs/process/issues/`
for the buildable backlog, `rg "^gate:" docs/process/issues/` for what each item
waits on, or `herdr-routines pick-feature`.

Most Next/Later items originally shared one gate — **the Pi deployment
running real jobs for a few weeks**. As of 2026-08-27 that gate is
considered met (several days of clean nightly runs) and those time-gated
items were promoted. Items gated on an *unmade design decision*, not on
elapsed time, say so in their file's `gate:`.

## Now

Pipeline work; no real-run evidence required.

- **Overnight feature-pipeline orchestrator (POC)** — 4 real dogfood runs so
  far. → [`004-overnight-feature-pipeline-poc.md`](docs/process/issues/004-overnight-feature-pipeline-poc.md)
- **Pipeline never gates on CI: a red PR passes every stage** — Gate 3 only
  ran `pytest`; nothing called `gh pr checks` or read `statusCheckRollup`, so
  a PR with failing `ruff format --check`/`ruff check` still got
  `## Outcome: ok` (hit on PR #81). →
  [`034`](docs/process/issues/034-gate-ci-checks.md)
- **Gate 6 measures blocking-count, not reply coverage — unanswered threads
  pass** — Stage 6's contract is fix-and-reply-to-every-thread; the gate only
  checked for a `[blocking]`-tagged thread, so an unfixed, unanswered
  non-blocking thread still passed (PR #81). →
  [`035`](docs/process/issues/035-gate6-reply-coverage.md)
- **`babysit-prs` can never fix a pipeline PR: worktree collision on the
  retained branch** — Its per-PR worktree checkout collided with the
  orchestrator's deliberately-retained `auto/pipeline-*` worktree, so it
  couldn't touch the 20+ PRs it exists to catch. →
  [`036`](docs/process/issues/036-babysit-worktree-collision.md)
- **`pipeline-launch.sh` never inspects settle status: a blocked
  orchestrator looks like success for 8h** — `--wait` exited 0 for a blocked
  settle same as idle/done, and the pane-close cleanup trap destroyed the
  diagnostic screen before anything captured it. →
  [`037`](docs/process/issues/037-launcher-blocked-settle.md)
- **Move the orchestrator's mechanical steps into code** — Phase A (PR #137):
  pre-flight runs in code (`pipeline-prepare`); agents keep only the judgment
  stages. Supersedes issue 052 and the "Code-level pipeline gates" Parking Lot
  item. →
  [`054`](docs/process/issues/054-orchestrator-mechanical-steps-to-code.md)
- **Orchestrator stage loop and stage 4 in code** — Phases B and C of 054
  (PR #141): the stage loop and stage 4 (push + PR) moved out of the
  orchestrator session into `pipeline-run`. →
  [`056`](docs/process/issues/056-orchestrator-stage-loop-in-code.md)
- **Pipeline bookkeeping in code, CI gate with a fix loop, test authoring split
  from implementation** — Stage-3 bookkeeping moves into `pipeline-run`; the PR
  is gated on `scripts/ci-check.sh` with a bounded fix loop. →
  [`059`](docs/process/issues/059-pipeline-bookkeeping-in-code.md)

Earlier items, kept as issue files for history: plugin manifest
([`001`](docs/process/issues/001-plugin-manifest.md), PR #29), worktree GC
dry-run ([`002`](docs/process/issues/002-worktree-gc-dry-run.md), PR #28),
status CLI table view ([`003`](docs/process/issues/003-status-cli-table-view.md),
PR #41/#43), failure reaping phase 2 watchdog
([`005`](docs/process/issues/005-failure-reaping-phase-2-watchdog.md), PR #47),
autonomous task selection / `pick-feature`
([`013`](docs/process/issues/013-autonomous-task-selection.md), PR #46),
Telegram plugin replacement
([`023`](docs/process/issues/023-replace-herdr-push-telegram.md), 2026-08-25).

Bugs found while running real jobs: tests reading host disk state
([`038`](docs/process/issues/038-tests-depend-on-host-disk.md)), a dead run
orphaning its picked issue ([`040`](docs/process/issues/040-dead-run-orphans-picked-issue.md)),
pick-feature's flip wedging repo sync ([`041`](docs/process/issues/041-pick-flip-blocks-repo-sync.md)),
digest listing disabled jobs as failing ([`046`](docs/process/issues/046-digest-reports-disabled-jobs-as-failing.md)),
the orchestrator wedging on a bare `herdr-routines`
([`050`](docs/process/issues/050-orchestrator-wedges-on-bare-herdr-routines.md)),
a blocked agent name wedging its recurring job
([`051`](docs/process/issues/051-blocked-agent-name-wedges-recurring-job.md)),
the pipeline launching with nothing to build
([`052`](docs/process/issues/052-pipeline-launches-with-no-feature-to-build.md)),
and a run of worktree GC fixes — pipeline exclusion
([`039`](docs/process/issues/039-gc-pipeline-exclusion-too-coarse.md)), squash-merge
detection ([`042`](docs/process/issues/042-gc-squash-merge-detection.md)), delete
matching dry-run ([`043`](docs/process/issues/043-gc-delete-matches-dry-run.md)), age
threshold ([`044`](docs/process/issues/044-gc-delete-age-threshold.md),
[`047`](docs/process/issues/047-gc-age-defeated-by-noncommitting-branches.md)),
orphaned worktree entries ([`045`](docs/process/issues/045-gc-leaves-orphaned-worktree-entries.md))
and the orphan scan's repo scoping
([`048`](docs/process/issues/048-gc-orphan-scan-hardcoded-to-herdr-routines.md)).

## Next

Promoted on 2026-08-27 (the shared "few weeks of real runs" gate is
considered met). One-liner index; full detail in the issue file.

- **Split `jobs.yaml` into `jobs.d/<name>.yaml`** — directory-discovered, one
  file per job + sibling `defaults.yaml`; scripted single-job edits and
  disable-by-rename instead of editing a block in a monolith. PR #61,
  2026-08-31; Pi live config migrated to `jobs.d/`. →
  [`006`](docs/process/issues/006-jobs-dir-per-file.md)
- **Approval path for `blocked` runs** — one actionable notification →
  approve the pending permission prompt from the phone; no auto-approve
  escape hatch. →
  [`007`](docs/process/issues/007-approval-path-blocked-runs.md)
- **Retries on failure** — opt-in, per-job, only for a declared whitelist of
  transient failure `reason`s; never for non-idempotent jobs. →
  [`008`](docs/process/issues/008-retries-on-failure.md)
- **Notification policy per job** — `always` / `on-failure` / `on-finding`;
  default to one terminal-state ping, not per-step noise. →
  [`009`](docs/process/issues/009-notification-policy-per-job.md)
- **Daily digest** — one morning summary of terminal states + report links.
  → [`010`](docs/process/issues/010-daily-digest.md)
- **Pane/session retention policy** — capture the transcript to the history
  log before closing the pane; document the retention window. →
  [`011`](docs/process/issues/011-pane-session-retention-policy.md)
- **Worktree GC, delete half** — PR #49. Human-invoked
  `gc --delete`/`--prune` acting on the dry-run's own output; never
  unattended. → [`012`](docs/process/issues/012-worktree-gc-delete-half.md)
- **Pi `/tmp` tmpfs hygiene** — PR #81. Age-based cleanup of leaked
  agent-runtime `.so` + pytest artifacts that filled the 2 GB RAM-backed
  `/tmp` and stalled agent starts. →
  [`027`](docs/process/issues/027-tmp-hygiene.md)
- **pick-feature: skip issues with an open pipeline PR** — so a later launch
  never re-picks the feature an in-flight run already has an open PR for
  (006 collision, 2026-08-31). →
  [`028`](docs/process/issues/028-pick-feature-skip-open-prs.md)
- **Issue refinement job (Parking Lot → PR)** — automate promoting a Parking
  Lot idea into a refined issue via a bounded author→reviewer loop (product
  refinement vs the pipeline's implementation refinement), delivered as an
  open PR for the human to merge; nightly 22:00, `opencode` only. →
  [`029`](docs/process/issues/029-issue-refinement-job.md)
- **Fetch+fast-forward every job's repo before every run** — PR #72.
  Generalized `ensure_repo` to sync plain `repo:` jobs too, not just
  `repository:`-managed ones (PR #69 had branched 2 days / 6 PRs behind
  `main`). → [`030`](docs/process/issues/030-sync-repo-before-every-run.md)
- **Pipeline stall watchdog** — PR #73. Automates G-4's manual
  "morning checklist" into a routine that detects a stalled/dead
  orchestrator past `deadline_epoch` and kills the orphaned worker. →
  [`031`](docs/process/issues/031-pipeline-stall-watchdog.md)
- **One-shot nudge before `no_report` failure** — give an agent that settled
  idle/done with no summary file one bounded follow-up prompt to write it,
  before the run is declared failed; `no_report` was 5 of 9 recent `fitted-*`
  job failures, and a whole-job retry (issue 008) can't safely apply since
  re-running risks duplicate side effects. →
  [`032`](docs/process/issues/032-nudge-before-no-report-failure.md)
- **Capture a diagnostic tail on `blocked`, not just other failure paths** —
  PR #77. Blocked settles save a screen tail like every other failure path
  in `execute_run` (3 real blocked failures had left zero diagnostic
  evidence). →
  [`033`](docs/process/issues/033-capture-tail-on-blocked.md)

## Later

Curated into issue files 2026-08-27. Time-gated items were promoted; items
gated on an unmade design decision say so in their file's `gate:`.

- **Autonomous task selection for the pipeline** — `pick-feature` + the
  `docs/process/issues/` structured layer (PR #46); self-*scheduling* is out
  of scope. → [`013`](docs/process/issues/013-autonomous-task-selection.md)
- **Auto-fix pull requests (standing job)** — PR #50. Watches CI + review
  threads on `auto/*` PRs a routine opened, dispatches capped fix workers. →
  [`015`](docs/process/issues/015-auto-fix-pull-requests.md)
- **`repository: <git-url>` job field** — PR #68. herdr-routines owns the
  clone lifecycle (clone-if-missing, fetch+fast-forward each run). →
  [`016`](docs/process/issues/016-repository-git-url-job-field.md)
- **Auto-fix standing job: checks + target (unified gate model)** — PR #56.
  Generalizes the auto-fix job around one model — a job runs an agent,
  `checks` optionally gate it, any failure spawns the fix agent — so
  `babysit-prs` and a repo-hygiene lint/typecheck job are the same job
  shape. →
  [`025`](docs/process/issues/025-gate-trigger-standing-job.md)
- **Unify routines + pipeline into one gated-workflow model** — PR #79. First
  increment: the pipeline schedules as a dispatched job in `tick` instead of
  its own detached `systemd-run` launcher; stages stay prompt-hardcoded. →
  [`026`](docs/process/issues/026-pipeline-as-routine.md)
- **One gated-workflow engine, `kind` as the single dispatch key** —
  Follow-up to 026: routines and the pipeline share one engine. →
  [`049`](docs/process/issues/049-unify-routines-pipeline-as-one-gated-workflow-engine.md)
- **Model selection per job beyond claude/opencode** — Extend `model` to
  another `agent_kind` + a validate-time existence check. →
  [`018`](docs/process/issues/018-model-selection-per-job.md)
- **Log rotation** — Size/age rotation of `history.jsonl` + opt-in reports
  prune. → [`021`](docs/process/issues/021-log-rotation.md)
- **API / webhook trigger** — PR #162, 2026-10-15. Shipped as the
  poll-based `github_event` gate check (`prs:labels` | `issues:opened` |
  `ci:failed`), generalizing issue 015's gh-api polling; no listener, no tunnel. →
  [`014`](docs/process/issues/014-api-webhook-trigger.md)
- **Docker image for trivial multi-host setup** — PTY-in-container worry
  resolved; waits on a secret-injection + image-architecture decision, and no
  live demand (hp migration paused). →
  [`017`](docs/process/issues/017-docker-image.md)
- **Concurrency beyond the single tick lock** — `history.jsonl` (2026-08-28)
  shows median 14s start delay, no starvation — the single tick lock is
  working as intended. → [`019`](docs/process/issues/019-concurrency-beyond-tick-lock.md)
- **Web / TUI dashboard** — CLI inspection is enough so far; the friction
  trigger hasn't been hit. → [`020`](docs/process/issues/020-web-tui-dashboard.md)

## Explicitly out of scope for now

- Connectors (MCP/skill config) — CLI agents already carry whatever they're configured with;
  no equivalent needed.
- A hosted/cloud environment equivalent to Claude's sandbox — this always runs on the Pi.

## Parking lot

Anything else noticed while actually running jobs — add a bullet here, promote to
Now/Next/Later once it's clear it's worth designing properly. Curated into issue
files 2026-08-27.

- **Switch provider/model on quota exhaustion** — PR #65. Per-job
  `fallback_model` retried once on a classified `quota_exhausted` settle;
  free-tier OpenCode quota modals are the dominant real failure mode. →
  [`022`](docs/process/issues/022-switch-model-on-quota-exhaustion.md)
- **Replace the herdr-push Telegram plugin** — `herdr.push` removed. The
  live bot is `relay/herdr_telegram.py` from the `guidodinello/herdr-remote` fork,
  not the `cokekitten/herdr-telegram-bridge` plugin first recorded (not installed;
  see issue 024's 2026-10-16 log). Residual laptop `herdr.push` cleanup is logged
  in the file. →
  [`023`](docs/process/issues/023-replace-herdr-push-telegram.md)
- **Spawn a session on the fly from Telegram** — PR #163, 2026-10-15.
  `/run <alias>` / `/start <alias>` from the one allowlisted chat starts an agent
  in a fresh worktree (or the checkout, `workspace: root`) via the
  `spawn-from-telegram` entrypoint, which the bridge must call per inbound
  message (see `deploy/README.md`). →
  [`024`](docs/process/issues/024-spawn-session-from-telegram.md)
- **Review `@me` PRs across repos** — PR #159, 2026-10-15; ships disabled as
  `deploy/jobs.d/review-my-prs.yaml`. A cross-repo `kind: review` job: one batched `gh` search for open PRs authored by me
  (or `review-requested`) across all repos, one worktree per dispatched PR, one
  posted review each. Distinct from `babysit-prs` (issue 015), which only watches
  PRs a routine itself opened (`auto/*`); that `auto/` slice is excluded here too,
  so pipeline PRs aren't reviewed twice. The design's hard part is that a review
  has no self-clearing gate: a content marker plus a per-`owner/repo#N` attempt
  budget is what makes it a finite loop. 2026-08-30 brainstorm. →
  [`055`](docs/process/issues/055-review-me-prs-across-repos.md)
- **Audit skills as report→diff gate jobs** — Phase A and B, 2026-10-10.
  New `kind: audit` jobs run an audit skill/command in a throwaway worktree, parse
  the findings manifest it writes to `$ROUTINE_FINDINGS`, and diff it against a
  per-job ledger: one fix worker is dispatched only for *new*, *regressed* or
  still-queued findings, so a stable pre-existing backlog never re-opens a PR.
  Phase A (PR #156) landed the config, manifest contract and the diff/ledger half
  (`findings.py`); phase B,
  [`058`](docs/process/issues/058-audit-fix-worker.md), runs the audit and
  dispatches the fix worker. Turns fitted's audit
  skills (`type-health`, `ui-ux-review`, `accessibility-review`, `fix-ignores`,
  `discover-conventions`, `improve-codebase-architecture`, `api-gap-audit`) into
  scheduled jobs. Builds on issue 025's gate model ("all checks pass → free").
  2026-08-30 brainstorm. →
  [`057`](docs/process/issues/057-audit-skills-as-report-diff-gate-jobs.md)
- **Release/update strategy for herdr-routines + plugins** — The runner
  fast-forwards itself to CI-green `main` nightly (self-update timer,
  [`053`](docs/process/issues/053-runner-checkout-self-update.md)),
  with [`docs/process/pi-update-runbook.md`](docs/process/pi-update-runbook.md)
  as the manual fallback. Plugins, the herdr CLI and `jobs.yaml` stay
  explicit, never auto-mutated. 2026-08-30 brainstorm.
- **Code-level pipeline gates (prompt → enforcement)** — folded into
  [`054`](docs/process/issues/054-orchestrator-mechanical-steps-to-code.md),
  whose phase B runs each stage's gate in code instead of trusting the
  orchestrator prompt. First slice landed 2026-09-07 (PR #109): `tick`
  reconcile enforces the G-17 stage-independence gate
  (`validate_stage_sessions`). 2026-08-30 brainstorm.
- **Inspect-only CLI for review candidates** — a read-only subcommand that runs
  the `kind: review` job's candidate scan and prints every PR it found with the
  reason it would or would not be dispatched, without spawning anything.
  Answers "why didn't it review PR X?". Deferred in issue 055.
- **Close issue 057's two open review notes** — layer the write-ahead ledger
  ordering test (criterion 9) between the pure `apply_diff` test and a
  tick-level one, and add a criterion plus test asserting the shape of the
  `gate` values and the history `extra` fields. Left open when 057's refine
  loop hit its cap.
- **Stable finding `id` from the audit skills** — without it, same-kind
  findings in one file collapse into one fix dispatch (issues 057, 058). The
  skills live in the target repo. Gate: the audit ledger holds real findings
  that collide.
- **Quota fallback chain and `agent_kind` override** — more than one
  `fallback_model`, tried in order (issue 022). Gate: a single fallback model
  proves insufficient.
- **`pick-feature --check`** — a read-only mode that also reclaims stale
  claims (issue 052). Gate: the current behaviour gets annoying in practice.
- **Resume a pipeline run whose orchestrator died** — the stall watchdog only
  kills a dead run and reports it as not resumed (issue 031). Now that
  `pipeline-run` owns the stage loop and persists resumable state, restart the
  run from its last completed stage instead, or at least write a partial report.
  Deferred in issue 031 and `docs/pipeline/design.md`.
- **Missed-run alert in the digest** — flag an enabled job whose last run is
  well past its own schedule ("should have run, didn't"), shown separately from
  failures. Suggested and deferred in issue 046; reads only the existing
  history, reports and job config.

House rule: anything a plan document explicitly defers ("out of scope", "v2 item", "deferred
to v1.5") gets a bullet here the day the plan lands, with its gate — so no deferred work lives
only inside `docs/`.
