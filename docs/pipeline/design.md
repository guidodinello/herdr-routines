# Pipeline POC — Design Draft (v1)

Status: draft (2026-08-23). POC scope: free models only, quota out of scope.
Canonical spec: [`spec.md`](spec.md) (sibling in `docs/pipeline/`; mirrored at `~/projects/raspberrypi/feature-pipeline-orchestrator-spec.md`).
Roadmap tracking: `ROADMAP.md:31` (Now, gate: a few overnight runs end-to-end without human rescue). Orchestrator prompt: [`orchestrator-prompt.md`](orchestrator-prompt.md); audits: `audits/`; **one-time host/repo setup: [`setup.md`](setup.md)**.

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
 orchestrator herdr session (opencode/muse-spark-1.2-contributor-free, lean)
       │  spawns via herdr CLI, detached --wait, writes state.json + $PIPELINE_REPORT
       ├─→ stage 1 plan/spec  (muse-spark)
       ├─→ stage 2 spec review (muse-spark fresh session — independence via sessions, not model family)
       ├─→ stage 3 implement+tests (ox-alpha-free)
       ├─→ stage 4 open PR (same session as 3)
       ├─→ stage 5 code review (separate session, big-pickle primary — fan-out hy3+x-preview is v2)
       └─→ stage 6 address comments (ox fixes + muse GH ops, capped 2 iterations)
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
| 1 | Plan + draft spec | `opencode/muse-spark-1.2-contributor-free` | idea paragraph | `spec.md` v1 | file exists, non-empty |
| 2 | Spec review + update | `opencode/muse-spark-1.2-contributor-free` (fresh session, ≠ stage 1) | spec v1 | spec v2 + Acceptance criteria & test plan (numbered, each → ≥1 named test) | reviewer posted updated spec + change notes |
| 3 | Implement | `opencode/x-preview-f-free` (= `ox-alpha-free`, alias per `opencode-e2e:17`) | spec v2 | branch commits + tests from acceptance section; **flips picked issue `status: open`→`done`** | all spec-derived tests pass locally; picked issue file committed as `status: done` |
| 4 | Open PR | same session as 3 | branch | PR **carrying the issue-close commit** | `gh pr view` exists |
| 5 | Code review | separate session (code-review skill) — **v1 single `opencode/big-pickle` primary** (measured 1/7, 5 high-sev uniques `pr4:106`); **v2 fan-out `hy3-free` + `x-preview-f-free` 2-tie** (`opencode-e2e:19`) | PR number | posted review | review posted (blocking allowed) |
| 6 | Address comments | same agent as stages 3–4, prompted with review digest + `gh pr view --comments` (preserves code context; audit gap 8) | review findings | fixes + `gh api` thread-resolve + replies | gate 6: no unresolved blocking threads (or replies on every blocking finding per Gates) |

Stage rules copied from spec §3: tests before code (stage 3 done = every
acceptance test exists and passes), comment-addressal capped at 2 iterations +
**wait-for-comments 60 min** (audit gap 13: previously unspecified `spec:51`) —
poll `gh pr view --json comments` (or `reviews` per G-3) every **5 min** for
60 min after stage 5 settles; gate stage 6 on the review's `blocking` findings,
not on arbitrary human comments arriving later; after 60 min with no review,
abort with partial report (G-12). Human gate stays at merge. Stage 5 code-review
skill is multi-agent (5 reviewers) — for v1 run as single worker session; full
5-reviewer fan-out is v2 if needed (audit gap 13).

**Issue close-on-merge (issue-lifecycle contract):** the picked issue's
`status: done` flip is **carried inside the implementing PR** (stage 3 commits
`open`/`in-progress` → `done` on `docs/process/issues/<file>`), so it lands on
`main` atomically **exactly when the PR merges** — merging the PR *is* closing
the issue. There is deliberately **no separate manual/post-merge flip step**:
that gap is what left issue 025 `open` after PR #56 implemented it, so tonight's
`pick-feature` would have re-picked an already-done feature. Because the flip is
a commit in the PR, a human *closed-without-merge* leaves `main` untouched (issue
stays `open`/`in-progress`) — correct. The implementer does the flip (stage 3),
and the orchestrator enforces it via **Gate 4** (standalone, not buried in Gate 3:
the flip only becomes a real "close" once the PR is open, so that's where it's
checked).

**Spec leakage (G-13):** stage 1's `spec.md` commit(s) on `auto/pipeline-<run_id>`
ride the PR opened in stage 4. Document as intentional — prefix spec commits
`spec:` so reviewers can trivially filter them. Stripping `spec.md` before PR is
not needed for dogfood.

**Spec path is per-run, not repo-root (G-15, added 2026-08-24 after PR #29's merge
conflict):** `spec.md` lives at `docs/pipeline/runs/<run_id>/spec.md`, never at the
repo root. Two real dogfood runs (`20260824T232136Z` → PR #28, `20260825T000735Z` →
PR #29) both wrote a full-file rewrite of a single root-level `spec.md`; since
PR #28 merged into `main` while PR #29's branch was still open (having branched
before that merge), PR #29 hit an unresolvable "both sides rewrote the whole file"
conflict on merge — not a content disagreement, a path collision. Any two pipeline
runs whose branches have overlapping history will hit this every time the design
uses one fixed path for a per-run scratch file. The per-run path removes the
collision entirely: each run's spec lives at its own path, so `main` only ever
gains new files, never edits to one a concurrent branch also touched.

## Handoff contract (single shared branch — audit gap 1)

All inter-stage artifacts live on **one shared worktree+branch**, not per-stage
worktrees. Orchestrator creates it before stage 1:

```sh
herdr worktree create --cwd <parent-clone> --base main --branch auto/pipeline-<run_id>
# → shared path: ~/.herdr/worktrees/<branch> (recorded in state.json: shared_worktree, branch)
```

* Every worker attaches to that same path via `herdr tab create --workspace "$SHARED_WS" --cwd "$WT"` (new tab in the shared workspace, cwd pinned to the shared worktree — verified `herdr tab create` syntax `herdr tab:12`). **Do not** use `herdr worktree create --cwd <shared_worktree>` to "reuse" — that form is invalid per `herdr worktree:3` (expects `--cwd` of an existing repo to link *from* plus `--branch` for a *new* worktree; pointing it at an already-linked worktree errors or duplicates branch) — G-6. Fresh **sessions** give independence, not fresh worktrees.
* Stage 1 must `git add docs/pipeline/runs/<run_id>/spec.md && git commit -m "spec: v1 for <feature>"` before settling — untracked files do not cross worktree boundaries (git shares object store, not untracked files). Stage 2 then sees the committed spec. **Per-run path, not repo root** — see G-15.
* Stage 3 continues on the same branch (commits implementation + tests); stage 4 PRs that same branch. Artifacts: `docs/pipeline/runs/<run_id>/spec.md`, `state.json`, `$PIPELINE_REPORT` (all on the shared worktree; `$PIPELINE_REPORT` also mirrored to `~/.local/state/herdr-routines/reports/<run_id>.md` for the notification layer). Every worker prompt names input files explicitly — workers never rely on prior-session context, only on disk.

Same inversion as `docs/plan-v1.md:414` (`$ROUTINE_REPORT`): file is the only
reliable extraction channel because agents render on the alternate screen and
`herdr agent read` cannot recover scrolled output.

Empirical check at build time (2 min): confirm `git status --porcelain` in a
fresh worktree does not show another worktree's untracked files.

## Orchestrator session

- **Kind:** lean `herdr` session (`herdr workspace create --cwd <repo-parent>`,
  `herdr agent start --kind opencode --model muse-spark-1.2-contributor-free --pane <id>`), so it is
  watchable in the TUI. Bare `opencode` is the fallback, but herdr session is
  preferred for visibility. Orchestrator model pinned to `muse-spark` per pi-2 e2e (`opencode-e2e:15` — muse excels at spec/arch reasoning; was `big-pickle` — keep as fallback if truncation/quota).
- **Env & unattended allowlist (audit gap 3):** `HERDR_ENV=1` must be set or
  the orchestrator cannot drive `herdr` at all — inject via
  `herdr workspace create --cwd <parent> --label pipeline-orchestrator --env HERDR_ENV=1`
  (plan:63 `herdr workspace create [--env K=V]`), not via shell export.
  `~/.config/opencode/opencode.json` on **both laptop and Pi** must allowlist the
  full overnight command surface, or the first un-allowlisted call wedges as
  `blocked` at 03:00 (same class as `docs/failure-reaping.md:1`, `roadmap:54-61`):
  - `herdr` (spawn/poll/close), `git` (commit/add/log/rev-parse), `gh` (pr
    create/view/api/graphql + thread resolve), `uv`/`pytest`/`ruff`, `rg`/`jq`/`column`,
    `herdr notification show` (deadline path)
  - `permission.external_directory` for `~/.config/opencode/**`,
    `~/.config/herdr/**` (`~/.config/herdr/herdr.sock`), `~/.herdr/worktrees/auto/pipeline-*`,
    `~/.local/state/herdr-routines/**` (incl. `reports/`), `~/.local/state/herdr/**` and the
    on-demand clone cache dir (if stage 5 ever clones cross-repo — same fix as
    `raspberrypi/troubleshooting-log.md` external-directory `blocked` — Pi probe hit `~/.config/opencode` `blocked` on first pipeline run, fixed 2026-08-23).
  - `GH_TOKEN` / `gh auth status` must be valid on the Pi — `gh pr create` and
    `gh api graphql` fail differently when unauthenticated and surface at 03:00
    as gate-4/6 failures, not permission wedges (G-5).
  **Transcription note (G-5 verification):** current `~/.config/opencode/opencode.json:3`
  only shows `permission.external_directory`; opencode's per-command `permissions` are
  pattern-based (e.g. `bash:herdr *` vs `bash:gh api *` are distinct) — the bullet
  list is an inventory checklist, not copy-paste JSON. Translate to actual
  `permissions` patterns and run one manual pipeline stage on the Pi under the
  real allowlist to confirm no `blocked` wedge before the overnight run.
- **Polling hygiene (audit gap 6):** do not hot-poll `herdr agent get` every
  30–60s (≈50–80 tool calls per 39-min review, burning orchestrator context).
  Instead: `nohup herdr agent prompt <worker> --wait --timeout <ms> > <stage>.result.json 2>&1 &`
  and sleep in 3–5 min chunks checking for the result file; fall back to
  `herdr agent get` only near timeout. If opencode's bash tool cannot block
  tens of minutes, the detached form is required — verify at build time (audit
  open question 3). One `agent prompt --wait` per stage is the cheap primitive
  (`docs/plan-v1.md:65`), not dozens of `agent get`.
- **Checkpointing & resume (audit gap 7 + G-9, updated G-16):** `state.json`
  (`{current_stage, pr_number, artifact_paths, shared_worktree, branch, run_id, deadline_epoch}`)
  updated **atomically** (write to temp + rename) after every stage transition.
  `$PIPELINE_REPORT` written at end regardless of outcome (stage-by-stage
  status, artifacts, where it stopped and why) — same pattern as
  `$ROUTINE_REPORT` in `docs/plan-v1.md:386`. **Resume recipe:** on relaunch
  read `state.json` → `herdr agent list | jq -r '.result.agents[] | select(.name | startswith("pl-")) | "\(.name) \(.agent_status)"'` (not `rg "pl-"` on JSON — G-9) to reconcile live `pl-<stage>-<run_id>` agents. Derive orphan stage from agent name suffix when `state.json:current_stage` lags spawn (crash between `herdr agent prompt` and state write). Adopt still-`working` worker if stage matches, otherwise `herdr pane close <pane_id>` (pane_close, not settled-agent reap — reaping `working` requires `pane_close` per `src/herdr_routines/herdr.py:34`). Atomic write prevents corrupt resume on crash mid-write. **Pane-lifecycle v2 (G-16):** a closed-but-resumable worker ("not found live, but its session ID is in `state.json`/history — reopen by `-s <session_id>` if this stage still needs it") is now a normal, expected case (per-stage close), not only a crash-recovery edge case; resume captures `agent_session.value` for `pl-3` so stage 6 can `herdr agent start pl-6-… -s <session_id>`.
- **Pipeline deadline (audit gap 5 + G-7):** hard wall-clock budget **launch + 7 h**
  (pinned default, written to `state.json:deadline_epoch` at orchestrator start;
  tune via open question 4). Orchestrator checks `date +%s` vs `deadline_epoch`
  between stages; when exceeded, **wait for the in-flight `herdr agent prompt --wait` to return** (do not kill mid-implementation), then skip remaining stages and write a **partial** `$PIPELINE_REPORT` + `herdr notification show --sound request` — since the report is "at end regardless" (`design:93`), an overrunning night with no deadline yields no report at all. Worst case 6×90 min + 2 loops >10 h without a deadline. `systemd-run --timeout 25200000` is a per-worker prompt timeout, not the pipeline deadline — wire `deadline_epoch` separately.
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
- **Cleanup (audit gap 12 + G-10, pane-lifecycle v2 G-16):** after `$PIPELINE_REPORT` is mirrored to
  `~/.local/state/herdr-routines/reports/<run_id>.md` and commits are on
  `auto/pipeline-<run_id>`, **close each worker's tab/pane** (`herdr tab close` /
  `herdr pane` close) — **do not** `herdr workspace close` the shared workspace
  nor `herdr worktree remove` the shared worktree (that would destroy the branch
  to keep). **Per-stage pane close on gate-pass (not only end-of-run) (G-16):** close a worker's pane as soon as its stage's gate has passed and the handoff (commit + `state.json` update) is confirmed on disk — close this worker's pane once its gate passes — for every worker except whichever one a later stage reuses for context. For the reused worker (`pl-3`, reused by stage 6 per G-11), close its pane too after stage 4, but when stage 6 needs it, reopen by resuming its session: `herdr agent start pl-6-<run_id> --kind opencode --pane <fresh_pane> -- -m <model> -s <session_id>` — where `<session_id>` is `agent_session.value` already captured in `state.json`/history for that worker — instead of keeping the pane open idle from stage 3 through stage 6. This close-then-resume-by-session-id mechanism was empirically verified 2026-08-25 (`-s <session_id>` true resume, `agent_session.value` matched exact session; proposal open question 1 settled). At most 2 opencode processes resident at any point (orchestrator + active stage) instead of one per stage. Open questions 2 (grace window before closing) and 3 (cross-model `-s` interaction) remain open — immediate close is the default; a short grace window is still permissible if scrollback reliance is later found, and cross-model `-s` is out of scope for current `pl-3`/`pl-6` same-model reuse. Order: (1) mirror report + ensure `auto/pipeline-<run_id>` has all
  commits, (2) close worker tabs/panes (per-stage as gates pass, plus final sweep for any still-open), (3) leave shared worktree+branch for
  manual GC per `roadmap:77-79`. Leaked live `pl-*` agents were the central
  incident of reaping §1. Future `herdr-routines gc` must exclude `auto/pipeline-*`
  (G-14).

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
`[a-z][a-z0-9_-]{0,31}`, unique among live agents — `docs/plan-v1.md:71`),
**except stage 6 which reuses the stage-3 session via close-then-resume-by-session-id (G-16)** via `herdr agent start pl-6-<run_id> --kind opencode --pane <fresh_pane> -- -m <model> -s <session_id>` where `<session_id>` is `pl-3`'s `agent_session.value` (`-s <session_id>` empirically verified 2026-08-25, true resume not fork; `agent_session.value` on resumed agent matched original). This preserves code context per G-11 without keeping the `pl-3` pane open idle from stage 3 through stage 6 — close a worker's pane as soon as its stage's gate passes (G-16), and reopen the one needed later by `-s <session_id>` against a fresh pane. Alternative fresh `pl-6-<run_id>` seeded with `git diff main...HEAD` + `gh pr view --comments` remains the fallback if context burn or cross-model `-s` becomes an issue (open question 3).

## Gates in v1 (orchestrator-supervised, machine-checkable forms — audit gap 2)

**Chosen: Option A — orchestrator judges, but against grep/rg/gh checkable
forms, not vibes.** `proceed | retry_stage | abort` is allowed only as
below; every gate-content failure is `abort` (no PR off failed upstream,
no address off failed review — `spec:57-59`).

| # | Gate (must all pass) | Check command (orchestrator runs it) |
|---|----------------------|--------------------------------------|
| 1 | `spec.md` exists, non-empty, committed on pipeline branch | `test -s "$WT/docs/pipeline/runs/$RUN_ID/spec.md" && test $(wc -l < "$WT/docs/pipeline/runs/$RUN_ID/spec.md") -gt 2 && git -C "$WT" rev-parse --abbrev-ref HEAD | grep -q "^auto/pipeline-" && git -C "$WT" log --oneline -1 -- "docs/pipeline/runs/$RUN_ID/spec.md" | grep -q .` |
| 2 | Spec v2 contains `## Acceptance criteria` with numbered items, each `Test: <name>`; change notes inside spec as `## Changelog v1→v2`; blocking tiers present | Count `Test:` via `rg -c "Test:" "$WT/docs/pipeline/runs/$RUN_ID/spec.md"` (≥ N, N derived by orchestrator counting `Test:` lines); headings via `rg -q "^## Acceptance criteria" "$WT/docs/pipeline/runs/$RUN_ID/spec.md" && rg -q "^## Changelog" "$WT/docs/pipeline/runs/$RUN_ID/spec.md"`; tiers via `rg -qw "blocking" "$WT/docs/pipeline/runs/$RUN_ID/spec.md" && rg -qw "non-blocking" "$WT/docs/pipeline/runs/$RUN_ID/spec.md" && rg -q "confidence:" "$WT/docs/pipeline/runs/$RUN_ID/spec.md"` (fixed: `-w` avoids substring tautology; see G-2 verification) |
| 1i/2i | **Stage isolation (G-17):** every stage marked independent in the workflow definition actually ran as a distinct worker — not the orchestrator authoring the content itself | `herdr agent list | jq -e --arg n "pl-<N>-<run_id>" '[.result.agents[] | select(.name==$n)] | length==1'` — a named `pl-<N>-<run_id>` agent must exist for stage `N`, and (for a stage the workflow does **not** declare as reusing an earlier stage's session) its `agent_session.value` must differ from every prior stage's recorded session id in `state.json`. Missing agent or a reused session where none was declared ⇒ gate-content failure, `abort` (same severity as 1/2), not a warning. |
| 3 | Every named test from acceptance section exists (file/symbol) **and** suite passes | Extract `Test: <name>` → `rg -F -q -- "<name>" "$WT/tests"` (fixed-string `-F`, scoped to `tests/` not `$WT` which contains `spec.md` itself — G-2); then `uv run pytest -q` (or repo's test cmd). Existence first, green second — audit gap 2: green alone proves nothing. |
| 4 | PR exists on the shared branch | `gh pr view <n> --repo <owner>/<repo> --json state,url,headRefName | jq -e '.headRefName=="auto/pipeline-<run_id>"'` |
| 5 | Review posted with structured blocking/non-blocking tiers (code-review skill) | `gh pr view <n> --json comments,reviews | jq -e 'any(.comments[].body // empty; test("blocking")) or any(.reviews[].body // empty; test("blocking"))'` — **relaxed after first real run** (`pipeline-20260823T234906Z`): the code-review skill posts `blocking`/`non-blocking` tier labels but no literal `confidence: high|medium|low` token, so the original `confidence:` regex false-failed; orchestrator judged pass-with-note. Keep `all(...)` shape for absence checks if a repo's review format adds confidence back; gate on what the skill actually writes. |
| 6 | No unresolved blocking findings | **Verified fix for G-1:** `gh api repos/{owner}/{repo}/pulls/<n>/threads` 404s (confirmed `gh: Not Found`). **Option A (GraphQL, preferred):** `gh api graphql -f query='query($owner:String!,$repo:String!,$pr:Int!){repository(owner:$owner,name:$repo){pullRequest(number:$pr){reviewThreads(first:50){nodes{isResolved comments(first:1){nodes{body}}}}}}}' -f owner=<o> -f repo=<r> -F pr=<n> | jq -e '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved==false and (.comments.nodes[0].body | test("blocking")))] | length==0'` — filter `isResolved==false` + body contains `blocking` (skill convention, not an API field). **Option B (fallback, no GraphQL):** `gh pr view <n> --json comments | jq -e 'all(.comments[].body; test("blocking")) | not or all(...replies...)` count-match. Delete the old REST `threads` line; pin one option in `orchestrator-prompt.md` after one real PR run. |

**Process fidelity, not just content shape (G-17):** every gate above (1-6)
checks the *shape of the output* — does `spec.md` have the right headings, do
named tests exist, is there a PR. None of them check *how* the output was
produced. On the pane-lifecycle-v2 dogfood run (`20260825T021919Z`, PR #31)
the orchestrator (`muse-spark-1.2-contributor-free`, free tier) skipped
spawning `pl-1`/`pl-2` entirely and wrote both spec versions itself in its own
session — Gates 1 and 2 both passed anyway, because a content-shape check
cannot distinguish "an independent reviewer wrote this" from "the same author
reviewed its own work." Stage 2 exists specifically for the latter
independence (`orchestrator-prompt.md` stage 2 harness note: "fresh session
... independence via sessions not model family"); collapsing it into stage 1
quietly defeats that, with no
prompt change required to trigger it — plain model non-determinism against an
unenforced instruction is enough. The general lesson, meant to generalize past
this one hardcoded 6-stage prompt: **any stage a workflow declares as needing
independent execution must be gate-verifiable by process identity (a distinct
agent name + session id), not merely trusted from the prompt.** Gate 1i/2i
above is the concrete instance for this v1's stage 1/2; a future declarative
workflow schema should carry this as a per-stage `isolation: independent |
reused:<stage>` field with one generic gate function reading it, rather than
re-deriving a bespoke check per hardcoded stage pair each time.

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
  bash -c "herdr agent start pipeline-orchestrator --kind opencode --pane \$WS --timeout 120000 -- -m opencode/muse-spark-1.2-contributor-free && herdr agent prompt pipeline-orchestrator \"\$(cat docs/pipeline/orchestrator-prompt.md)\" --wait --until idle --timeout 25200000"

# B — manual one-liner from an existing herdr pane (simplest for first run)
WS=$(herdr workspace create --cwd ~/.local/state/herdr-routines/repos/<target> --label pipeline-poc --env HERDR_ENV=1 | jq -r '.result.root_pane.pane_id')
herdr agent start pipeline-orchestrator --kind opencode --pane "$WS" --timeout 120000 -- -m opencode/muse-spark-1.2-contributor-free
herdr agent prompt pipeline-orchestrator "$(cat docs/pipeline/orchestrator-prompt.md)" --wait --until idle --timeout 25200000
```

For the POC the prompt file is checked out alongside the target repo worktree
so the orchestrator can reference it on disk. A `herdr-routines run orchestrator`
job wrapper is the later ergonomic improvement — not v1.

**Feature idea (2026-08-25):** neither launch command above appends a
`FEATURE_IDEA` — that's deliberate. The orchestrator prompt's Inputs section
now self-selects from `docs/process/issues/` (`herdr-routines pick-feature
--mark-in-progress`) when none is supplied, so an unattended `systemd-run`
launch (option A) works without a human pre-authoring an idea file. To force
a specific feature instead, append it to the prompt text before `cat`, e.g.
`"$(cat docs/pipeline/orchestrator-prompt.md)\n\nFEATURE_IDEA: <text>"`. See
`docs/pipeline/orchestrator-prompt.md` § Inputs and `ROADMAP.md` §
"Autonomous task selection for the pipeline" for scope and the promotion
gate this doesn't change.

**Blast radius (audit gap 10):** worktree isolation shares the object store and
can push (`roadmap:58-61`); stage 4 requires push; dogfood target is
`herdr-routines` itself — the repo containing the scheduler. This is
consistent with the single-user threat model but must be stated: keep the first
feature trivial (build-order step 3) to bound blast radius while the orchestrator
prompt is green.

## Target repo & models (POC defaults)

- **Repo:** `herdr-routines` itself (dogfood, cheapest to verify). `fitted` is
  the stress test after the dogfood run succeeds — open question 1 in spec §5.
- **Models (pi-2 e2e `opencode-e2e-workflow-recommendations.md:13`, interim until measured):**
  `1 muse-spark` plan/spec (`muse excels at spec, terrible at code-gen` `opencode-e2e:15`) /
  `2 muse-spark` fresh session spec review (independence via sessions, not model family — `1≠2` is `spec §5 Q4` via fresh session, not model family; `ox planning bad` `opencode-e2e:17` makes ox a poor spec reviewer) /
  `3 ox-alpha-free` (`opencode/x-preview-f-free` on Zen = `opencode-go/ox-alpha-free` `opencode-e2e:17`, 1M ctx, coding best) /
  `5 big-pickle` primary single reviewer v1 (measured 1/7 `pr4:106`), fan-out `hy3-free` + `x-preview-f-free` 2-tie is v2 (`opencode-e2e:19`, dedup `pr4:45` not yet built) /
  `6 ox` fixes + `muse` GH ops (`commit/push/reply` pattern #1040 `opencode-e2e:20`).
  Orchestrator itself: `muse-spark` (spec-like reasoning, not coding; was `big-pickle` — pi-2 suggests muse for planning) or `big-pickle` fallback if muse quota/truncation (`opencode-e2e:15` chunk prompts) — pin `muse` for first manual run.
- **Workspace:** orchestrator owns the parent clone
  (`~/.local/state/herdr-routines/repos/<name>`) and creates the single shared
  worktree+branch `auto/pipeline-<run_id>` before stage 1 (see Handoff contract).
  All workers attach to that same worktree — no per-stage `herdr worktree create
  --base main`. Stage 5 (review) also attaches to the shared worktree (read-only
  is not needed; review isolation comes from the session, not the worktree).
  This supersedes the earlier per-stage worktree sketch; the old
  `src/herdr_routines/config.py:workspace` `worktree` vs `root` distinction does
  not apply to the pipeline POC.

## First real run — findings (2026-08-23/24, `pipeline-20260823T234906Z`)

Dogfood `--version` flag run on the Pi completed all 6 stages end-to-end
(`docs/pipeline/features/version-flag-idea.md`, PR #27, report
`~/.local/state/herdr-routines/reports/pipeline-20260823T234906Z.md`). What it taught:

- **Silent orchestrator death is real.** The first orchestrator (`wS:p1`, muse) died
  between stage 4 and 5 with zero feedback: no error anywhere, just
  `herdr-server.log` `agent changed pane=28 → None`. No report was written (report
  is "at end regardless" — a killed orchestrator writes nothing). What saved the run:
  `state.json` checkpoint (stage 4, pr_number 27) + the resume recipe (`design:131`)
  — relaunched with same RUN_ID, reconciled live `pl-*` agents, continued at stage 5.
  **v1 hardening adopted:** orchestrator writes a heartbeat line to
  `/tmp/pipeline_resume_<run_id>.log` every poll; morning checklist "no heartbeat
  advance + no report ⇒ orchestrator died, resume from state.json". A future v2
  could move the deadline/report writing out of the orchestrator into the launcher
  wrapper so even a dead orchestrator produces a partial report.
- **Gate 5's `confidence:` regex doesn't match the skill's actual output.** The
  code-review skill posts `blocking`/`non-blocking` tier labels but never a literal
  `confidence: high|medium|low` string — the strict gate false-failed and the
  orchestrator had to judge pass-with-note. Gate relaxed to check tier structure
  only (see Gates table row 5). Lesson: pin gate regexes to observed output, not
  the skill doc's described format.
- **Commit signing must be automatic, not prompt-driven.** Repo ruleset requires
  verified signatures; workers committed as `opencode <opencode@herdr.local>`
  unsigned → merge BLOCKED. Fix: dedicated SSH signing key per host
  (`~/.ssh/herdr-routines-pipeline_signing`, registered once on GitHub as a
  **Signing key**), wired via **repo-local** `git config` on the parent clone
  (linked worktrees inherit it) — see [`setup.md`](setup.md). The orchestrator
  prompt does not touch signing config; new repos need the one-time
  `setup.md` steps.
- **Allowlist gaps surface as mid-run `blocked` prompts**, each costing a human
  tap: `~/.config/opencode/**` (orchestrator reads its own config dir),
  `/etc/**` (only when probing a missing binary like `rg` — avoid by pre-installing
  tooling), plus the expected worktree/report/socket dirs. Current Pi allowlist:
  `/tmp/**`, `~/.claude/rules/**`, `~/.config/opencode/**`, `~/.config/herdr/**`,
  `~/.herdr/worktrees/**`, `~/.local/state/herdr/**`,
  `~/.local/state/herdr-routines/**` (+`reports/**`). Keep `/etc` out unless a
  stage genuinely needs it.
- **`rg` must be pre-installed on target hosts** (Pi lacked it → gates would have
  failed; installed `ripgrep 14.1.1` to `~/.local/bin/rg`). Add to `setup.md`.
- **Stage 6 reuse of `pl-3` worked well** (code context preserved, fix pushed,
  thread resolved via GraphQL). Detached `nohup --wait` + result-file polling kept
  orchestrator context at ~6% across the whole night.

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
   into `docs/pipeline/`~~ — done: this doc + [`docs/pipeline/spec.md`](spec.md) (canonical) now live in `docs/pipeline/` on this branch (PR still required per `ROADMAP.md` ruleset).
2. Write `orchestrator-prompt.md` (the hardcoded stage list + spawn/poll/checkpoint
   instructions) and a minimal `state.json` schema.
3. First manual run against `herdr-routines` with a trivial feature idea; fix
   orchestrator prompt hygiene (poll interval, checkpoint writes) before any code.
4. Overnight `systemd-run` run; collect `$PIPELINE_REPORT`.
5. Decide on `verify-gate.sh` based on what the orchestrator actually missed.
