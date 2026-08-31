---
id: "028"
title: "pick-feature: skip issues with an open pipeline PR"
status: open
priority: medium
area: pipeline
---

## Description

`herdr-routines pick-feature` (issue 013) is a pure-filesystem selector: it picks
the lowest-id `status: open` issue and cannot see GitHub. So when a run is still
in flight and a **later launch** fires its own pick moment, the new run re-picks
the same issue the in-flight run already has an open PR for. Both runs then
mutate the same issue file (`--mark-in-progress` flips it in the parent clone),
two orchestrators write overlapping `state.json`/reports, and the work is
duplicated.

Hit on 2026-08-31: run `20260831T012350Z` was resumed (stage 6) when the same
night's 02:00 auto-run would have re-picked its feature (**006**, still `open`
on main because the PR was unmerged). Avoided manually by keeping
`pipeline-nightly.timer` stopped — a structural guarantee it is not.

## Design (proposal)

`pick-feature --skip-open-prs`:

- Lists open PRs via `gh pr list --repo <owner>/<repo> --state open --json
  number,headRefName,body`, deriving `owner/repo` from the cwd's
  `git remote get-url origin` (pick-feature always runs in `$REPO_PARENT`).
- Filters to PRs whose `headRefName` starts with `auto/pipeline-`.
- Extracts claimed issue ids from the pipeline PR body convention
  ("Closes issue NN — the `status: done` flip rides this PR", stage-4 template).
- Excludes those ids from selection without touching the issue files.
- **Fail-open**: if git-remote resolution or `gh` errors, warn to stderr and
  pick anyway — a transient gh outage must never brick the nightly run.
  (Issue 027 proved the failure-investigation budget is already tight.)

Files: `src/herdr_routines/pick_feature.py`, `src/herdr_routines/cli.py` (flag
wiring), `tests/test_pick_feature.py`, `docs/pipeline/orchestrator-prompt.md`
(the self-select invocation gains `--skip-open-prs`).

## Acceptance

- With an open `auto/pipeline-*` PR whose body closes issue N, `pick-feature`
  returns the next eligible issue, not N.
- Non-pipeline open PRs (head not under `auto/pipeline-*`) never exclude an issue.
- Missing `git remote` or `gh` failure → warning + normal pick (exit 0).
- The orchestrator-prompt self-select uses the flag: a resumed run's feature is
  never re-picked by a later launch.

## Log

- **2026-08-31**: filed from run `20260831T012350Z` collision analysis (resume
  vs. the same night's 02:00 auto-run). Design proposed in-session: pregnant
  `--skip-open-prs`, cheap, fail-open.
