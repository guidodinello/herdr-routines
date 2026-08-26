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

Ready to build whenever; no real-run evidence required. Curated into
per-item files as of 2026-08-25 — see
[`docs/process/issues/`](docs/process/issues/) for full description, update
log, and acceptance criteria; this section is a one-liner index only
(pattern matches `~/projects/PENDING.md`).

- **Overnight feature-pipeline orchestrator (POC)** — in progress, 4 real
  dogfood runs so far. → [`docs/process/issues/004-overnight-feature-pipeline-poc.md`](docs/process/issues/004-overnight-feature-pipeline-poc.md)
- **Failure reaping & quota-exhaustion handling** — phase 1 shipped, phase 2
  (watchdog) gated on phase 1 surviving a real overnight cycle. → [`docs/process/issues/005-failure-reaping-phase-2-watchdog.md`](docs/process/issues/005-failure-reaping-phase-2-watchdog.md)

Shipped and removed from this list during curation (stale — `ROADMAP.md`
hadn't been updated after landing): plugin manifest (PR #29), worktree GC
dry-run (PR #28), status CLI table view (PR #41, #43). Kept as `status: done`
issue files for history — see
[`docs/process/issues/001-plugin-manifest.md`](docs/process/issues/001-plugin-manifest.md),
[`002-worktree-gc-dry-run.md`](docs/process/issues/002-worktree-gc-dry-run.md),
[`003-status-cli-table-view.md`](docs/process/issues/003-status-cli-table-view.md).

## Next

Worth designing once their gate clears — usually "a few weeks of real runs on the Pi".

- **Split `jobs.yaml` into one file per job** (2026-08-25 idea) — `jobs.d/<name>.yaml` discovered
  by directory listing, filename doubling as (or validated against) the job's `name`, instead of
  one monolithic `jobs.yaml` list. Editing a single job today means hand-editing a block inside a
  bigger file (fragile for scripted edits — flipping `herdr-pr-review`'s `enabled` flag tonight
  needed a regex substitution rather than a plain file write); disable-by-rename or `git mv` is
  more legible, and a syntax error in one job's file can't break parsing of the others, unlike one
  shared YAML document today. Shared fields (`agent_kind`, `workspace`, `timezone`, etc., today's
  top-level `defaults:` block) become a sibling `defaults.yaml` that the loader merges under each
  job file's own fields, rather than duplicating those fields into every job file. Real work, not
  a config reshuffle: `src/herdr_routines/config.py` needs the directory-discovery + merge logic,
  filename/`name` consistency validation, and `validate`/`status`/`history` need to stop assuming
  a single file path. Gate: job count growing enough that the monolithic file is real friction —
  at today's 3 jobs it's mildly annoying, not painful.
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
  like. **2026-08-25 note:** gate is closer to clearing than it looks — the overnight pipeline
  POC's own manual-monitoring loop tonight (repeated 2-5 min check-ins across three dogfood runs)
  is exactly the noisy-ping experience this item is meant to fix; an unattended overnight run
  should push exactly one notification (the final report/PR link, or a failure), not a stream of
  progress pings. Bundle with the Parking Lot's Telegram-bot item below — the notification
  *policy* (what to send) and the *transport* (how it reaches the phone) are separate decisions
  but land in the same place.
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

- **Autonomous task selection for the pipeline** (2026-08-25 idea) — every dogfood run so far
  (5, as of tonight) needed a human to hand-author a `FEATURE_IDEA` file before launch. For the
  pipeline to genuinely run nightly unattended, stage 0 needs to pick its own next feature rather
  than waiting on a human to name one. **`fitted-implementer`'s pattern doesn't transfer
  directly:** it picks from `docs/process/issues/`, a directory of already-drafted issues with
  `status:`/`priority:` frontmatter — that structured layer and the drafting process behind it
  don't exist for `herdr-routines`, which only has `ROADMAP.md`, one higher-level prose document
  with no per-item files. Two real options, not yet decided between: (a) build the equivalent
  structured layer here too (split `ROADMAP.md`'s `Now` bullets into individual drafted files
  with frontmatter, mirroring `fitted`'s convention) — real prerequisite work, not just "reuse the
  pattern," since the drafting step is what's actually missing, not the selection logic; or
  (b) have stage 0 make a judgment call reading `ROADMAP.md`'s `Now` section directly, the same
  way the orchestrator already exercises judgment at other gates (e.g. gate 5's relaxed
  pass-with-note) — less mechanically verifiable than frontmatter, but no new structured layer to
  build or keep in sync with the prose. Trigger: the pipeline is actually promoted out of POC and
  running on a schedule — before that, a human picking the feature each run is a feature (keeps
  prioritization judgment in the loop), not a gap to close.
  **2026-08-25 update:** option (a)'s structured layer now exists —
  [`docs/process/issues/`](docs/process/issues/) curates the `Now` horizon into frontmatter'd
  files (`id`/`title`/`status`/`priority`/`area`/`gate`), same shape as `fitted`'s convention.
  Curation also caught 3 stale `Now` entries that had already shipped (`ROADMAP.md` wasn't
  updated after landing) — a structured, git-log-cross-checkable layer is easier to keep honest
  than prose.
  **Second 2026-08-25 update — deliberate partial trigger-cross:** `herdr-routines pick-feature`
  now exists (highest-priority, lowest-id `status: open` issue → `FEATURE_IDEA` text,
  `--mark-in-progress` to claim it) and the orchestrator prompt falls back to it when no
  `FEATURE_IDEA` is supplied (`docs/pipeline/orchestrator-prompt.md` § Inputs). This closes the
  "which feature" half of stage-0 selection *before* the item's own trigger cleared — a
  conscious choice to unblock experimentation with exactly one open issue in the backlog at the
  time (low blast radius: one candidate, easy to review), not a quiet reversal of the trigger.
  What it does **not** do: make the pipeline self-scheduling. A human (or a `systemd-run
  --on-calendar` the human configures) still decides *when* a run happens; only *which*
  Now-horizon item it builds is now automatable. The original trigger — several real overnight
  runs finishing end-to-end without human rescue — still gates actually wiring this into a
  recurring schedule; until then this is a manually-invoked helper a human chooses to use per
  launch, not unattended autonomy.
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
- **Docker image for trivial setup** (2026-08-25 idea) — bundle `herdr-routines` so standing it
  up on a new machine is "run the image," not `uv sync` + copy/edit `jobs.yaml` + install the
  systemd units + separately install and configure Herdr itself. Real open question before
  committing to this, not just packaging effort: **does Herdr itself run cleanly inside a
  container?** This tool doesn't run agents directly — it drives Herdr, which manages real
  terminal panes/PTYs for the `opencode`/`claude` processes it spawns (`herdr pane`/`agent`
  commands throughout this codebase). If Herdr needs host-level TTY/session semantics that don't
  survive containerization cleanly, a Docker image would only wrap the thin Python
  scheduler half of the stack and leave the actually-fiddly half (Herdr install + its own auth,
  `gh`/git SSH auth, model-provider auth) still manual — worth checking against Herdr's own docs
  before assuming this is a straightforward `Dockerfile`. Related to the `repository: <git-url>`
  item above (same "new host" trigger, same underlying motivation) but distinct in scope — that
  one is about the clone lifecycle, this one is about the whole runtime environment. Trigger: a
  second host, same as the repository-field item, once the Herdr-containerization question above
  is actually answered.
- **Model selection per job, beyond claude/opencode** — `model` is wired through to
  `agent_start` for `agent_kind: claude`/`opencode` (the only two kinds with a pinned-down
  native flag, see `AGENT_MODEL_FLAGS` in `config.py`). Extending to other agent kinds, or
  adding a model-catalog/existence check. Trigger: actually wanting either.
- **Concurrency beyond the single tick lock** — the blocking tick means one long job delays
  other jobs' start times by up to one run; acceptable at a handful of nightly jobs. If it
  stops being acceptable, the fix is per-job units, not a daemon (plan-v1.md §3). Trigger: a
  job regularly starving others.
- **Web/TUI dashboard** — the status/history CLI covers inspection at current scale. See the Now
  item "Status CLI, table view" for a cheaper first step (plain tables, no web/TUI framework).
  Trigger: reaching for `status` feels like friction, not ritual, even after that table view
  exists.
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
- **Replace the herdr-push Telegram plugin — decided, installed on the Pi (2026-08-25)**:
  went with an existing community plugin, `cokekitten/herdr-telegram-bridge`, rather than
  building a bot from scratch — it already does exactly the bidirectional/long-polling shape
  this item wanted (outbound-only `getUpdates`, no inbound webhook, so the Pi's lack of inbound
  reachability is a non-issue), reply-to-steer any agent pane from Telegram (covers the
  "spawn a session on the fly" item below — steering an already-running `herdr-routines` worker
  pane via reply, not literally spawning a new one from a cold message yet), and notifies on
  `done`/`blocked` pane transitions (covers the notification-policy want: one push per
  run-completion or error, not per-step noise). Source-reviewed before install: full docs in
  `agent-orchestrator-research/herdr/security-reviews.md`. Config skeleton is in place
  (`~/.config/herdr/plugins/config/telegram.bridge/config.toml` on the Pi) but the bot
  token/chat_id still need to be filled in by hand — not done yet.
  **Open verification item**: this plugin's notification hook is `pane.agent_status_changed`,
  which fires on raw Herdr pane state. The pane-lifecycle-v2 work above (`execute_run` closing
  a job's pane immediately on settling, `done`/`no_report`) could race against this hook seeing
  the status before the pane closes. Needs confirming with a real routine run before trusting it
  for overnight jobs — if it turns out unreliable, the fallback is wiring the notification off
  `herdr notification show` (the routine's own explicit completion signal) instead of pane
  status, which this plugin doesn't currently listen for.
  **2026-08-25 update**: bot token/chat_id moved over from the old `herdr-remote` secrets file
  (it was independently deployed on the Pi too, not just the laptop) — test notification
  confirmed received, reply poller confirmed running. `herdr.push` uninstalled from the Pi (it
  was a silent no-op once `herdr-remote`'s relay was removed). Laptop still has `herdr.push`
  installed and equally dead — same cleanup still pending there.
- **Spawn a session on the fly from Telegram** — partially covered by `herdr-telegram-bridge`
  above (reply-to-steer an existing pane). What's still missing: starting a *brand new* session
  against a named repo from a cold message (no existing notification to reply to) — needs a
  mapping from "which repo/job" to `herdr workspace create` + `agent start`, roughly the same
  mechanics the pipeline launcher already uses. Smaller remaining scope than originally
  written, now that the transport question is settled.

House rule: anything a plan document explicitly defers ("out of scope", "v2 item", "deferred
to v1.5") gets a bullet here the day the plan lands, with its gate — so no deferred work lives
only inside `docs/`.
