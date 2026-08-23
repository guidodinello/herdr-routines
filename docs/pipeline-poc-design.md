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
| 6 | Address comments | implementer-context session | review findings | fixes + replies | no unresolved blocking findings |

Stage rules copied from spec §3: tests before code (stage 3 done = every
acceptance test exists and passes), comment-addressal capped at 2 iterations +
wait-for-comments timeout, human gate stays at merge.

## Handoff contract

All inter-stage artifacts live in the repo worktree (committed or not):
`spec.md`, `state.json`, `$PIPELINE_REPORT`. Every worker prompt names input
files explicitly — workers never rely on prior-session context, only on disk.
Same inversion as `docs/plan-v1.md:414` (`$ROUTINE_REPORT`): file is the only
reliable extraction channel because agents render on the alternate screen and
`herdr agent read` cannot recover scrolled output.

## Orchestrator session

- **Kind:** lean `herdr` session (`herdr workspace create --cwd <repo-parent>`,
  `herdr agent start --kind opencode --model big-pickle --pane <id>`), so it is
  watchable in the TUI. Bare `opencode` is the fallback, but herdr session is
  preferred for visibility.
- **Env:** `HERDR_ENV=1` must be set or the orchestrator cannot drive `herdr`
  at all. `~/.config/opencode/opencode.json` needs
  `permission.external_directory` allowlist for the herdr socket/state dirs and
  for the on-demand clone cache if stage 5 clones cross-repo (same fix as
  `raspberrypi/troubleshooting-log.md` external-directory `blocked`).
- **Polling hygiene:** coarse `sleep 30–60s` between `herdr agent get <worker>`
  checks. Never per-minute tool-call polling — a 40-min review would otherwise
  eat the orchestrator's own context/quota. This is a load-bearing detail from
  spec §5 / open question 2.
- **Checkpointing:** `state.json` (`{current_stage, pr_number, artifact_paths}`)
  updated after every stage transition. `$PIPELINE_REPORT` written at end
  regardless of outcome (stage-by-stage status, artifacts, where it stopped and
  why) — same pattern as `$ROUTINE_REPORT` in `docs/plan-v1.md:386`.

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

## Gates in v1 (orchestrator-supervised)

**Chosen: Option A — orchestrator judges, no code gate.**

Orchestrator verifies by reading disk / running the command itself, then
decides `proceed | retry_stage | abort`:

- `test -f spec.md && wc -l spec.md` / `cat spec.md`
- `gh pr view <n> --json state,url` for PR existence
- `pytest -q` / `npm test` for stage 3 (run them, don't trust agent report)
- `gh pr view <n> --comments` + review thread state for stage 6

Failure semantics (spec §4): any gate not met → pipeline stops, never open a
PR off a failed upstream stage, never address comments off a failed review.
Orchestrator writes `$PIPELINE_REPORT` with where it stopped and why.

**Deferred to v2 (opt-in): Option B — `scripts/verify-gate.sh <stage>`**
deterministic oracle (`0/1`) that orchestrator can call before judging. One
bash call per transition; add only if first runs show orchestrator misses
checks. **Option C — `src/herdr_routines/pipeline.py` typed `Gate` objects**
only if we want `tick` to own the pipeline as a scheduled job — explicitly out
of scope for POC.

## Launcher (one-shot, not cron)

`herdr-routines` cron is not used for the pipeline (`roadmap.md:32`). Two
equivalent one-shot options on the Pi (pick one for the POC, both are
`systemd-run` transient, no lingering unit):

```sh
# A — pinned-date transient timer (visible in systemctl, survives SSH drop)
systemd-run --user --on-calendar="2026-08-24 02:00:00" --timer-property=AccuracySec=30s \
  --unit=pipeline-poc-20260824 \
  herdr agent start orchestrator --kind opencode --model big-pickle --pane <id> -- prompt.md

# B — manual one-liner from an existing herdr pane (simplest for first run)
herdr workspace create --cwd ~/.local/state/herdr-routines/repos/<target> --label pipeline-poc
herdr agent start pipeline-orchestrator --kind opencode --model big-pickle --pane <pane_id> -- < orchestrator-prompt.md
```

For the POC the prompt file is checked out alongside the target repo worktree
so the orchestrator can reference it on disk. A `herdr-routines run orchestrator`
job wrapper is the later ergonomic improvement — not v1.

## Target repo & models (POC defaults)

- **Repo:** `herdr-routines` itself (dogfood, cheapest to verify). `fitted` is
  the stress test after the dogfood run succeeds — open question 1 in spec §5.
- **Models:** `stage 1 ≠ stage 2` harness for genuine independence
  (spec §5 Q4). POC: `1 claude` / `2 opencode/big-pickle` / `3 opencode` /
  `5 opencode/code-review` (or `claude` if review quality needs it).
- **Workspace:** orchestrator owns the parent clone
  (`~/.local/state/herdr-routines/repos/<name>`); workers use
  `herdr worktree create --cwd <repo> --base main --branch auto/pipeline-<ts>`
  except stage 5 which may reuse `workspace: root` for review-only idempotence
  (`src/herdr_routines/config.py:workspace`).

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
