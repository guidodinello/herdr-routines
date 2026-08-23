# Roadmap

v1 (time-triggered jobs, YAML config, run history, systemd deployment, notifications via
`herdr notification show` — relayed off-box by the separately installed `herdr-push` plugin)
covers the core loop and is considered done — see [`docs/plan-v1.md`](docs/plan-v1.md) and the
README's Status section.

Items are grouped by horizon (**Now / Next / Later**) rather than version numbers: version
framing (v1.5/v2) buys nothing for a single-user tool with no external consumers. Each item
carries its **gate** — the condition that must hold before it's worth designing properly.
Promote items as gates clear; park brand-new ideas in the Parking Lot first.

Note that most Next items share one gate: **the Pi deployment running real jobs for a few
weeks** (tracked outside this repo). Laptop smoke tests don't produce the evidence those
decisions need.

## Now

Ready to build whenever; no real-run evidence required.

- **Plugin manifest (`herdr-plugin.toml`)** — actions-only (no startup hook, no daemon):
  invoke `herdr-routines run <job>` / `status` from inside the Herdr UI via keybinding or
  `herdr plugin action invoke`, and make the tool installable with
  `herdr plugin install guidodinello/herdr-routines`. Design already done in `plan-v1.md`
  §8.4; v1 made config/state paths env-var-aware (`HERDR_PLUGIN_CONFIG_DIR` /
  `HERDR_PLUGIN_STATE_DIR`) precisely so this needs no file moves later. Stays within the
  documented plugin model — systemd keeps owning the schedule.
- **Worktree GC, dry-run half** — `herdr-routines gc --dry-run`: list `auto/<name>-<ts>`
  branches that are merged or whose worktree is gone. Read-only and mechanical; useful as soon
  as the first real worktree jobs run. The deletion half is gated separately (see Next).
- **Overnight feature-pipeline orchestrator (POC)** — one orchestrator agent session drives an
   entire feature lifecycle by spawning per-stage worker sessions via herdr: plan/spec →
   independent spec review (adds acceptance criteria + test plan) → implement-until-spec-tests-pass
   → PR → code review → capped comment-addressal loop. Files-as-handoff, machine-checkable gates
   between stages, stop-on-failure semantics, checkpoint/resume. Full POC spec:
   [`docs/feature-pipeline-orchestrator-spec.md`](docs/feature-pipeline-orchestrator-spec.md) (canonical; mirrored at `~/projects/raspberrypi/feature-pipeline-orchestrator-spec.md` until Pi rollout settles). Design draft: [`docs/pipeline-poc-design.md`](docs/pipeline-poc-design.md) (v1: workflow hardcoded in orchestrator prompt, gates judged by orchestrator). Under Now because it needs no real-run evidence to attempt: every piece it
   composes (programmatic spawn/settle via runner.py's patterns, worktree jobs, gh-driven code
   review) already works in isolation — the missing thing is the integration, which is learned
   only by running it. Generalizes the auto-fix-PR idea (Later) into a full chain. Gate for
   promoting beyond POC: a few real overnight runs finishing end-to-end without human rescue.

- **Failure reaping & quota-exhaustion handling** — a failed run whose agent never settles
  (OpenCode free-quota modal; observed twice on the Pi, 2026-08-22/23) left a live `working`
  agent behind, so every later tick skipped the job (`agent_name_live`) until manual cleanup.
  Spec: [`docs/failure-reaping.md`](docs/failure-reaping.md). Phase 1 (reap own pane on
  failure, post-hoc quota classification, failure-path screen tails) ready to build; phase 2
  (mid-run fast-fail watchdog) is gated on phase 1 surviving a real overnight cycle and the
  dead wait mattering once more.

## Next

Worth designing once their gate clears — usually "a few weeks of real runs on the Pi".

- **Approval path for `blocked` runs** — a job blocked on an agent permission prompt ends as
  `blocked` and waits. Preferred direction (plan-v1.md §2): surface an actionable herdr-push
  notification so it can be approved from the phone (herdr-push's headline feature, already
  verified working end to end). The alternative — a skip-permissions / unattended auto-approve
  escape hatch — is deliberately unbuilt: scheduled + unattended + auto-approve is the one
  combination where a single bad prompt becomes an unreviewable repo mutation, and worktree
  isolation does not contain it (worktrees share the object store and can push). Reconsider
  auto-approve only if the phone-approval path proves too slow in practice.
- **Retries on failure** — transient failures (server down mid-run, startup timeout) currently
  end the run. Gate: real failure data showing which failures are actually transient — a retry
  can't fix a bad prompt, and blindly rerunning a non-idempotent job is worse than not
  retrying.
- **Notification policy per job** — decide what "worth telling you" means per job (only on
  failure, or only on a non-trivial finding) instead of a ping on every run; Claude Routines
  frames its notification toggle the same way. Gate: enough real runs to know what noise looks
  like.
- **Daily digest** — aggregate terminal states + report links into one morning summary instead
  of (or alongside) per-run notifications. Natural home is the herdr-push/Telegram relay.
  Gate: enough nightly jobs that per-run pings are noise.
- **Pane/session retention policy** — deliberately punted (conversation 2026-08-21/22):
  capture the full transcript to the run-history log as soon as a run finishes, then close the
  pane; actual cleanup timing (immediate vs. keep-for-a-week vs. manual) needs a few real runs
  before locking in.
- **Worktree GC, delete half** — opt-in deletion of what `gc --dry-run` lists. Nothing is ever
  removed automatically — a scheduled tool that deletes branches while I sleep is not a tool I
  want. Gate: several weeks of trusting the dry-run output.

## Later

Real designs, but untouched until something demands them. Each names its trigger.

- **API/webhook trigger** — Claude Routines supports "Call via API" (POST to trigger a run) and
  a GitHub-event trigger. Both assume inbound reachability, which the Pi doesn't have without a
  tunnel (declined earlier for the Telegram relay — see
  `../agent-orchestrator-research/herdr.md`). A GitHub-event-style trigger would likely start
  as **poll-based** (a timer checks `gh api` on a short interval and diffs) rather than a true
  webhook; a same-LAN "call via API" (small local HTTP endpoint, no tunnel) is more plausible
  short-term. Trigger: a recurring need to start runs from outside the cron model.
- **Auto-fix pull requests** — Claude Routines' "Behavior" toggle: "Watch CI and review
  comments on PRs this routine opens, and let Claude push fixes." The `babysit-prs` skill
  pattern as a standing job instead of something invoked manually. Trigger: a couple of
  scheduled review-style jobs have run for real and earned trust.
- **`repository: <git-url>` job field** — herdr-routines would own the clone lifecycle:
  idempotent clone-if-missing (likely under `~/.local/state/herdr-routines/repos/<name>`),
  pulled/kept up to date on each run, rather than requiring `repo:` to already exist on the
  host. Mainly for standing the tool up on a new host (or pointing a job at a repo not yet
  cloned there) without a manual `git clone` step; also makes jobs.yaml describe a job fully
  portably. Trigger: a second host, or such a job.
- **Model selection per job, beyond claude/opencode** — `model` is wired through to
  `agent_start` for `agent_kind: claude`/`opencode` (the only two kinds with a pinned-down
  native flag, see `AGENT_MODEL_FLAGS` in `config.py`). Extending to other agent kinds, or
  adding a model-catalog/existence check. Trigger: actually wanting either.
- **Concurrency beyond the single tick lock** — the blocking tick means one long job delays
  other jobs' start times by up to one run; acceptable at a handful of nightly jobs. If it
  stops being acceptable, the fix is per-job units, not a daemon (plan-v1.md §3). Trigger: a
  job regularly starving others.
- **Web/TUI dashboard** — the status/history CLI covers inspection at current scale. Trigger:
  reaching for `status` feels like friction, not ritual.
- **Log rotation** — a handful of jobs writing a few JSONL lines a day won't matter for years
  (plan-v1.md §5). Trigger: history.jsonl size becoming noticeable.

## Explicitly out of scope for now

- Connectors (MCP/skill config) — CLI agents already carry whatever they're configured with;
  no equivalent needed.
- A hosted/cloud environment equivalent to Claude's sandbox — this always runs on the Pi.

## Parking lot

Anything else noticed while actually running jobs — add a bullet here, promote to
Now/Next/Later once it's clear it's worth designing properly.

- **Switch provider/model on quota exhaustion** — react to `reason=quota_exhausted`
  (`docs/failure-reaping.md` §3.2) with a per-job failover model list, or degrade to a
  smaller tier for the rest of the window. Gate: quota failures recurring after failure-reaping
  phase 1 ships.

House rule: anything a plan document explicitly defers ("out of scope", "v2 item", "deferred
to v1.5") gets a bullet here the day the plan lands, with its gate — so no deferred work lives
only inside `docs/`.
