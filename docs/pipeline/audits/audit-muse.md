# Pipeline POC — Second Audit (muse)

Status: audit (2026-08-23). Reviewer: muse (second pass, after fixes for x-preview first audit).
Scope: `docs/feature-pipeline-orchestrator-spec.md` (spec), `docs/pipeline-poc-design.md` (design) as amended by commit `74a84e5` on branch `docs/pipeline-poc-design`, against `docs/pipeline-audit-xpreview.md` (first audit, 14 gaps / 3 blockers), `docs/failure-reaping.md` (reaping), `ROADMAP.md`, `docs/plan-v1.md` (plan). Read-only audit; no spec/design edits.

First-audit abbreviations kept (`design:171` = design line 171, `spec:72` = spec line 72, `reaping:88` = failure-reaping line 88, `plan:63` = plan-v1 line 63). All citations are to the post-fix files unless the `e74cad1..74a84e5` diff is explicitly discussed.

---

## Summary

The `74a84e5` patch materially addresses the first audit's three blockers and most of its hardening items. The single-shared-branch topology (`spec:72-82`, `design:71-88`), the enumerated unattended allowlist + `HERDR_ENV=1` via `herdr workspace create --env` (`design:98-112`), and the launcher pane-parsing fix (`design:215-229`) are substantive, not cosmetic. The new machine-checkable gates table (`design:183-190`) closes the "judge on vibes" failure mode in intent, but three of its six command sketches are wrong or permissive enough to be theater in their current form — most critically gate 6's `gh api` endpoint does not exist as written. The remaining v1 risks (detached `--wait` feasibility, gate regex correctness, shared-worktree concurrency and cleanup ordering, deadline enforcement mechanics, resume collision handling) are real but bounded and empirically checkable in a day; none re-establishes the first audit's "not ready to build" verdict if the gate fixes below are applied before the first overnight run.

**One-sentence verdict on the first audit's "not ready": overturned, conditionally.** The design is now build-ready for a watchable manual first run; it is not yet ready for an unattended overnight run until the high gate-6 and medium gate-2/3 issues are corrected and the two empirical checks (detached `--wait`, actual thread-resolution API) pass.

---

## Verification of first audit fixes

Commit `74a84e5` (`docs(pipeline): fix audit gaps …`) touches only the two pipeline docs (365 insertions). Every recommendation was attempted; most landed.

| Gap | First-audit claim | Fix location(s) | Verdict | Evidence / caveat |
|-----|-------------------|-----------------|---------|-------------------|
| 1 HIGH — handoff contradicts per-worker worktrees (untracked `spec.md` invisible) | `spec:69-73` / `design:68-75` + `design:171-175` each created a fresh `--base main` worktree | `spec:72-82`, `design:71-88` + `design:247-257` | **Fixed** | Topology switched to one shared `herdr worktree create --base main --branch auto/pipeline-<run_id>` before stage 1; stage 1 must `git add && git commit` before settling; all later stages attach to the same path. Fresh *sessions* give independence, not fresh worktrees. Narrow residue: the attach mechanism is stated as "either `herdr tab create --workspace <shared_ws>` or `herdr worktree create --cwd <shared_worktree>` reuse" (`design:81`) — the second form is not a valid herdr invocation (see §3 G-6). |
| 2 HIGH — gates 2/3/5/6 not machine-checkable | `design:120-131` only covered stages 1/3/4; "posted *where*?" | `design:176-206` (gates table + retry policy) | **Partial — intent fixed, commands not yet correct** | Table pins syntactic checks for every stage, and the "existence before green" distinction for gate 3 is right. But gate 2's `rg` and gate 3's `rg -q "<name>"` have regex/false-positive issues, gate 5's `jq` shape is wrong, and gate 6's `gh api` endpoint does not exist as written (see §3 G-1..G-4). Status downgrades from blocker to "must fix before overnight" but is not yet closed. |
| 3 HIGH — unattended permission surface underspecified | Only `HERDR_ENV` + `external_directory` mentioned | `design:98-112` | **Fixed (substantive, with coverage caveat)** | Enumerates `herdr`, `git`, `gh` (incl. `api` + thread resolve), `uv`/`pytest`/`ruff`, `rg`/`jq`/`column`; external directories for socket/state/cache; `HERDR_ENV via --env` (citing `plan:63`); replication to Pi + blocked-prompt dry-run. Remaining caveat: opencode allowlist is pattern-based (`permissions` in `~/.config/opencode/opencode.json`) — the doc lists binaries but does not pin the actual JSON shapes, so a typo still wedges at 03:00; treat the enumeration as a checklist to transcribe, not a copy-pasteable config (see §3 G-5). |
| 4 HIGH (accepted-risk) — orchestrator runs `big-pickle`, quota death is silent | `design:3` / `spec:3-4` declared quota out-of-scope while putting longest role on the quota-exhausted model (`reaping:8-9`) | `design:136-145` | **Surfaced & partially mitigated — accepted risk, correctly** | Worker-side quota reaping via `herdr agent read --source visible | rg "Free usage exceeded"` on settle-timeout is now specced (mirrors `reaping:88`). Orchestrator self-death remains silent (no report) and is explicitly accepted with a morning checklist (`no report file ⇒ check systemctl + herdr agent list`) and a v2 provider failover pointer (`ROADMAP:126-129`). This matches the correct handling for an accepted risk; no further doc fix required. |
| 5 MEDIUM — no whole-pipeline wall-clock budget | Only per-worker 60-90 min timeouts; worst case >10 h | `design:130-135` | **Fixed (mechanical enforcement still underspecified)** | Hard deadline "launch + 7 h (tune to window; OQ 4)" with "finish current stage → partial `$PIPELINE_REPORT` → `herdr notification show --sound request`" closes the "no report at all" failure. "e.g." and "finish the current stage" are still vague about whether the orchestrator kills or waits out the in-flight worker (see §3 G-7). |
| 6 MEDIUM — hot `agent get` polling burns orchestrator context | `design:88-91` proposed 30-60 s polling (~50-80 calls/stage) vs blocking `--wait` | `design:113-120` | **Fixed (with new feasibility risk)** | Replaced by `nohup herdr agent prompt --wait --timeout … > <stage>.result.json 2>&1 &` + 3-5 min sleep polling of the result file, with `agent get` only near timeout. Saves ~10× cycles as intended. The detached form's viability inside opencode's bash tool is explicitly flagged as "verify at build time" (`design:118-119`, `spec:107`) — unproven until run (see §3 G-8). |
| 7 MEDIUM — resume asserted, not specified | `state.json` listed but no procedure; spawn/state-write crash ⇒ duplicate name collision | `spec:65-70`, `design:121-129` | **Partial — recipe added, collision handling thin** | `state.json` now includes `{current_stage, pr_number, artifact_paths, shared_worktree, branch, run_id}` and resume is "read `state.json` → `herdr agent list | rg "pl-"` reconcile → adopt or reap → reap-before-spawn for duplicate-name crash". The `rg "pl-"` on JSON and "adopt vs reap" semantics need tightening (see §3 G-9). |
| 8 MEDIUM — stage 6 session semantics ambiguous | "implementer-context session" undefined | `design:60,62`, `design:168-170` text + `spec:44` | **Fixed** | Pinned to "same agent as stages 3-4, prompted with review digest + `gh pr view --comments`" (preserves code context). Honours the "implementer-context" label. Minor tension with the `pl-<stage>-<run_id>` naming convention left implicit (see §3 G-11). |
| 9 MEDIUM — `retry_stage` vs stop-on-failure contradiction | `design:125` allowed `retry_stage` while `spec:57-59` said stop | `design:195-200` | **Fixed** | `retry_stage` now scoped to exactly two cases: `EmptyResponse` start-race (once) and infra-flavoured transient test failures (once); every gate-content failure is `abort` + report. Removes the 03:00 improvisation window. |
| 10 LOW/MEDIUM — blast radius, self-referential target | Pipeline can push to dogfood repo containing the scheduler | `design:235-241` | **Fixed** | One-paragraph blast-radius statement added (shares object store, can push per `ROADMAP:58-61`; stage 4 requires push; dogfood is `herdr-routines` itself; single-user threat model; "keep first feature trivial" bounding rule). Proportionate for POC. |
| 11 LOW — launcher sketches omit prerequisites | `herdr agent start` needs pre-existing pane; workspace-create pane-id parsing hidden; `-- pane` mix-up | `design:209-229` | **Fixed** | Both options now show `WS=$(herdr workspace create … --env HERDR_ENV=1 | jq -r '.result.root_pane.pane_id')` and correct `herdr agent start … --pane $WS` followed by separate `herdr agent prompt … --wait`. Systemd `Type=oneshot` + `TimeoutStartSec` not needed for one-shot transients is moot; `design:226-229` surfaces the valid two-option choice. |
| 12 LOW — no pane/workspace lifecycle or cleanup | 6+ worker panes leaked (cf. `reaping:1`) | `design:146-151` | **Fixed (with ordering tension vs new topology)** | Adds "after `$PIPELINE_REPORT` is written and artifacts committed/copied out, close worker panes/workspaces; keep `auto/pipeline-<run_id>` branch (aligns with `ROADMAP:77-79` manual GC)". Ordering is right for the old per-worker worktree model but needs a one-line correction for the new single-shared-worktree model (see §3 G-10). |
| 13 LOW — small undefineds | wait-for-comments timeout unspecified | `spec:50-52`, `design:65-69`, `design:92-94` | **Fixed** | Wait-for-comments pinned to **60 min** (both docs). Stage 5 fan-out explicitly "single worker session for v1; 5-reviewer fan-out is v2". Change notes pinned to `## Changelog v1→v2` inside `spec.md`. `state.json` schema expanded to include `shared_worktree`/`branch`/`run_id`. |
| 14 LOW — scheduler interaction | `tick`/cron correctly excluded; residuals benign | Unchanged (already correct) | **No fix needed — remains benign** | Namespaces `pl-` (pipeline) vs `rt-` (routines) remain disjoint; Pi contention (orchestrator+worker vs `tick` every 5 min) stays acceptable at this scale (`plan:318-323`). No doc change expected and none made. |

Summary of the table: 10 fully fixed, 3 partially fixed (gaps 2, 7, and the ordering nuance in 12), 1 correctly surfaced as accepted risk (gap 4). No gap was merely reworded.

---

## New / Remaining Gaps / Risks

Numbered G-1..G-12 for this report. Severity calibrated to "what breaks the first overnight run if not fixed."

### HIGH

**G-1 — Gate 6's primary check command is wrong: the `gh api` REST endpoint does not exist (`design:190`).**

`design:190` Option A gates on:

```
gh api repos/{owner}/{repo}/pulls/<n>/threads | jq -e '[.[] | select(.isResolved==false and .isBlocking==true)] | length==0'
```

GitHub's REST API has no `GET /repos/{owner}/{repo}/pulls/{number}/threads`. The checkable thread-resolution surfaces are (a) GraphQL `pullRequest.reviewThreads` (`isResolved`, `isOutdated`, reply edges) or (b) the REST review-comments endpoint `GET /repos/{owner}/{repo}/pulls/{number}/comments` plus a separate thread-state query — neither matches the sketched path or the `isBlocking` field. `isBlocking` is not a GitHub thread field at all; the code-review skill's "blocking vs non-blocking" is a convention rendered into the review *body*, not a thread property the API returns. As written the gate will exit non-zero for the wrong reason (404) or, if the CLI swallows the error, pass vacuously.

Impact: stage 6 can never gate correctly on Option A. The fallback Option B ("a reply exists on every blocking finding" via comments JSON) is checkable but the doc leaves the counting rule vague and the "chosen option must be written in orchestrator prompt" (`design:190`) means the 03:00 decision is still open.

Fix: before the first overnight run, run one real PR through the code-review skill, then empirically pin the gate: either a `gh api graphql -f query='… pullRequest(number:$n){reviewThreads … isResolved …}'` query plus a `rg "blocking"` filter on the thread body, or commit to Option B with an explicit `jq` count-match rule. Do not ship the current `gh api repos/…/threads` line.

**G-2 — Gate 2/3 `rg` patterns are brittle and will false-positive/false-negative (`design:186-187`).**

- Gate 2: `rg -c "^[0-9]+\\. .*Test:" "$WT/spec.md"` assumes every acceptance criterion is a single line of the form `1. … Test: <name>`. A multi-line criterion, a `## Acceptance criteria` section that uses `- [ ]` or `### AC-3` headings, or a criterion whose `Test:` sits on the next line will undercount and the `≥ N` check becomes meaningless (and `N` itself must be derived by the orchestrator LLM counting lines — the gate delegates the hard part back to the judge). `rg -q "blocking|non-blocking"` (`design:186`) is near-tautological: any file containing the word "non-blocking" matches "blocking" via the alternation, so the check passes the moment one of the two words appears anywhere. `rg -q "^## Changelog"` will also match a stray `## Changelog` outside `spec.md` if `$WT` expands wrong, but more importantly it does not verify the section *contains* a changelog — only that the heading exists.

- Gate 3: `rg -q "<name>" "$WT"` (`design:187`) interpolates the test name as a regex. A name like `test_handles_foo (unit)` or `test: pipeline/timeout` will either fail to match or match a comment that happens to contain a substring. The existence check should be fixed-string (`rg -F -q -- "<name>"`) and scoped to the test tree (`tests/` or `test_*`), not `$WT` (which includes `spec.md` itself — the spec trivially contains its own test names).

These are not nits: the gates were the flagship fix for the first audit's "silent judge failure" blocker, and the current regexes reintroduce it in a more specific form.

### MEDIUM

**G-3 — Gate 5's `jq` filter is syntactically plausible but semantically permissive and structurally fragile (`design:189`).**

```
gh pr view <n> --json comments | jq -e '.comments[].body | test("blocking|non-blocking") and test("confidence")'
```

- `jq -e` with `.comments[].body | test(…)` emits one boolean per comment; `jq -e`'s exit code reflects the *last* output value, so a PR with two comments where the first is well-formed and the second is not will gate on the second alone. The correct gate is `all(.comments[].body; test("blocking") …)` or a length/count check, not a per-element stream.
- `test("blocking|non-blocking")` again matches anything containing "blocking" as a substring; `test("confidence")` matches the word anywhere, including "no confidence in this check" or a quoted finding. The gate does not verify the skill's structured output (e.g. a `### Verdict: blocking` heading or `confidence: high/medium/low` field) — it verifies that two English words appear.
- `gh pr view --json comments` returns `comments` (issue comments) but the code-review skill, if it posts via the review API, may put findings in `reviews` rather than `comments`. The design should verify which field the skill actually writes to and gate on that field.

**G-4 — Gate 1's "non-empty, committed" check does not actually gate on non-empty (`design:185`).**

`test -f "$WT/spec.md" && wc -l "$WT/spec.md" && git -C "$WT" log --oneline -1 -- spec.md` — `wc -l` always exits 0 even for an empty file (it prints `0`), so the chain succeeds on a zero-line spec. A committed empty `spec.md` (e.g. `touch spec.md && git add && git commit`) will pass gate 1 and gate 2 will then fail, but the orchestrator will have wasted a stage 2 worker on an empty input. Add `test -s` or `[[ $(wc -l <"$WT/spec.md") -gt 2 ]]`.

**G-5 — Allowlist enumeration is now thorough but not transcribable as written (`design:103-112`).**

The list (`herdr`, `git`, `gh` + `api`, `uv`/`pytest`/`ruff`, `rg`/`jq`/`column`, plus `permission.external_directory` for `~/.config/herdr/herdr.sock`, `~/.local/state/herdr-routines/`, `~/.local/state/herdr/`, cache dir) plus `HERDR_ENV=1` via `herdr workspace create --env HERDR_ENV=1` (`design:100`, citing `plan:63`) is the right inventory. Three transcription risks remain:

1. `~/.config/opencode/opencode.json` permission syntax is not "one line per binary" — it is a JSON `permissions` / `allow` list of tool-call patterns (e.g. `bash:herdr *`, `bash:gh pr *` vs `bash:gh api *`). The doc's bullet list will need to be translated into the actual opencode pattern language; a naive one-entry-per-binary transcription will still block on `gh api` vs `gh pr view` distinctions.
2. The external-directory allowlist omits the pipeline's own checkout path (`~/.local/state/herdr-routines/repos/<target>` and the shared worktree under `~/.herdr/worktrees/…`) — the orchestrator's `herdr worktree create --cwd` and workers' `git -C "$WT"` both need directory access there if the opencode sandbox restricts it.
3. `gh` auth (`gh auth login` / `GH_TOKEN`) is not mentioned; `gh pr create` and `gh api` fail differently when unauthenticated, and that failure will surface at 03:00 as a gate-4/6 failure rather than a permission wedge.

None of these re-establishes a blocker, but the "replicate to the Pi and verify with a blocked-prompt dry-run" (`design:112`) should be a literal checklist item before the overnight run, not an aspiration.

**G-6 — Shared-worktree attach mechanism is underspecified and one of the two sketched forms is invalid (`design:81`).**

> Every worker attaches to that same path (either `herdr tab create --workspace <shared_ws>` or `herdr worktree create --cwd <shared_worktree>` reuse — no fresh `--base main` per stage).

- `herdr tab create --workspace <shared_ws>` creates a *new tab* in the given workspace; the tab's cwd is not pinned to the shared worktree path unless `--cwd` is also passed — so the worker may start in the parent clone instead of the pipeline branch.
- `herdr worktree create --cwd <shared_worktree>` is not a valid invocation: `herdr worktree create` expects `--cwd` to be an existing repo/worktree that contains the history to link from and `--branch` to name the new branch; pointing `--cwd` at an already-linked worktree to "reuse" it will error or create a second worktree with a duplicate branch.

The correct v1 primitive is almost certainly: workers reuse the shared worktree *path* directly — `herdr tab create --cwd "$WT" --workspace "$SHARED_WS"` or simply `herdr agent start … --pane "$WT_PANE"` where `$WT_PANE` is a pane whose cwd is `$WT` — not a second `worktree create`. The design should pin one form and show it in the orchestrator prompt template. Also note: stage 1's `spec.md` commit must be *pushed or at least not pruned* before stage 2 reads it; since the branch is never pushed until stage 4, stage 2 reading from the same local worktree path is fine, but any "fresh clone" interpretation of stage 2 would break again.

**G-7 — Deadline enforcement mechanics are still vague (`design:130-135`, `spec:65-70`).**

"Hard wall-clock budget, e.g. launch + 7 h" (`design:130`) — "e.g." means the orchestrator prompt does not yet have a pinned value; the build-time open question 4 in the spec (`spec:108`) is still open. "When exceeded, finish the current stage, write a partial `$PIPELINE_REPORT` and fire `herdr notification show --sound request`" leaves open: does the orchestrator `herdr agent prompt --timeout` kill the worker, `herdr agent get` + `pane_close`, or wait out the in-flight 90-min worker (which defeats the deadline)? The `systemd-run --timeout 25200000` in the launcher (`design:223`) is a herdr-level agent prompt timeout, not a pipeline-level deadline — the two need to be wired together (e.g. orchestrator tracks `date +%s` and compares to a `deadline_epoch` written to `state.json`).

**G-8 — Detached `nohup … --wait &` feasibility is load-bearing and still unproven (`design:115`, `spec:103-107`).**

The design correctly flags "verify at build time: confirm opencode bash can block / detach" (`spec:107`, `design:118-119`), but the v1 workflow now *depends* on that check passing: the 3-5 min sleep loop (`design:115-116`) assumes the orchestrator's bash tool can background a `nohup` job and that the result file appears asynchronously. If opencode's bash tool is synchronous (one command = one tool call, no `&` persistence across calls), the detached pattern silently degrades to a foreground block or a lost job. This is the single empirical gate for the entire polling hygiene fix; it should be the first thing run, before any other pipeline work.

**G-9 — Resume recipe is now sketched but collision/state semantics need tightening (`design:121-129`).**

- `herdr agent list | rg "pl-"` (`design:126`) pipes JSON through `rg`. `rg "pl-"` will match any occurrence of the substring `pl-` in the JSON (including `pane_id`, `workspace_id`, `label`), and without `-F`/`-x` will also match inside larger strings. The reliable reconcile is `herdr agent list | jq -r '.result.agents[] | select(.name | startswith("pl-")) | "\(.name) \(.agent_status)"'` or the `herdr.py:231-251` `agent_statuses()` helper filtered by `LIVE_AGENT_STATUSES`.
- "Adopt still-working worker or reap stragglers before re-spawning" — if the crash was between `herdr agent prompt` and `state.json` write, `state.json:current_stage` still points at the *previous* stage while the live `pl-<next>-<run_id>` agent is the orphan. "Adopt" then requires the orchestrator to infer the orphan's stage from its name, not from `state.json`. The doc says "handle duplicate-name collision via reap-before-spawn" but `herdr.py:34-40` says only `working` is live and only `idle`/`done` are settled/reapable — reaping a still-`working` orphan would require a `pane_close` outside the settled-agent path, which is not the standard flow and needs an explicit step.
- `state.json` is now the resume keystone but its write atomicity is not specified (write-to-temp + rename vs in-place overwrite); a crash mid-write leaves a corrupt resume file.

**G-10 — Cleanup ordering contradicts the new single-shared-worktree topology (`design:146-151`).**

> after `$PIPELINE_REPORT` is written and artifacts are committed/copied out, close worker panes/workspaces (`herdr workspace close` / `herdr worktree remove`); keep the PR branch

With the new topology there is one shared worktree+branch (`auto/pipeline-<run_id>`) and N worker *sessions* that attach to it (via tabs/panes), not N worktrees. "Close worker panes/workspaces" should be "close each worker's *tab/pane*" — closing the *workspace* that owns the shared worktree would destroy the worktree that holds the branch you intend to keep. The correct order is: (1) ensure all commits are on `auto/pipeline-<run_id>` and `$PIPELINE_REPORT` is mirrored to `~/.local/state/herdr-routines/reports/<run_id>.md`, (2) close each worker tab/pane, (3) leave the shared worktree + branch in place for manual GC per `ROADMAP:77-79`. The doc's use of `herdr worktree remove` is vestigial from the per-stage worktree era.

**G-11 — Stage 6 "same agent as 3-4" vs `pl-<stage>-<run_id>` naming is in tension (`design:60,173-174`).**

Workers are named `pl-<stage>-<run_id>` (`design:173-174`, `plan:71` cap `[a-z][a-z0-9_-]{0,31}` unique among live agents). If stage 6 reuses the stage 3-4 agent, the name `pl-3-<run_id>` persists into stage 6, but the gate table and state machine refer to `pl-6-<run_id>` as the stage-6 principal. The orchestrator prompt needs to specify whether stage 6 reuses `pl-3-<run_id>` via `herdr agent prompt pl-3-<run_id> …` (no new `agent start`) or starts a new `pl-6-<run_id>` agent seeded with `git diff main...HEAD` + `gh pr view --comments`. Either is viable, but the current text implies both ("same agent" and "each worker gets a unique name").

**G-12 — Wait-for-comments 60 min timeout has no polling rule (`spec:50-52`, `design:65-66,193`).**

The timeout value is now pinned (closes first-audit gap 13), but the orchestrator behaviour during the 60 min is not: poll `gh pr view --json comments` / `gh api …/reviews` every N minutes? Count only `blocking` findings? What if stage 5 posts the review 10 min after stage 4, but a human adds further comments 90 min later — does the orchestrator gate on review-post time, on "no new comments for 60 min", or on "no unresolved blocking threads"? The stage graph says stage 6 reads "review findings" (`spec:44`, `design:60`) — if wait-for-comments is really a human-comment wait, the gate that "waits 60 min for comments before stage 6" and the gate that "blocks until no unresolved blocking findings" are contradictory; if it is a review-post wait, it is just stage 5's settle timeout under another name.

### LOW (advisory)

**G-13 — Single shared branch: spec commits leak into the PR diff.**

Stage 1's `spec.md` commit(s) on `auto/pipeline-<run_id>` become part of the PR opened in stage 4 (`design:82-83`). The PR will contain `spec.md` alongside the implementation, which reviewers must ignore. Not a bug for dogfood, but worth a one-line note: either `.gitignore` `spec.md` after stage 1 and recreate it from state, or accept the leakage and prefix spec commits with `spec:` so they are trivially separable in review.

**G-14 — Interaction with `tick` / `history.jsonl` / systemd remains benign but deserves one explicit guard.**

Pipeline runs deliberately bypass `tick` (`ROADMAP:36`, `design:17-18`), write no `history.jsonl` lines, and use `systemd-run --on-calendar` transients, not the `herdr-routines.timer` (`plan:258-262`). The `pl-` vs `rt-` namespace split (`design:173` vs `config:59-60` `rt-<name>`) prevents agent-name collisions, and the 5-min `tick` cadence (`plan:261`) keeps contention low. The only remaining interaction is disk: the shared worktree under `~/.herdr/worktrees/…` is the same directory tree that `tick`'s `worktree` jobs link from (`config:55`, `herdr:124-143`); a pipeline branch named `auto/pipeline-<run_id>` will not collide with `auto/<job>-<ts>` (`schedule:etc.`), but a future `herdr-routines gc --dry-run` (`ROADMAP:28-29`) that matches `auto/*` could list pipeline branches unexpectedly. A one-line exclusion (`auto/pipeline-*` is not GC-eligible) is enough.

**G-15 — Spec commit `spec.md` is committed but not validated as being on the right branch.**

Gate 1 checks `git -C "$WT" log --oneline -1 -- spec.md` (`design:185`) — this checks that *some* commit in the current branch's history touched `spec.md`, not that the current `HEAD` is on `auto/pipeline-<run_id>` or that the latest commit is the spec commit. A stage 1 that commits to `main` by mistake (e.g. `herdr worktree create --base` mishap) would still pass gate 1 if `main` already has a `spec.md` from a prior run. Add `git -C "$WT" rev-parse --abbrev-ref HEAD | rg -q "^auto/pipeline-"` to gate 1.

---

## Improvements / Recommendations

Prioritised. "v1 before overnight" = blocks the first unattended run; "v1 next" = fix before promoting beyond manual watchable run.

### v1 — before the first overnight run

1. **Fix gate 6 empirically (G-1, HIGH).** Run one real PR through the code-review skill, capture the actual JSON shapes for comments vs reviews, and pin the gate: either a `gh api graphql` reviewThreads query (filter `isResolved==false` + body contains `blocking`) or commit to Option B with an explicit `jq` count-match on `gh pr view --json comments` (or `reviews`). Delete the current `gh api repos/…/threads` line — it will 404 and mislead the orchestrator. Owner: whoever writes `orchestrator-prompt.md`.

2. **Fix gate 2/3/5/1 regexes (G-2..G-4, MEDIUM).** Gate 2: replace `rg -c "^[0-9]+\\. .*Test:"` with a spec-pinned exact heading (`rg -c "^[0-9]+\\. .*Test:"` may be okay if the spec template is pinned, but add `rg -F` for the test-name extraction and scope to the spec file alone; make the orchestrator derive `N` by counting `Test:` lines itself rather than trusting the LLM). Gate 3: `rg -F -q -- "<name>" tests/` (fixed-string, scoped to test tree, not `$WT`). Gate 5: wrap in `all(…)` and gate on the field the skill actually writes (`comments` vs `reviews`); tighten `test("confidence")` to `test("confidence:\\s*(high|medium|low)")`. Gate 1: add `test -s` / `wc -l` count check and branch-name assertion (`git rev-parse --abbrev-ref HEAD`).

3. **Transcribe the allowlist into real `opencode.json` patterns and dry-run it (G-5).** Translate the bullet list (`design:103-112`) into the actual `permissions` JSON that opencode enforces; include `gh api` vs `gh pr` as separate patterns if the schema distinguishes them; add `herdr notification show` for the deadline path; add the shared worktree and report paths to `permission.external_directory`. Run one manual pipeline stage on the Pi under the real allowlist and confirm no `blocked` wedge.

4. **Pin the deadline value and the enforcement primitive (G-7).** Replace "e.g. launch + 7 h" with a pinned default (e.g. 7 h) written to `state.json:deadline_epoch` at orchestrator start; orchestrator checks `date +%s` vs `deadline_epoch` between stages; "finish current stage" means "wait for `herdr agent prompt --wait` to return, then skip remaining stages and write partial report" — do not kill the in-flight worker mid-implementation.

5. **Empirically gate the detached `--wait` path (G-8).** Before any other pipeline work, run a 2-min experiment on both laptop and Pi opencode: `nohup herdr agent prompt <w> --wait --timeout 120000 > /tmp/result.json 2>&1 &` + `sleep` loop checking for the file. If `&` does not persist across bash tool calls, the design falls back to plain blocking `herdr agent prompt --wait` (which is acceptable for v1; the context saving is smaller but the pipeline still functions).

6. **Pin the shared-worktree attach primitive and fix cleanup ordering (G-6, G-10).** Choose one: workers attach via `herdr tab create --cwd "$WT" --workspace "$SHARED_WS"` (tab reuse), not a second `worktree create`. Cleanup order: (1) mirror `$PIPELINE_REPORT` + ensure commits on `auto/pipeline-<run_id>`, (2) close each worker *tab/pane*, (3) leave the shared worktree+branch for manual GC. Remove `herdr worktree remove` from the pipeline cleanup path.

### v1 next — before promoting beyond POC

7. **Tighten resume (G-9).** Replace `herdr agent list | rg "pl-"` with `jq -r '.result.agents[] | select(.name | startswith("pl-")) | "\(.name) \(.agent_status)"'`; derive the orphan stage from the agent name suffix when `state.json` lags the spawn; specify atomic `state.json` writes (write to temp + rename); specify that reaping a `working` orphan uses `pane_close`, not the `settled_agent_pane` path.

8. **Resolve stage-6 naming (G-11).** In `orchestrator-prompt.md` state whether stage 6 reuses `pl-3-<run_id>` via `herdr agent prompt` (no new start, keeps code context, burns context) or starts a seeded `pl-6-<run_id>`. Recommend the former for dogfood (simpler, preserves context) with a note to switch to fresh-seeded if context burn becomes an issue.

9. **Pin wait-for-comments polling (G-12).** In the orchestrator prompt, state: poll `gh pr view --json comments` (or `reviews`) every 5 min for 60 min after stage 5 settles; gate stage 6 on the review's `blocking` findings (stage 5 output), not on arbitrary human comments that may arrive later; after 60 min with no review, abort with partial report.

10. **Exclude pipeline branches from future GC (G-14).** When `herdr-routines gc` lands, exclude `auto/pipeline-*` from `--dry-run` listing (or document that it will list them and that deletion is manual). One line in the GC filter.

11. **Decide on spec leakage (G-13).** Document whether `spec.md` intentionally rides the PR (prefix spec commits `spec:`) or is stripped before PR creation. Either is fine; leaving it unspecified guarantees reviewer confusion.

### v2 / deliberately deferred (agree with the design's deferrals)

- `scripts/verify-gate.sh` deterministic oracle (`design:202-207` Option B) — revisit only if orchestrator mis-judges gates despite the fixes above; the in-prompt `rg`/`gh`/`jq` checks should be sufficient for v1.
- Provider/model failover on `quota_exhausted` (`ROADMAP:126-129`) remains correctly deferred until reaping phase 1 proves the dead-wait matters again (`reaping:192-195`).
- `herdr-routines run orchestrator` job-wrapper ergonomic improvement (`design:232-233`) and `workflows/pipeline.yaml` promotion (`design:47-50`) only after a few successful runs.
- Full 5-reviewer fan-out for stage 5 (`design:67-69`) and paid-tier quota handling (`spec:3-4`).

---

## Open questions still remaining

Mapped against `spec:93-119` open questions plus new ones surfaced by the fixes.

| # | Question | Status after `74a84e5` | What still needs to be closed |
|---|----------|------------------------|-------------------------------|
| OQ-1 | Which repo hosts the POC feature? | **Resolved.** Dogfood `herdr-routines` first, `fitted` as stress test (`spec:95-97`, `design:243-245`). | None for v1; revisit only if dogfood run surfaces blast-radius issues (see §3 G-14). |
| OQ-2 | Orchestrator→worker driving: port runner.py failure handling | **Resolved in doc, empirical guard remains.** Patterns (EmptyResponse retry, quota disambiguation, per-worker timeout, settle mapping, `HERDR_ENV` via `--env`, allowlist, deadline, quota scan, detached `--wait`) are now pinned (`spec:98-107`, `design:77-151`). | Empirically confirm at build time: (a) opencode bash can background `nohup --wait` (G-8), (b) `gh api` thread-resolution endpoint shape (G-1). |
| OQ-3 | Herdr session vs bare opencode? | **Resolved.** Lean `herdr` session for watchability (`spec:108-109`, `design:94-97`). | None; fallback to bare opencode if herdr-session creation fails is already noted. |
| OQ-4 | Model assignments per stage | **Resolved.** `1 claude / 2 opencode/big-pickle / 3 opencode / 5 opencode/code-review` (`spec:110-113`, `design:246-248`). | None for v1; stage 5 may switch to `claude` if review quality needs it (`design:248`). |
| OQ-5 | Pipeline launcher | **Resolved.** One-shot `systemd-run` transient (pinned-date timer or manual one-liner), `herdr workspace create` + pane-id parsing corrected (`spec:114-119`, `design:209-233`); `herdr-routines run` wrapper deferred. | Pin the calendar date per run; decide `Persistent=` if the Pi may be off at 02:00. |
| OQ-6 (new) | Spec-template vs gate-regex coupling | **Open.** Gate 2 assumes a fixed `## Acceptance criteria` + `1. … Test: <name>` template, but the stage 1/2 prompts that enforce that template are not part of this audit's scope (they live in `orchestrator-prompt.md`, not yet written). | When writing `orchestrator-prompt.md`, pin the spec v2 template verbatim and make the gate regexes match that template exactly; otherwise G-2 reopens. |
| OQ-7 (new) | Wait-for-comments semantics | **Open.** 60 min value pinned (`spec:50-52`, `design:66`) but polling behaviour and what is being waited on (review post vs human comments) remain unspecified. | Pin in orchestrator prompt: what is polled, at what cadence, and what triggers stage 6 vs abort (G-12). |
| OQ-8 (new) | Pipeline vs worktree GC interaction | **Open.** `auto/pipeline-*` will appear in a future `auto/*` GC listing. | Add exclusion when GC lands (G-14). Low risk for POC. |

First-audit open questions 2↔OQ-2, 3↔OQ-3, 4 (deadline) ↔ now `design:130-135` + OQ-2(b), 5↔OQ-5, 6↔deferred to v2. No orphaned open question remains from the first audit's list except where noted as new OQ-6..8.

---

## Verdict

**Build-ready for a watchable manual first run (with two pre-flight fixes); not yet ready for an unattended overnight run as written.**

What blocks the *overnight* qualifier is narrow and fixable in place, not architectural:

1. **Must fix before overnight:** gate 6's `gh api` endpoint (G-1, HIGH) — the current line will 404 and the stage-6 gate can never pass as written. Replace it with an empirically verified GraphQL or comment-count rule and commit to one option in the orchestrator prompt. Without this, the pipeline either hangs in the address-comments loop or gates on vibes.
2. **Must fix before overnight:** gate 2/3/5/1 regex correctness (G-2..G-4, MEDIUM) — the gates that were the flagship fix for the first audit's blocker will false-positive on trivial substring matches and mis-handle test names with regex metachars. Apply the fixed-string and `all()` corrections above.
3. **Must verify before overnight:** detached `--wait` feasibility (G-8) and the actual review-thread API shape (G-1) — two 10-min empirical checks on the Pi opencode that decide whether the doc's commands execute at 03:00.
4. **Should fix before overnight (low-risk, 30 min):** allowlist transcription into real `opencode.json` patterns + one blocked-prompt dry-run on the Pi (G-5); pin deadline enforcement and shared-worktree attach/cleanup primitives (G-6, G-7, G-10).

If those four items are addressed, the remaining findings (G-11..G-15, OQ-6..OQ-8) are polish or v2 deferrals — the POC is worth running, and the single-shared-branch topology plus the failure-reaping port remain the strong parts of the plan. If they are not addressed, the second audit's HIGH (G-1) alone is sufficient to keep the pipeline from completing an overnight loop, and the MEDIUM gates (G-2..G-4) reintroduce the first audit's "silent judge failure" in a more specific, harder-to-diagnose form.

No new HIGH architectural contradiction was introduced by the fixes; the over-engineering risk is low (the design correctly keeps `verify-gate.sh` and `pipeline.py` as v2 opt-ins, per `design:202-207`).

---

*Reviewer: muse. Source files read: `docs/feature-pipeline-orchestrator-spec.md` (126 lines), `docs/pipeline-poc-design.md` (287 lines), `docs/pipeline-audit-xpreview.md` (208 lines), `docs/failure-reaping.md` (207 lines), `ROADMAP.md` (133 lines), `docs/plan-v1.md` (656 lines), plus `src/herdr_routines/herdr.py` (403 lines), `src/herdr_routines/config.py` (317 lines), and `e74cad1..74a84e5` diff (365 insertions). Plan: `docs/pipeline-audit-muse.md:1`.*
