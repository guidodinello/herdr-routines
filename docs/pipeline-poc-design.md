# Pipeline POC — Design Draft (v1)

Status: draft (2026-08-23). POC scope: free models only, quota out of scope.
Canonical spec: [`docs/feature-pipeline-orchestrator-spec.md`](feature-pipeline-orchestrator-spec.md) (mirrored at `~/projects/raspberrypi/feature-pipeline-orchestrator-spec.md`).
Roadmap tracking: `ROADMAP.md:31` (Now, gate: a few overnight runs end-to-end without human rescue).

## Goal

Prove one feature can go from a one-paragraph idea to a reviewed PR overnight
through a chain of fresh-context agent sessions, driven by a single orchestrator
that spawns workers via `herdr` CLI and judges gates itself. No hard gate
enforcement in code for v1 — define the workflow, let the orchestrator supervise.

## Non-goals (v1)

- Generic DAG/pipeline YAML, parallel stages, unattended merge, auto-retry loops.
- `pipeline.py` / `tick` integration. `herdr-routines` cron stays out (spec §3).
- Programmatic gate harness (`verify-gate.sh` is a v2 opt-in, not v1).
- Any change to `src/herdr_routines/*` for the POC. The POC runs as herdr
  sessions/panes; `herdr-routines` only supplies the patterns to copy.

## Architecture

```
 human idea (one paragraph)
       │
       ▼
 orchestrator herdr session (opencode/big-pickle, lean)
       │  spawns via herdr CLI, polls coarse, writes state.json + $PIPELINE_REPORT
       ├─→ stage 1 plan/spec  (claude)
       ├─→ stage 2 spec review (opencode, different model — independence)
       ├─→ stage 3 implement+tests (opencode)
       ├─→ stage 4 open PR (same session as 3)
       ├─→ stage 5 code review (separate session, code-review skill)
       └─→ stage 6 address comments (capped 2 iterations)
```

Why agentic orchestrator over a Python driver (`src/herdr_routines/runner.py`
pattern): between-stage judgment (rewrite prompt after weak spec review,
addressable vs. needs-human on review comments, retry vs. abort) is where we
want intelligence. A bash loop cannot do that. Tradeoffs accepted per spec §2:
orchestrator burns context across the night; if it dies the run halts
(mitigated by checkpointing, not by pretending it can't happen).

## Workflow definition (v1)

No config parser. Stages are **hardcoded in the orchestrator prompt** — a
numbered list with input/output/gate_hint per stage. Promotion to
`workflows/pipeline.yaml` (`{name, agent_kind, model, timeout_ms, prompt_file,
gate_hint}`) only after the pattern survives a few runs.

Hardcoded stages (mirrors spec §3, stages mirror
`fitted/.claude/skills/feature-workflow` split across sessions):

| # | Stage | Worker harness | Input | Output | Gate hint (orchestrator judges) |
|---|-------|----------------|-------|--------|---------------------------------|
| 1 | Plan + draft spec | `claude` | idea paragraph | `spec.md` v1 | file exists, non-empty |
| 2 | Spec review + update | `opencode` (≠ stage 1) | spec v1 | spec v2 + Acceptance criteria & test plan (numbered, each → ≥1 named test) | reviewer posted updated spec + change notes |
| 3 | Implement | `opencode` | spec v2 | branch commits + tests from acceptance section | all spec-derived tests pass locally |
| 4 | Open PR | same session as 3 | branch | PR | `gh pr view` exists |
| 5 | Code review | separate session (code-review skill, confidence + blocking tiers) | PR number | posted review | review posted (blocking allowed) |
| 6 | Address comments | same agent as stages 3–4, prompted with review digest + `gh pr view --comments` (preserves code context; audit gap 8) | review findings | fixes + `gh api` thread-resolve + replies | gate 6: no unresolved blocking threads (or replies on every blocking finding per Gates) |

Stage rules copied from spec §3: tests before code (stage 3 done = every
acceptance test exists and passes), comment-addressal capped at 2 iterations +
**wait-for-comments 60 min** (audit gap 13: previously unspecified `spec:51`),
human gate stays at merge. Stage 5 code-review skill is multi-agent (5
reviewers) — for v1 run as single worker session; full 5-reviewer fan-out is v2
if needed (audit gap 13).

## Handoff contract (single shared branch — audit gap 1)

All inter-stage artifacts live on **one shared worktree+branch**, not per-stage
worktrees. Orchestrator creates it before stage 1:

```sh
herdr worktree create --cwd <parent-clone> --base main --branch auto/pipeline-<run_id>
# → shared path: ~/.herdr/worktrees/<branch> (recorded in state.json: shared_worktree, branch)
```

* Every worker attaches to that same path (either `herdr tab create --workspace <shared_ws>` or `herdr worktree create --cwd <shared_worktree>` reuse — no fresh `--base main` per stage). Fresh **sessions** give independence, not fresh worktrees.
* Stage 1 must `git add spec.md && git commit -m "spec: v1 for <feature>"` before settling — untracked files do not cross worktree boundaries (git shares object store, not untracked files). Stage 2 then sees the committed spec.
* Stage 3 continues on the same branch (commits implementation + tests); stage 4 PRs that same branch. Artifacts: `spec.md`, `state.json`, `$PIPELINE_REPORT` (all on the shared worktree; `$PIPELINE_REPORT` also mirrored to `~/.local/state/herdr-routines/reports/<run_id>.md` for the notification layer). Every worker prompt names input files explicitly — workers never rely on prior-session context, only on disk.

Same inversion as `docs/plan-v1.md:414` (`$ROUTINE_REPORT`): file is the only
reliable extraction channel because agents render on the alternate screen and
`herdr agent read` cannot recover scrolled output.

Empirical check at build time (2 min): confirm `git status --porcelain` in a
fresh worktree does not show another worktree's untracked files.

## Orchestrator session

- **Kind:** lean `herdr` session (`herdr workspace create --cwd <repo-parent>`,
  `herdr agent start --kind opencode --model big-pickle --pane <id>`), so it is
  watchable in the TUI. Bare `opencode` is the fallback, but herdr session is
  preferred for visibility.
- **Env & unattended allowlist (audit gap 3):** `HERDR_ENV=1` must be set or
  the orchestrator cannot drive `herdr` at all — inject via
  `herdr workspace create --cwd <parent> --label pipeline-orchestrator --env HERDR_ENV=1`
  (plan:63 `herdr workspace create [--env K=V]`), not via shell export.
  `~/.config/opencode/opencode.json` on **both laptop and Pi** must allowlist the
  full overnight command surface, or the first un-allowlisted call wedges as
  `blocked` at 03:00 (same class as `docs/failure-reaping.md:1`, `roadmap:54-61`):
  - `herdr` (spawn/poll/close), `git` (commit/add/log/rev-parse), `gh` (pr
    create/view/api + thread resolve), `uv`/`pytest`/`ruff`, `rg`/`jq`/`column`
  - `permission.external_directory` for `~/.config/herdr/herdr.sock`,
    `~/.local/state/herdr-routines/`, `~/.local/state/herdr/` and the
    on-demand clone cache dir (if stage 5 ever clones cross-repo — same fix as
    `raspberrypi/troubleshooting-log.md` external-directory `blocked`).
  Replicate the allowlist to the Pi's `~/.config/opencode/opencode.json` before
  the first overnight run and verify with a manual blocked-prompt dry-run.
- **Polling hygiene (audit gap 6):** do not hot-poll `herdr agent get` every
  30–60s (≈50–80 tool calls per 39-min review, burning orchestrator context).
  Instead: `nohup herdr agent prompt <worker> --wait --timeout <ms> > <stage>.result.json 2>&1 &`
  and sleep in 3–5 min chunks checking for the result file; fall back to
  `herdr agent get` only near timeout. If opencode's bash tool cannot block
  tens of minutes, the detached form is required — verify at build time (audit
  open question 3). One `agent prompt --wait` per stage is the cheap primitive
  (`docs/plan-v1.md:65`), not dozens of `agent get`.
- **Checkpointing & resume (audit gap 7):** `state.json`
  (`{current_stage, pr_number, artifact_paths, shared_worktree, branch, run_id}`)
  updated after every stage transition. `$PIPELINE_REPORT` written at end
  regardless of outcome (stage-by-stage status, artifacts, where it stopped and
  why) — same pattern as `$ROUTINE_REPORT` in `docs/plan-v1.md:386`. **Resume
  recipe:** on relaunch read `state.json` → `herdr agent list | rg "pl-"` to
  reconcile live `pl-<stage>-<run_id>` agents → adopt still-working worker or
  reap stragglers before re-spawning. Crash between spawn and state-write:
  handle duplicate-name collision via reap-before-spawn.
- **Pipeline deadline (audit gap 5):** hard wall-clock budget, e.g. launch + 7 h
  (tune to overnight window; audit open question 4). When exceeded, finish the
  current stage, write a **partial** `$PIPELINE_REPORT` and fire
  `herdr notification show --sound request` — since the report is "at end
  regardless" (`design:93`), an overrunning night with no deadline yields no
  report at all. Worst case 6×90 min + 2 loops >10 h without a deadline.
- **Quota handling (audit gap 4):** worker quota death already has timeout
  backstop (`design:109-112`); add reaping's visible-screen scan on settle-timeout:
  `herdr agent read <worker> --source visible --lines 200 | rg "Free usage exceeded"`
  (or `DEFAULT_FAILURE_MARKERS` from `docs/failure-reaping.md:88`) → report
  `quota_exhausted` not bare `timeout`. Orchestrator itself runs `big-pickle`,
  the model that died twice (`docs/failure-reaping.md:8-9`), in the longest
  role while owning `$PIPELINE_REPORT` — its own quota death is silent (no
  report). Accept for v1 but add morning checklist: "no report file ⇒ check
  `systemctl --user status` + `herdr agent list`" and keep v2 provider/model
  failover in Parking Lot (`roadmap:126-129`).
- **Cleanup (audit gap 12):** after `$PIPELINE_REPORT` is written and artifacts
  are committed/copied out, close worker panes/workspaces (`herdr workspace close`
  / `herdr worktree remove`); **keep the PR branch** `auto/pipeline-<run_id>`
  (aligns with manual GC stance `roadmap:77-79`). Order matters: artifacts out
  before workspace close. Leaked live `pl-*` agents were the central incident
  of reaping §1.

## Worker spawning (what must be ported, not rediscovered)

Mechanically solved — same `herdr` CLI calls `src/herdr_routines/herdr.py` +
`runner.py` use — but the failure handling must be copied verbatim:

1. **Start-race retry:** `interactive_ready` only means TUI drawn; prompts sent
   ~3s after `agent start` can still be rejected server-side ~5s in
   (`EmptyResponse`). Retry empty-response once, never resend on settle-timeout
   (runner.py #15, spec open question 2).
2. **Quota-retry disambiguation:** `Free usage exceeded` modal → API retry loop
   (`retrying in Xh`) never settles. Must be detected as `failed`/`stuck`, not
   waited on until `tick` timeout. Memory #1042 / `raspberrypi` engram 2026-08-23.
3. **Timeout backstop per worker:** `timeout_ms` (60–90 min for implement/review
   per Pi tuning: `fitted-pr-review` real 39m52s, `fitted-implementer` 90m) plus
   `start_timeout_ms 120s` for cold boots on Pi. Orchestrator enforces, not just
   `herdr agent prompt --wait --timeout`.
4. **Settle mapping:** both `idle` and `done` → success; `blocked` → needs-human;
   `unknown` → `interrupted_unknown` (verified empirically in `docs/plan-v1.md`
   §5, not SKILL.md's `done`/`idle` claim).

Each worker gets a unique agent name `pl-<stage>-<run_id>` (Herdr cap
`[a-z][a-z0-9_-]{0,31}`, unique among live agents — `docs/plan-v1.md:71`).

## Gates in v1 (orchestrator-supervised, machine-checkable forms — audit gap 2)

**Chosen: Option A — orchestrator judges, but against grep/rg/gh checkable
forms, not vibes.** `proceed | retry_stage | abort` is allowed only as
below; every gate-content failure is `abort` (no PR off failed upstream,
no address off failed review — `spec:57-59`).

| # | Gate (must all pass) | Check command (orchestrator runs it) |
|---|----------------------|--------------------------------------|
| 1 | `spec.md` exists, non-empty, committed | `test -f "$WT/spec.md" && wc -l "$WT/spec.md" && git -C "$WT" log --oneline -1 -- spec.md` |
| 2 | Spec v2 contains `## Acceptance criteria` with numbered items, each `Test: <name>`; change notes inside spec as `## Changelog v1→v2`; blocking tiers present | `rg -c "^[0-9]+\\. .*Test:" "$WT/spec.md"` ≥ N (N from count), `rg -q "^## Acceptance criteria" && rg -q "^## Changelog" && rg -q "blocking|non-blocking" "$WT/spec.md"` |
| 3 | Every named test from acceptance section exists (file/symbol) **and** suite passes | Extract `Test: <name>` → `rg -q "<name>" "$WT"` for existence; then `uv run pytest -q` (or repo's test cmd). Existence first, green second — audit gap 2: green alone proves nothing. |
| 4 | PR exists on the shared branch | `gh pr view <n> --json state,url,headRefName | jq -e '.headRefName=="auto/pipeline-<run_id>"'` |
| 5 | Review posted with structured confidence+blocking tiers (code-review skill) | `gh pr view <n> --json comments | jq -e '.comments[].body | test("blocking|non-blocking") and test("confidence")'` |
| 6 | No unresolved blocking findings (or replies exist on every blocking finding — pick one) | **Option A (preferred, checkable):** worker via `gh api` resolves threads it addressed; gate is `gh api repos/{owner}/{repo}/pulls/<n>/threads | jq -e '[.[] | select(.isResolved==false and .isBlocking==true)] | length==0'`. **Option B (fallback):** gate is "a reply exists on every blocking finding" via `gh pr view --json comments` count match. Chosen option must be written in orchestrator prompt; default is Option A when `gh api` thread access is confirmed at build. |

Stage rules: tests before code (stage 3 done = every acceptance test exists and
passes), comment-addressal capped at 2 iterations + 60-min wait-for-comments
timeout (audit gap 13: value now pinned), human gate stays at merge.

Retry policy (resolves `design:125` vs `spec:57` contradiction): `retry_stage`
allowed **only** for `EmptyResponse` start-race (once) and infra-flavored
transient test failures (once, e.g. network). Every gate-content failure (missing
criterion, missing test, unresolved blocking) is `abort` and writes
`$PIPELINE_REPORT` with where it stopped and why.

**Deferred to v2 (opt-in): Option B — `scripts/verify-gate.sh <stage>`**
deterministic oracle (`0/1`) that orchestrator can call before judging. One
bash call per transition; add only if first runs show orchestrator misses
checks — recommendation 2 above preempts most of its value. **Option C —
`src/herdr_routines/pipeline.py` typed `Gate` objects** only if we want `tick`
to own the pipeline as a scheduled job — explicitly out of scope for POC.

## Launcher (one-shot, not cron — audit gap 11)

`herdr-routines` cron is not used for the pipeline (`roadmap.md:32`). Two
equivalent one-shot options on the Pi (pick one for the POC, both are
`systemd-run` transient, no lingering unit). **Prerequisite:** `herdr agent
start` never creates layout (`docs/plan-v1.md:70-71`) — a pane must exist first;
parse `.result.root_pane.pane_id` from `herdr workspace create` output:

```sh
# A — pinned-date transient timer (visible in systemctl, survives SSH drop)
WS=$(herdr workspace create --cwd ~/.local/state/herdr-routines/repos/<target> \
  --label pipeline-poc-20260824 --env HERDR_ENV=1 | jq -r '.result.root_pane.pane_id')
systemd-run --user --on-calendar="2026-08-24 02:00:00" --timer-property=AccuracySec=30s \
  --unit=pipeline-poc-20260824 \
  bash -c "herdr agent start pipeline-orchestrator --kind opencode --pane $WS --timeout 120000 -- -m opencode/big-pickle && herdr agent prompt pipeline-orchestrator \"\$(cat orchestrator-prompt.md)\" --wait --until idle --timeout 25200000"

# B — manual one-liner from an existing herdr pane (simplest for first run)
WS=$(herdr workspace create --cwd ~/.local/state/herdr-routines/repos/<target> --label pipeline-poc --env HERDR_ENV=1 | jq -r '.result.root_pane.pane_id')
herdr agent start pipeline-orchestrator --kind opencode --model big-pickle --pane "$WS" --timeout 120000 -- -m opencode/big-pickle
herdr agent prompt pipeline-orchestrator "$(cat orchestrator-prompt.md)" --wait --until idle --timeout 25200000
```

For the POC the prompt file is checked out alongside the target repo worktree
so the orchestrator can reference it on disk. A `herdr-routines run orchestrator`
job wrapper is the later ergonomic improvement — not v1.

**Blast radius (audit gap 10):** worktree isolation shares the object store and
can push (`roadmap:58-61`); stage 4 requires push; dogfood target is
`herdr-routines` itself — the repo containing the scheduler. This is
consistent with the single-user threat model but must be stated: keep the first
feature trivial (build-order step 3) to bound blast radius while the orchestrator
prompt is green.

## Target repo & models (POC defaults)

- **Repo:** `herdr-routines` itself (dogfood, cheapest to verify). `fitted` is
  the stress test after the dogfood run succeeds — open question 1 in spec §5.
- **Models:** `stage 1 ≠ stage 2` harness for genuine independence
  (spec §5 Q4). POC: `1 claude` / `2 opencode/big-pickle` / `3 opencode` /
  `5 opencode/code-review` (or `claude` if review quality needs it).
- **Workspace:** orchestrator owns the parent clone
  (`~/.local/state/herdr-routines/repos/<name>`) and creates the single shared
  worktree+branch `auto/pipeline-<run_id>` before stage 1 (see Handoff contract).
  All workers attach to that same worktree — no per-stage `herdr worktree create
  --base main`. Stage 5 (review) also attaches to the shared worktree (read-only
  is not needed; review isolation comes from the session, not the worktree).
  This supersedes the earlier per-stage worktree sketch; the old
  `src/herdr_routines/config.py:workspace` `worktree` vs `root` distinction does
  not apply to the pipeline POC.

## Verification

1. Manual first run (watchable): launch orchestrator pane, tail
   `$PIPELINE_REPORT` and `state.json` after each stage. Expect 6 worker panes
   created sequentially, each settling to `idle`/`done`, spec.md v1→v2 diff
   visible, tests passing locally, PR opened, review posted.
2. Unattended overnight run: same launcher via `systemd-run --on-calendar`;
   check `herdr notification show` outcome + report file in morning.
3. Promotion gate (ROADMAP.md): a few real overnight runs end-to-end without
   human rescue before considering any `pipeline.py` or YAML promotion.

## Open questions resolved for v1

- Q2 driving → solved mechanically, failure handling ported as above.
- Q3 herdr session vs bare → lean herdr session (watchable).
- Q5 launcher → `systemd-run` one-shot (option A/B above).
- Q1/Q4 repo & models → `herdr-routines` dogfood + `claude`/`opencode` split as
  above; revisit after first run.

## Build order (when picked up)

1. ~~Copy this design + `~/projects/raspberrypi/feature-pipeline-orchestrator-spec.md`
   into `docs/`~~ — done: this doc + [`docs/feature-pipeline-orchestrator-spec.md`](feature-pipeline-orchestrator-spec.md) (canonical) now live in `docs/` on this branch (PR still required per `ROADMAP.md` ruleset).
2. Write `orchestrator-prompt.md` (the hardcoded stage list + spawn/poll/checkpoint
   instructions) and a minimal `state.json` schema.
3. First manual run against `herdr-routines` with a trivial feature idea; fix
   orchestrator prompt hygiene (poll interval, checkpoint writes) before any code.
4. Overnight `systemd-run` run; collect `$PIPELINE_REPORT`.
5. Decide on `verify-gate.sh` based on what the orchestrator actually missed.
