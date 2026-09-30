---
id: "055"
title: "Review `@me` PRs across repos: a cross-repo `kind: review` job that posts one review per open PR"
status: open
priority: medium
area: pipeline
---

## Description

`babysit-prs` (issue 015) watches one repo and only PRs a routine opened itself:
`_process_pr_target` calls `list_open_prs(..., branch_prefix="auto/")` and derives a
single `owner/repo` from the job's own clone, so the branch prefix — not the author
— is what scopes it. The PRs a human writes by hand, on their own laptop, in any
repo, get no review at all; neither do PRs where the human is the requested
reviewer. That gap is the Parking Lot bullet this issue promotes.

The bullet's stated prerequisite has shipped: the jobs refactor (issue 006) made
`jobs.d/<name>.yaml` a per-file edit, and 049's `kind:` made the dispatch axis
exhaustive, so a new job is one file plus one new `kind` branch rather than a new
trigger mode bolted onto `auto_fix`.

### The one hard problem: a review has no self-clearing eligibility signal

Every existing job is cheap because its *gate* is re-derived from live state each
tick: `pr_health` asks "is CI red or a thread unresolved?", and once the fix lands the
PR stops being eligible. A review's trigger is "this open PR is unreviewed" — and
after the review posts, the PR is **still open and still unreviewed by any signal the
job can cheaply re-derive**. Ship this naively and the same PR gets reviewed again on
the next tick, forever, each pass commenting on a PR it already commented on.

So this issue is mostly about the two guards that make "review unreviewed PRs" a
finite loop, and about cross-repo mechanics:

1. **A content-based guard.** The review agent ends its posted review with a stable
   HTML marker naming both its job and its target, `<!-- herdr-routines:review
   job=<job> key=owner/repo#N -->`. Before dispatching, the engine reads the PR's
   comments/reviews and skips any candidate already carrying *this job's* marker for
   *that* target key. The job name is in the marker for the same reason the attempt
   budget is keyed per job: two review jobs over one account must not treat each
   other's reviews as their own. It is the same shape as
   `refinement_open_pr_slugs`'s "open PR already covers this bullet" guard, and it
   composes with the agent's own idempotence check.
2. **An attempt budget keyed on the full target key.** `max_attempts_per_target`
   (default **1** for this kind) counted per `owner/repo#N`, so a review that never
   landed is a real miss that surfaces in the report instead of a nightly retry
   storm, and `#7` in repo A is independent of `#7` in repo B.

The other structural difference from `babysit` is that **the target's repo is not known
until it is discovered**: each dispatched PR needs its own local checkout, at its own
default branch, in a directory derived from the repo identity so two jobs (or two
targets) share one clone instead of one clone per PR.

## Design

Ship as two PRs, in this order. Phase A is entirely deterministic and dispatches no
agent, so the first real run is a readable list of what the job *would* touch;
Phase B adds the agent and the read-only worktree lifecycle.

### Config

```yaml
- name: review-my-prs
  enabled: false # ships off: it posts to GitHub on the operator's behalf
  cron: "30 8 * * *" # 08:30, after the 02:00 pipeline's PRs have settled
  kind: review
  select: [author] # author | review_requested
  include_auto_branches: false
  max_workers_per_tick: 2
  max_attempts_per_target: 1
  agent_kind: opencode
  model: null
  prompt: "" # empty = engine-injected review prompt
  timeout_ms: 1800000
  start_timeout_ms: 120000
```

`kind: review` is the fourth value of the `kind` literal and the third dispatch
branch in `_process_job` (after `pipeline` and `gated`). Config validation, all
rejected at load time with the same message shape as the existing `kind: pipeline`
rules:

- `repo` / `repository` are **not applicable** — the job has no single checkout.
  `Job.repo` is set to the managed-clone base dir (`repos.default_repos_dir()`) and
  per-repo checkouts are derived under it. `ensure_repo` is never called with a
  `kind: review` job; that is the invariant that makes the overload safe.
- `checks` is not applicable (there is no gate to configure; verification is the
  post-condition below), and `target` must be absent or `pr`.
- `select` is **required** and must be a non-empty, duplicate-free list drawn from
  `{author, review_requested}` — the bullet's "authored by me (or
  `--review-requested`)". `author` is the default-worthy one; `review_requested` is
  opt-in because it puts the routine in someone else's PR queue.
- `include_auto_branches` is an optional bool, default `false`.
- `max_attempts_per_target` defaults to **1** for this kind (a review that landed is
  never redone; a review that failed to land is a miss to report, not to retry).

### Discovery — one batched search

A new `GhClient.search_prs(modes, limit)` method issuing **one**
`gh api graphql` call whose query text is assembled with one alias per selected mode
(the existing `RealGhClient.graphql` already takes variables, so both modes ride a
single round-trip):

```graphql
query($authorQuery: String!, $requestedQuery: String!) {
  author: search(query: $authorQuery, type: ISSUE, first: 100) { nodes { ...on PullRequest { …fields } } }
  requested: search(query: $requestedQuery, type: ISSUE, first: 100) { nodes { …on PullRequest { …fields } } }
}
```

An unselected mode contributes no alias and no variable. Qualifiers:

- `author` → `is:pr is:open archived:false draft:false author:@me`
- `review_requested` → `is:pr is:open archived:false draft:false review-requested:@me`

`first: 100` is GitHub's own per-alias ceiling, and this design does **not** paginate
in phase A: a search returning exactly 100 nodes for a mode is recorded as
`search_truncated` in `extra`, and the oldest-first ordering means the next tick
works the next slice rather than re-serving the same head of the list. A mode at the
ceiling is a real limit to fix later, not something to silently ignore — a human
with 200+ open PRs is exactly the case that would make it matter, so phase A's
report has to name it.

Per-node fields: `number, title, url, createdAt, headRefName, author{login},
repository{nameWithOwner}, headRepository{nameWithOwner}`. Any `gh` failure —
including an auth error or a secondary rate limit — returns `None` and the tick
records `failed (search_failed)`: fail closed, the same posture as
`refinement_open_pr_slugs`, because an unknown answer must never mean "nothing to
do".

Skip rules, each recorded with its own `extra["skip_reason"]`:

| Reason | Rule |
|---|---|
| `author_mismatch` | `author.login != gh api user --jq .login` — search can return a stale login |
| `routine_owned_branch` | `headRefName.startswith("auto/")` and not `include_auto_branches` |
| `fork_pr` | `headRepository != repository` |

`routine_owned_branch` is the double-review guard, not a coincidence: pipeline PRs
already get a review at orchestrator stage 5, and `babysit-prs` owns the `auto/*`
slice. The opt-in exists for someone who wants the overlap on purpose, at the cost of
reviewing every pipeline PR twice.

`fork_pr` is a real limitation, not an oversight. A fork's head branch lives in
another repository, so the head ref has to be fetched from a second clone (or
`pull/<n>/head`) before any worktree can exist — a different mechanic with its own
failure modes. The headline use case is one human's own repos; fork support is a
named follow-up in Non-goals.

Candidates are ordered **oldest first** (`createdAt` ascending, tie-broken by
`updatedAt`, then by `owner/repo#N` so the order is total). `babysit` sorts by PR
number, which is meaningless as a cross-repo order; oldest-first also guarantees the
`max_workers_per_tick` cap starves the *newest* PRs, never the backlog.

### Reading the marker — one call, both surfaces

Both the pre-dispatch guard and the post-condition need the same read, so it is one
method with one defined shape: a new `GhClient.pr_review_bodies(owner, repo,
number)` on top of `gh pr view <n> --repo owner/repo --json comments,reviews`, whose
result is **both** surfaces concatenated — the posted review bodies (the
`code-review` skill posts a review, so this is the primary one) *and* the issue
comments (catches an agent that answered with a top-level comment instead). One call
rather than two keeps the read atomic: a "reviews succeeded, comments failed" split
has no useful fail-closed answer. It inherits `gh`'s 100-item page, so a marker
buried under 100 later comments is invisible to the guard — bounded by the attempt
budget (one extra review at worst, never a loop), and stated here so nobody
mistakes the guard for exhaustive.

A failure of this call is `None`, never `[]`, and every caller treats `None` as
*unverified*.

### Per-repo checkout

For each distinct repo that still has a candidate after the cap, one
`gh api repos/{owner}/{repo}` call yields `clone_url` + `default_branch`, cached per
tick. Refactor `repos.ensure_repo(job)` into `repos._ensure_checkout(clone_url,
dest, base)` (plus the `_clone` / `_fetch_and_fast_forward` calls it already makes),
with `ensure_repo` delegating so `babysit`'s `repo:`/`repository:` behavior is
unchanged — criterion 18 pins that. The checkout lands at
`<base>/<owner>/<repo>`, keyed by repo identity so a second job or a second PR in the
same repo reuses one clone. `base` is the repo's own `default_branch`, never
`job.base`.

Then, per dispatched PR: `git fetch origin <headRefName>`, and create the worktree
from `origin/<headRefName>` (creating the local branch tracking it when absent).
A clone/sync failure fails only that repo's targets (`clone_failed` /
`repo_sync_failed` in `extra`); every other repo still dispatches.

### Worktree lifecycle — read-only, and never stealing one

- Own worktree: `<checkout>/.worktrees/review-pr<N>`. Reuse it when it already
  exists and passes the existing `_worktree_reuse_check` (issue 036's rule); otherwise
  `worktree remove --force` the stale path and re-add it, so a second attempt never
  hits git's "already used by worktree".
- **Any other** worktree holding the head branch → `skipped
  (head_branch_checked_out)`, no worktree created. Issue 036 showed that fighting for
  another owner's branch is the failure mode; reviewing a branch that the pipeline
  orchestrator is still writing to is a review of a moving target anyway.
- After the worker settles — success *or* failure — remove the worktree with
  `--force`. A review needs no retained branch (only the fixer path does), and a
  leftover worktree is both a collision waiting to happen and litter for `gc`.

### Agent identity

`rt-<job>-rv<h6>` where `h6 = sha1(f"{job_name}|{target_key}")[:6]`, plus a run tail.
A bare PR number is ambiguous across repos, so the discriminator is a hash of the
*job and the full target key*: it is unique per (job, target), fixed-length, and the
same token names the per-PR report file and the history record.

Mirroring `auto_fix.build_worker_agent_name`, the `rv<h6>` discriminator is never
truncated and the run tail is appended, hashed when it doesn't fit. That fixes the
budget arithmetic: the prefix is `3 + j + 3 + 6 = j + 12` chars for a `j`-char
job-name portion, so the tail gets `32 - (j + 12) - 1 = 19 - j` chars. Cap `j` at
**13** and the tail always has ≥ 6, which is what keeps a long job name from
degenerating into a bare prefix that every run of that target would reuse (and
`agent_name_taken`, issue 051, would refuse). The live agent check matches **by
prefix** — `build_review_agent_prefix(job, target_key)` is the single function both
the builder and the liveness guard use, so two ticks 5 minutes apart can never
double-dispatch one PR (issue 036c's lesson). Because the run tail is in the name, a
blocked worker from a previous night cannot wedge this job with `agent_name_taken`
(issue 051's failure mode); correlate agents to runs through history `extra`, not
through the opaque name.

### Prompt

`prompt: ""` injects `auto_fix.build_review_prompt(...)`'s sibling, mirroring
`build_fix_prompt` (and honoring the same `job.prompt or build_…` override). It
directs the agent to run the `code-review` skill against the PR URL in
single-session mode — stage 5's measured configuration (single primary reviewer; the
fan-out is v2) — to post exactly one review with `blocking` / `non-blocking` tier
labels, and to:

- **re-read the PR before posting and post nothing** if this job's marker for this
  target is already present (the in-agent half of guard 1; the engine re-queries
  regardless);
- **never edit, commit, push, resolve threads, or fix anything** — this job reviews,
  and the worktree disappears after it finishes;
- write `$ROUTINE_REPORT` with a `## Outcome: ok|skipped|failed` line and the PR URL.

### Post-condition verification — the gate analogue

`pr_health` has a live gate; a review needs an *after*-condition. After the worker
settles, the engine re-queries the PR's comments/reviews for the marker:

- marker present → `done`, `extra["verified"] = True`, `gate: "passed"`;
- marker absent → `failed`, `reason: "review_not_posted"` — the worker's settle
  status and its self-written report are **not** evidence;
- the verification query itself failed → `failed (review_verification_failed)`.

The last case is the important one: an unverifiable answer is never `done`, the same
way `auto_fix.commit_check_runs` refuses to be read as green.

### History and report

Per-target `extra`: `target_key` (`owner/repo#N`), `repo`, `pr_number`,
`headRefName`, `select_mode`, `attempt`, `agent`, `pane_id`, `report_path`,
`verified`, `target: "pr"`, plus `reason`/`error`/`skip_reason` where they apply.
Terminal record carries `gate: passed|failed` and the counts `enumerated`,
`candidates`, `dispatched`, `skipped`, `repos_failed`. The attempt counter is
`attempt_count_for_target(history_path, job, target_key)` — a sibling of
`attempt_count_for_pr` over the same `_COUNTABLE_STATES` rule (a `skipped` record
must not consume budget), keyed on `extra["target_key"]`.

`reports/review-<run_id>.md` is the aggregate (the shape
`_process_pr_target` already writes: counts, then one line per candidate with its
disposition), plus one per-PR report at
`reports/auto-review-<run_id>-<h6>.md`. **Every** candidate appears in the aggregate,
including the skipped ones and why — that list is the audit trail that stands in for
a dry-run subcommand in this phase.

### Files touched

`src/herdr_routines/config.py`, `src/herdr_routines/tick.py`,
`src/herdr_routines/repos.py`, `src/herdr_routines/auto_fix.py` (new prompt/name
helpers), **new** `src/herdr_routines/review_scan.py` (pure selection: search →
candidates → skip reasons → ordering), **new** `tests/test_review_scan.py`, **new**
`deploy/jobs.d/review-my-prs.yaml`, `ROADMAP.md` (this bullet's pointer).

### Non-goals

- No fixing, committing, pushing, or thread resolution. `babysit-prs` (015) fixes and
  orchestrator stage 6 addresses comments; this job only looks.
- No fork PRs, no multi-select fan-out, no inspect-only CLI subcommand.
- No change to `babysit-prs`'s scope, and no re-review of pipeline PRs.
- Not a replacement for stage 5: pipeline PRs are excluded by design, not by
  omission.

## Acceptance criteria

**Phase A — the selector (no agent runs)**

1. `kind: review` loads with `repo`/`repository`/`workspace`/`checks` rejected,
   `select` required and validated, `target` absent-or-`pr`, and
   `max_attempts_per_target` defaulting to 1; `Job.repo` is the managed-repos base
   dir. Test: `test_review_job_config_validation`
2. Selection returns only open, non-draft, same-repo PRs whose author matches
   `gh api user`; a stale/renamed login is `skipped (author_mismatch)`. Test:
   `test_review_scan_selects_open_non_draft_prs`
3. `auto/*` head branches are skipped as `routine_owned_branch` by default, and are
   candidates when `include_auto_branches: true`. Test:
   `test_review_scan_skips_auto_branches_unless_opted_in`
4. Fork PRs are skipped as `fork_pr` and never become candidates. Test:
   `test_review_scan_skips_fork_prs`
5. Candidates are ordered oldest-first with a total tie-break, and the
   `max_workers_per_tick` cap cuts the newest, not the backlog. Test:
   `test_review_scan_orders_oldest_first_and_caps_newest`
6. A candidate whose PR already carries this job's marker for that target is
   `skipped (review_already_posted)` with zero dispatches — and another job's marker
   for the same PR is *not* a match; a `gh` search failure is
   `failed (search_failed)`, never an empty candidate set; a mode returned at
   GitHub's 100-node ceiling records `search_truncated`. Test:
   `test_review_scan_marker_guard_and_search_fail_closed`
7. Across ticks, a target with one recorded attempt is skipped as
   `max_attempts_exceeded` without dispatching, and `#7` in repo A does not consume
   `#7`'s budget in repo B. Test: `test_review_attempts_are_counted_per_target_key`
8. The aggregate report lists every candidate with its dispatch/skip reason; a tick
   with nothing eligible records `done`, `dispatched: 0`, and sends no notification
   under `notify_policy: on-failure`. The shipped `deploy/jobs.d/review-my-prs.yaml`
   loads through `load_config_dir` and asserts `kind`, `select`, `enabled: false`.
   Test: `test_review_report_lists_candidates_and_skips`,
   `test_shipped_review_job_config_validates`

**Phase B — dispatch, review, verify**

9. Each dispatched PR's repo is cloned to `<base>/<owner>/<repo>` at its own default
   branch, with exactly one `gh api repos/...` call per distinct repo per tick, and
   the worktree is created from the fetched head ref. Test:
   `test_review_dispatch_creates_per_repo_checkout`
10. A head branch already held by a *different* worktree is
    `skipped (head_branch_checked_out)` and no worktree is created. Test:
    `test_review_dispatch_skips_other_worktrees_head_branch`
11. The job's own clean review worktree is reused on a second attempt, and a stale one
    is force-removed and re-added rather than colliding. Test:
    `test_review_dispatch_reuses_own_review_worktree`
12. The review worktree is removed after the worker settles on both success and
    failure, leaving the head branch unchecked out. Test:
    `test_review_dispatch_removes_worktree_after_settle`
13. Post-condition over `pr_review_bodies` (reviews **and** issue comments, one
    call): marker present → `done` + `gate: passed`; absent →
    `failed (review_not_posted)`; the read failing →
    `failed (review_verification_failed)` and never `done`. Test:
    `test_review_verification_is_fail_closed`
14. One repo failing to clone or sync fails only that repo's targets; the other repos
    still dispatch and the tick does not crash. Test:
    `test_review_dispatch_isolates_per_repo_failures`
15. A live worker for a target blocks a second one, matched by the shared
    `rt-<job>-rv<h6>` prefix; the full name is ≤ 32 chars for a 24-char job name and
    the run tail is never truncated away. Test:
    `test_review_live_agent_prefix_guard_and_name_cap`
16. The injected prompt names the target key, carries this job's exact marker and the
    read-only clause, invokes the `code-review` skill, and a job-level `prompt`
    overrides it. Test: `test_review_prompt_is_read_only_and_idempotent`
17. The dispatched PR is reviewed exactly once across ticks: a prior
    `done`/`verified: true` record for the same `target_key` makes the next tick skip
    it, so no duplicate review is ever posted. Test:
    `test_review_is_not_repeated_across_ticks`
18. `babysit-prs` is untouched: the shared-helper extraction leaves
    `ensure_repo`'s `repo:`/`repository:` behavior identical and the whole existing
    `tests/test_auto_fix.py` + `tests/test_tick.py` suites pass unmodified. Test:
    `test_ensure_checkout_refactor_preserves_babysit` (plus the two existing suites)

## Why these tests

- 1–5 pin the *selection*, which is the part that decides what the routine speaks
  into on the operator's behalf. The `auto/` and fork exclusions are the two
  overlaps that would make it noisy, and oldest-first is what keeps a cap from
  starving the backlog.
- 6, 7, 17 pin the three ways a review loop can fail open: re-reviewing a posted PR
  forever, re-reviewing across ticks after a restart, and re-reviewing when the
  per-repo PR numbering collides. They are the reason this job is safe to leave
  enabled.
- 8 pins the audit trail: before Phase B exists, the report *is* the feature's
  visible output, and `enabled: false` in the shipped example is a deliberate
  posture, not an oversight.
- 9–12 pin the cross-repo mechanics. Reusing another owner's worktree is issue 036's
  exact bug in a new form, and leaving a worktree behind would recreate the collision
  the reuse logic exists to avoid.
- 13, 14 pin the two fail-closed paths: an unverifiable review and a broken clone must
  both surface as failures, never as a quiet success.
- 15, 16 pin the two ways a dispatch can misbehave quietly — a duplicate agent, and an
  agent that pushes to a PR the job only meant to read.
- 18 is the regression budget for the shared refactor: the only code this touches
  outside its new module is `tick.py`'s and `repos.py`'s, and `babysit-prs` must
  behave exactly as before.

## Log

- **2026-09-30:** refined from the ROADMAP Parking Lot bullet
  "**Review `@me` PRs across repos**" (2026-08-30 brainstorm), selected by
  `herdr-routines refine-issue` under issue 029. The bullet's stated prerequisite —
  issue 006's `jobs.d/` layout and 049's `kind:` dispatch key — has shipped, so the
  shape here is a new `kind` branch plus one new file, not a new trigger mode.
  The design question the bullet left open ("idea, not designed") is the guard
  problem: unlike `pr_health`, "unreviewed" does not clear itself when the review
  lands, so the content marker plus a per-target-key budget are load-bearing, not
  hardening. Fork PRs and the inspect-only CLI are deferred and named.
