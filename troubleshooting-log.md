# Troubleshooting Log

Real incidents, root causes, and fixes for `herdr-routines` itself — scheduled-job/pipeline bugs,
not Pi infrastructure. Infra-level incidents (memory exhaustion, OOM, network) live in
[`../raspberrypi/troubleshooting-log.md`](../raspberrypi/troubleshooting-log.md). Newest at the
bottom.

---

## 2026-08-24 — `spec.md` path collision causes a merge conflict (and one silent overwrite)

**Symptom.** The overnight pipeline orchestrator's third dogfood run (`20260825T000735Z`, PR #29)
came back `mergeable: CONFLICTING` against `main` — not a content disagreement, a straight
merge-conflict on `spec.md`.

**Diagnosis.** Every pipeline run's stage 1/2 wrote its spec to the same repo-root path,
`spec.md` — a single shared file every run rewrote in full. PR #28 had already merged its own
full-file rewrite of `spec.md`; PR #29's branch had a different full-file rewrite of the same
path from an unrelated feature. Two branches independently overwriting one shared path is a
structural collision, not a fluke — it will recur any time two runs' branch histories overlap.
Checking history (`git log --follow --all -- spec.md`, `gh pr view` on every pipeline PR) also
turned up that PR #27's spec (`--version`/`-V` flag) had already been **silently overwritten** at
`main`'s tip by PR #28's merge, with no conflict raised — full-file rewrites of a shared path
don't always even surface as conflicts.

**Root cause.** No per-run namespacing on the handoff artifact path. `docs/pipeline/design.md`
and `docs/pipeline/orchestrator-prompt.md` both hardcoded `$WT/spec.md` for every stage.

**Fix.** Moved to a per-run path: `docs/pipeline/runs/<run_id>/spec.md` (`design.md` G-15).
Backfilled the full historical record — recovered PR #27's overwritten spec from its merge
commit, and PR #28's spec via `git mv`, into the new convention. Landed as PR #30, including the
backfill.

**Lesson.** A file every run rewrites in full at a fixed shared path is a collision waiting to
happen, whether or not git happens to raise a conflict on any given pair of runs — namespace by
run id from the start, not after the second collision.

---

## 2026-08-25 — git's rename-detection silently misplaces content during a real merge

**Symptom.** While manually resolving PR #29's conflict (above) by merging `main` (which now had
the G-15 per-run-path fix) into the PR's branch, the merge completed **cleanly** — no conflict
markers, no manual-resolution prompt. But the resulting file at
`docs/pipeline/runs/20260823T234906Z/spec.md` (PR #27's `--version`/`-V` flag run) turned out to
contain PR #29's plugin-manifest spec content instead.

**Diagnosis.** Git's `ort` merge strategy resolves a delete/modify conflict (root `spec.md`
deleted on `main`'s side, modified on the branch's side) via content-similarity rename detection
when there's a plausible target. With multiple newly-added `docs/pipeline/runs/*/spec.md` files
on `main`'s side to choose from, it picked the wrong one by similarity score — not by which run
the branch's own history actually belonged to. The merge diff even showed `rename spec.md =>
docs/pipeline/runs/20260823T234906Z/spec.md (100%)`, i.e. git was confident, and wrong.

**Root cause.** Git's rename detection has no concept of "which run this branch is," only
content similarity between deleted and added blobs across the two merge parents. A clean merge
is not proof the result is correct when multiple similarly-shaped files exist on one side.

**Fix.** Caught by diffing the resulting file content against what the branch's own stage 1/2
commits actually wrote (not by trusting the clean merge). Restored the correct backfilled
`--version`/`-V` spec at the `20260823T234906Z` path, moved the plugin-manifest spec to its
correct `20260825T000735Z` path, reran the test suite, and pushed as a follow-up commit on PR #29
before merging.

**Lesson.** Any future branch that predates a shared-path-migration commit landing on `main`
needs its resulting per-run paths manually verified after merging `main` in — a clean merge
(no conflict markers) is not sufficient evidence the content landed in the right place when the
migration itself was a rename-shaped change.

---

## 2026-08-25 — pipeline gates check content shape, not who produced it

**Symptom.** On the pane-lifecycle-v2 dogfood run (`20260825T021919Z`, PR #31), the orchestrator
(`opencode/muse-spark-1.2-contributor-free`, free tier) never spawned separate `pl-1`/`pl-2`
workers for stages 1 and 2 — it wrote `spec.md` v1 and v2 itself, in its own single session.
Every gate passed anyway; nothing in the run's own report or gate output flagged this as unusual.

**Diagnosis.** Confirmed via `herdr agent list` during the run: no `pl-1-20260825T021919Z` or
`pl-2-20260825T021919Z` agent ever existed, only the orchestrator's own agent. Re-reading
`orchestrator-prompt.md`'s stage 2 harness note — "fresh session ... independence via sessions
not model family" — made the actual defect clear: stage 2 exists specifically to be an
*independent* review of stage 1's spec. The same session authoring both stages isn't independent
review, even though the resulting file has the right headings.

**Root cause.** Every gate in `design.md`'s gate table (`rg`/`jq` checks against `spec.md`,
PR state, review bodies) checks the shape of the *output* — never which *process* produced it.
Nothing enforced that the "Worker spawn template (use for every stage)" instruction was actually
followed; a model choosing to shortcut it, even with no prompt change involved, went completely
uncaught.

**Fix.** Added G-17: a process-fidelity gate. Any stage a workflow declares independent must now
be gate-verified by agent name + session id (`state.json`'s new `stage_sessions` map), not merely
trusted from the prompt — concretely, gates 1i/2i check that a distinct `pl-N-<run_id>` agent
exists and (for stage 2) that its session id differs from stage 1's recorded session. Framed
generally on purpose: the fix is "any stage marked independent needs a process-identity check,"
not a `pl-1`/`pl-2`-specific patch, so it survives the later move to a declarative per-stage
`isolation:` field instead of being re-derived per hardcoded stage pair. Landed in PR #33.

**Lesson.** An LLM orchestrator can silently deviate from an explicit "spawn a separate worker"
instruction with no prompt change and no error — plain model non-determinism against an
unenforced instruction is enough. Content-shape gates alone cannot catch this class of bug;
verifying *how* something was produced needs its own explicit check.
