---
name: babysit-prs
description: >
  Orchestrate every open PR in this repo toward merge: triage failed CI runs
  (flaky vs. real), resolve merge conflicts, get unreviewed PRs reviewed via
  a free opencode model in a herdr pane, dispatch address-pr-comments on
  reviewed PRs with findings, and enable auto-merge on anything clean. Use
  when the user asks to "babysit the PRs", "clear the PR queue",
  "orchestrate the open PRs", or "get everything mergeable" for this repo.
  Requires an interactive herdr session — do not use from a headless/cron
  context (see the Herdr caveat below).
---

# Babysit PRs

Runs this repo's multi-PR merge pipeline end to end, one pass at a time.
This is an orchestration *procedure*, not a single tool call — expect to
spawn several `Agent` calls and a couple of herdr panes, and to loop back
after each merge because landing one PR can put another into conflict.

Adapted from the version of this skill used in `fitted` (a different repo,
with its own self-hosted CI runner and `development`-branch process) —
this repo is a solo personal project with GitHub-hosted runners and `main`
as the only branch, so the runner-health step and any `development`-branch
assumptions from that version don't apply here. Base branch is always
`main`; merges are squash-only (enforced by the repo's branch ruleset, not
just convention — `allow_merge_commit`/`allow_rebase_merge` are both off).

Mental model: **Claude Sonnet agents write/fix code, a free opencode model
(via herdr) reviews it.** You are the orchestrator tying both together with
`gh`, never the one hand-editing PR branches yourself except for trivial,
mechanical merge-conflict resolution you're confident about.

## Herdr caveat

Getting a PR reviewed needs a live, interactive herdr pane running opencode.
This only works when invoked from an active herdr-managed session
(`test "${HERDR_ENV:-}" = 1`). If that check fails, stop and tell the user —
don't fall back to a headless `opencode run` fanout. Free-tier opencode
models churn frequently and several have failed silently in headless mode
in the past — hanging, posting empty/thin reviews, or mislabeling PRs — so
treat a headless fanout as unreliable rather than a fallback (see the review
step below).

## Step 1 — Triage failed CI runs before retrying

For every open PR, check `gh pr checks <n>`. For each failed job, read its
log (`gh run view <run-id> --log-failed`) and classify before touching
anything:

- **Known transient/runner flake** — retry with `gh run rerun <run-id>
  --failed`.
- **Real, persistent infra problem** — e.g. a GitHub Actions billing/spend
  limit issue (`"recent account payments have failed or your spending limit
  needs to be increased"`). This is not flaky and a retry will not help.
  **Tell the user, don't try to fix it yourself** — it's an account-level
  setting only they can change (Settings → Billing & plans). Until it's
  resolved, treat "no CI signal" as the reality, not "CI passed."
- **Real, non-transient failure with a fix available** — e.g. `pip-audit`
  failing on a freshly-published CVE against a *transitive* dependency
  pinned in a lockfile. Investigate whether it's fixable with a lockfile
  bump (`uv lock --upgrade-package <pkg>`) before assuming it needs a code
  change or should just be added to an ignore-list. If it's a fix that
  unblocks CI for every other open PR, treat it as **P0**: branch, fix,
  review, merge it first via the steps below, *then* merge it into every
  other open PR's branch before continuing their triage (`git fetch origin
  && git merge origin/main`, resolve trivial conflicts, push) — their own
  CI won't go green until they have the fix.
- **Real code failure** — don't retry; treat like any other reviewable
  problem (an actual bug the PR introduced).

## Step 2 — Merge conflicts

For any PR with `mergeStateStatus: DIRTY` / `mergeable: CONFLICTING`, spawn
a Sonnet `Agent` (subagent_type default, `isolation: "worktree"`) with:

- The PR number, branch name, and base branch (`main`).
- Instruction to fetch, merge (or rebase) `main` in, resolve conflicts by
  reading both sides and preserving both intents (don't silently drop
  either side), run the relevant test suite, and push (normal push, or
  `--force-with-lease` only if a rebase was used and clearly flagged).
- Explicitly: do NOT merge or touch auto-merge — that's a later step.

**This can happen more than once per PR.** Every time another PR merges
into `main` during this session, every other open PR's merge state can flip
from clean to `DIRTY`. Re-check `gh pr list --state open --json
number,mergeStateStatus` after each merge and re-run this step as needed —
don't treat "resolved once" as permanent.

If you need to `git merge` yourself for something this trivial and you're
confident about it (e.g. a pure lockfile-only fix propagating), doing it
directly is fine — reserve the Sonnet agent for conflicts that need
judgment about actual source changes. Either way, run the affected test
suite before pushing.

## Step 3 — Get unreviewed PRs reviewed (via herdr + opencode)

A PR needs review if it has no `reviewed` or `ready-to-merge` label (or has
`pending-review`). For a trivial, self-authored, mechanical PR on this solo
repo (e.g. a pure dependency bump, a lockfile-only change), it's fine to use
judgment and skip straight to Step 5 instead of spinning up a review pane —
reserve the full review pipeline for PRs with actual logic/config changes
worth a second pair of eyes.

1. Split a sibling pane per PR needing review (don't reuse one pane for
   multiple sequential reviews if you can parallelize):

   ```bash
   herdr pane split --current --direction right --cwd "$PWD" --no-focus
   ```

   (or `--direction down` off an already-split pane — avoid stacking splits
   in the same direction until columns/rows get unusably thin).

2. Start an opencode agent in the new pane:

   ```bash
   herdr agent start <name> --kind opencode --pane <pane-id>
   ```

   Model choice is a judgment call each run — check `opencode models | grep
   -- '-free$'` for what's currently available; the free tier churns
   constantly. Don't hardcode a model list in this skill; pass one via
   `-- -m <model>` on `agent start` if you want a specific one, otherwise
   take the default.

3. Prompt it:

   ```bash
   herdr agent prompt <name> "/code-review <PR URL>" --wait --timeout 120000
   ```

   Expect the wait to time out (multi-agent code review legitimately takes
   10–25 min) — that's not a failure, it just means the command moved to
   background. Poll with `herdr agent get <name>` until `agent_status` is
   `idle`, then `herdr agent read <name> --source recent-unwrapped --lines
   150` to see what it posted.

### Verify the label transition — do not trust it blindly

The `code-review` skill's contract is: it **only ever sets the `reviewed`
label**, swapping out `pending-review` if present. It never sets
`ready-to-merge` — that label is exclusively `address-pr-comments`'s to
set, and only after threads are actually addressed and replied to.

After a review session goes idle, check the actual label
(`gh pr view <n> --json labels`) against what it *should* be:

- If the model jumped straight to `ready-to-merge` **and there's an
  unresolved blocking finding in its own posted review**, that's a
  mislabel. Correct it back: `gh pr edit <n> --remove-label "ready-to-merge"
  --add-label "reviewed"`. Do not proceed to auto-merge on the strength of a
  label alone; check for inline comments with `[blocking]` markers
  regardless of what's labeled.
- If it posted a thin, generic-sounding review (a short body, no inline
  comments, on a PR that plausibly has something to say) treat it with
  suspicion — re-read the diff yourself or spawn a second model as a check
  before trusting a "no issues found" verdict on anything non-trivial.

## Step 4 — Address review findings

If a PR is `reviewed` and the review has any `[blocking]` or non-blocking
inline comments still unaddressed, spawn a Sonnet `Agent`
(`isolation: "worktree"`) instructed to run the `address-pr-comments` skill
against that PR number. Give it the specific findings you already know
about (don't make it re-discover context you have), but tell it to fetch
the live threads itself rather than trust your summary blindly. Also have
it merge in `main` first if Step 1/2 identified a pending fix that hasn't
landed on this branch yet.

`address-pr-comments` sets the `ready-to-merge` label itself once done —
you don't need to.

## Step 5 — Auto-merge

Once a PR is CLEAN (not DIRTY) and it's genuinely `ready-to-merge` (reviewed
with no unresolved blocking findings, or judged trivial enough to skip
review per Step 3):

```bash
gh pr merge <n> --auto --squash
```

Never merge directly (`gh pr merge` without `--auto`) unless the user
explicitly asks for an immediate merge, or CI has no real signal right now
(e.g. the billing issue above) and the user has explicitly accepted merging
without it for this specific PR.

If `enablePullRequestAutoMerge` fails with `"Pull request is in unstable
status"`, that's transient (checks still settling) — just retry once CI
progresses, don't treat it as a real error.

## Loop until clear

After every merge, re-run `gh pr list --state open` — a merge can conflict
or re-trigger CI on the remaining PRs (Step 2's re-check applies here too).
Keep cycling Steps 1–5 across the remaining open PRs until the list is
empty or everything left is genuinely blocked on something only the user
can decide.

## Wrap-up

When the queue is clear (or as clear as it's going to get this pass), give
the user a concise summary: what merged, what infra issues were found and
how, what conflicts were resolved and how, what review findings were
addressed, and — if more than one model reviewed PRs this run — a short
note on which one's output you'd trust more next time and why.

Close any herdr panes you opened for this run once their work is done,
unless the user wants them kept open.
