# spec: Auto-fix pull requests (standing job) (20260829T050025Z)

## Problem

`herdr-routines` opens PRs from scheduled jobs (`src/herdr_routines/runner.py:262` `build_branch_name` → `auto/<job>-<ts>`) and from the pipeline (`docs/pipeline/design.md:32` `auto/pipeline-<run_id>`), but after `gh pr create` there is no unattended follow-up when CI turns red or reviewers post `blocking` threads. Two manual fallbacks exist today:

- `address-pr-comments` / `babysit-prs` skill invoked by hand (PR URL → agent → `gh pr view --json comments` + `gh api graphql` `reviewThreads` → fix, push, reply/resolve), and
- pipeline stage 6 (`docs/pipeline/design.md:56` `pl-6-<run_id>` resume of `pl-3` session) which only covers the PR the pipeline itself just opened, capped at 2 iterations.

For routine-opened PRs the gap is permanent: a nightly job can merge-block overnight on a lint/typecheck failure that a constrained fix prompt could have resolved, or leave `blocking` review threads unresolved until a human notices. The general fix is a standing scheduled job that, on each `tick` (`src/herdr_routines/tick.py:86` `run_tick`), enumerates open routine-owned PRs with failing checks or unresolved threads and dispatches bounded fix workers — the skill pattern as a scheduled job rather than a manual invocation (`docs/process/issues/015-auto-fix-pull-requests.md:10`).

Goal: same safety properties as the scheduler that already exists — never touch a PR the routine didn't open, never loop forever on one PR, always leave an audit trail — applied to PR babysitting.

## Approach

Add one new scheduled job kind that reuses the existing `tick`/`history`/`herdr.py` machinery and the stage-6 `gh` thread handling already proven in the pipeline.

### 1. Config surface (extends `jobs.yaml`, `src/herdr_routines/config.py:103` `Job`)

Prefer a distinct job kind over overloading every job's prompt. Options, pick at build after checking `src/herdr_routines/config.py:58` name cap:

- **A (preferred):** new optional block on a `Job` — e.g. `auto_fix: { enabled, repo, branch_prefix, max_prs_per_tick, max_attempts_per_pr, timeout_ms, agent_kind, model }` — with its own `Job` (`name: auto-fix-prs`) whose `cron` drives enumeration. `branch_prefix` defaults to `auto/` (matches `runner.py:264` and `gc.py:17` `PIPELINE_PREFIX` `auto/pipeline-` exclusion). `max_prs_per_tick` defaults to 3, `max_attempts_per_pr` defaults to 3.
- **B:** a top-level `auto_fix_prs:` stanza parallel to `jobs:` with the same fields but no `cron` reuse. Equivalent at runtime; choose the one that keeps `src/herdr_routines/config.py:138` `load_config` validation simpler (single `Job` list).

Validation (mirrors `config.py:213` cron/`repo`/kind checks): `repo` is a git repo, `branch_prefix` non-empty, `max_*` non-negative ints, `agent_kind` in `VALID_AGENT_KINDS` (`config.py:19`), `model` only for kinds in `AGENT_MODEL_FLAGS` (`config.py:50`), prompt or prompt template for the fix worker (default template ships in code, not in YAML).

Authorship filter (must satisfy acceptance "never touches a PR not opened by a herdr-routines job"): a PR is eligible only when **both** hold:

1. `headRefName` starts with `branch_prefix` (`auto/`), and
2. `author.login` equals the authenticated `gh` user (`gh api user --jq .login`) or, if `author` is a bot/app, the branch prefix alone is treated as provenance — but never fall back to "any open PR with red CI." The branch prefix is the same provenance the pipeline and `runner.py` already use; `gc.py:57` `list_auto_branches` proves the enumeration is cheap and local.

Enumeration (inside the tick, sequential, bounded): at `run_tick` for this job, after the existing `has_ever_been_seen`/`find_stale_running`/`is_currently_running`/`_live_agent_exists` guards (`tick.py:106`):

1. `gh pr list --repo <owner>/<repo> --state open --limit 100 --json number,headRefName,author,url` → filter by branch prefix + author.
2. For each candidate, fetch `gh pr view <n> --json statusCheckRollup,headRefName` and GraphQL `reviewThreads(first:50){nodes{isResolved comments(first:1){nodes{body}}}}` (`docs/pipeline/design.md:211` verified query; REST `repos/.../pulls/.../threads` 404s). Eligibility: any `statusCheckRollup[].state` in `FAILURE|ERROR|TIMED_OUT` (or GraphQL `checkSuite` equivalent) **or** any thread with `isResolved==false` (scope to `blocking` threads if the review skill convention is required; default is any unresolved thread — narrower scope is a one-line `test("blocking")` filter as stage-6 does).
3. Sort eligible oldest-first (PR number ascending) and take at most `max_prs_per_tick`.
4. For each taken PR, check retry budget: count prior `history.jsonl` records for this job+PR where `extra.pr_number==<n>` and `state` in `done|failed` but fix did not clear the signal (i.e. the next tick still finds the same PR eligible). If count >= `max_attempts_per_pr`, skip and surface: `history.append` `skipped` with `reason: max_attempts_exceeded`, `_notify` (`tick.py:264` `notification_show --sound request`). No further attempts for that PR until a human pushes or closes it (which resets eligibility).

Dispatch (one worker per PR, reusing `runner.py` patterns, not a new Herdr primitive):

- Reuse the shared-branch checkout already proven by the pipeline's single-worktree handoff (`design.md:66`): `herdr worktree create --cwd <repo> --branch <prHeadRef> --base <prHeadRef>` is invalid for an existing branch — create a linked worktree pinned to the PR head via `git worktree add` or `herdr worktree create` against the parent clone then `git -C <wt> checkout <branch>`; pick the form that `herdr worktree:3` validates at build (empirical check: `worktree create --cwd <repo> --branch <existing>` errors or duplicates branch `design.md:68`).
- Naming: worker agent `rt-<job.name>-pr<n>-<run_id>` (fits `config.py:60` `NAME_RE` 32-char cap via `rt-` prefix; truncate or hash if PR suffix would overflow — same cap that forced `Job` name ≤24).
- Prompt template (substituted `src/herdr_routines/runner.py:267` style): PR number, branch, failing check names/logs (`gh pr checks` or `statusCheckRollup`), unresolved thread bodies JSON, instructions: fix code, run `uv run pytest -q` + `uv run ruff`, commit, `git push`, then reply to each thread body and `gh api graphql` `resolveReviewThread` for threads addressed (stage-6 pattern `design.md:211`). Bounded `timeout_ms` per PR.
- Runner integration: call `herdr.py:308` `agent_prompt_wait_with_watchdog` with `poll_interval_s`/`markers` reuse (`runner.py:49` `DEFAULT_FAILURE_MARKERS` + `runner.py:184` `_matched_failure_marker` guard `marker not in prompt_text`) so quota-wedge fast-fail (`design.md:314`) applies to fix workers too.
- Ordering (`runner.py:516` success vs `failed`/`interrupted_unknown`): identical — tail capture (`runner.py:157` `_capture_visible_tail`) before `_close_run_pane` (`runner.py:176`), `session_id` capture for resume-and-inspect (`runner.py:218`).

Logging/surfacing (acceptance: "Fix attempts and thread replies are logged; a PR that keeps failing after N attempts is left alone and surfaced"):

- Each PR fix is its own `HistoryRecord` (`history.py:20` terminal states) with `job == <auto-fix job name>`, `run_id == <job>-<scheduled_occurrence>` plus `extra: {pr_number, headRefName, attempt, eligible_reason, fix_worker_agent, pane_id, report_path, report_written, final_agent_status}`. Thread replies and `isResolved` transitions are recorded in the per-PR report file (`default_reports_dir()` `runner.py:246` → `reports/auto-fix-<run_id>-pr<n>.md`) and the tail `.tail.txt`.
- The tick's aggregate report (`reports/<run_id>.md` per `docs/plan-v1.md:386` `$ROUTINE_REPORT`) lists enumerated/eligible/dispatched/skipped-max-attempts counts so `herdr-routines history <job>` tells the full story without extra state.
- No new mutable state file beyond `history.jsonl`; attempt count is derived by counting prior terminal records for that PR — same derivation `schedule.py:83` uses for `last_terminal_run` (`history.py:112`).

Tick integration:

- Extend `_process_job` (`tick.py:103`) with a branch: if `job` is the auto-fix kind, run `enumerate→filter→cap→attempt-check→dispatch` loop sequentially in the same `tick_lock` (`tick.py:46`). One tick never spawns more than `max_prs_per_tick` workers and never loops over a single PR (one `agent_prompt_wait` per PR). Overlap protection: existing `tick_lock` plus `is_currently_running` (`history.py:166`) and `_live_agent_exists` (`tick.py:275` `LIVE_AGENT_STATUSES==working`) still gate the auto-fix job itself, not each PR worker — add a per-PR live-agent check (`rt-<job>-pr<n>` still `working`) to avoid dispatching a second fix for a PR already being fixed.
- Keep `TickOutcome.any_job_failed` (`tick.py:68`) semantics: a dispatched PR that returns `failed`/`interrupted_unknown` flips it so systemd sees the unit as `failed`; `missed`/`skipped`/`not due` do not.

GH identity and repo detection:

- Repo owner/name from `git -C <repo> remote get-url origin` (parse `owner/repo`, same as pipeline stage 4 `gh pr create --repo <owner>/<repo>` `design.md:128`); fail the enumeration with `failed` + `reason: gh_auth_missing` if `gh auth status` is not valid. The Pi's `GH_TOKEN` requirement is already documented (`design.md:118` `GH_TOKEN` / allowlist).

### 2. Prompt / skill reuse

Fix-worker prompt reuses pipeline stage-6 instructions and the `code-review` skill's `blocking`/`non-blocking` tier convention (`docs/pipeline/spec.md:35` stage graph) but runs in the fix job's own worktree, not the pipeline's shared worktree. No change to `docs/pipeline/orchestrator-prompt.md`.

## Files touched

- `docs/pipeline/runs/20260829T050025Z/spec.md` — this file (per-run spec, `docs/pipeline/design.md:79` G-15).
- `src/herdr_routines/config.py` — extend `Job`/`_JOB_ALLOWED_KEYS` (`config.py:76`) and `_DEFAULTS_ALLOWED_KEYS` (`config.py:62`) for `auto_fix` / `max_prs_per_tick`/`max_attempts_per_pr`/`branch_prefix` (or the equivalent `auto_fix_prs` stanza); validate as non-negative ints and branch prefix non-empty; keep `_JOB_REQUIRED_KEYS` (`config.py:75`) unchanged for non-auto-fix jobs.
- `src/herdr_routines/herdr.py` — no new Herdr CLI primitive required (reuse `agent_prompt_wait_with_watchdog` `herdr.py:308` + `agent_read_visible` `herdr.py:401`); if enumeration is wrapped, add a small `GhClient` (subprocess `gh` wrapper) behind the same `CommandRunner` seam (`herdr.py:88`) so tier-2 tests can script `gh pr list` / `gh pr view` / GraphQL `gh api graphql` responses without a live `gh` binary.
- `src/herdr_routines/tick.py` — branch in `_process_job` (`tick.py:103`) for the auto-fix job kind: enumerate, filter by `branch_prefix`+author, check CI (`statusCheckRollup`) and `reviewThreads` (GraphQL), enforce `max_prs_per_tick` and `max_attempts_per_pr` via `history.py:102` `read_job` / `last_terminal_run`, dispatch per-PR workers sequentially, write `history.append` (`history.py:73`) per PR and aggregate counts.
- `src/herdr_routines/runner.py` — add `auto_fix.py`'s per-PR worker spawn helper or a `dispatch_fix_worker` in `runner.py` that builds branch checkout + prompt substitution (`runner.py:267` `substitute_prompt`) and calls `agent_prompt_wait_with_watchdog`; reuse `build_branch_name` (`runner.py:262`), `_prompt_with_watchdog` (`runner.py:95`), `WATCHDOG_POLL_INTERVAL_S` (`runner.py:54`), `_close_run_pane` (`runner.py:216`), and report/tail ordering (`runner.py:469` comment).
- `src/herdr_routines/auto_fix.py` (new, or `babysit.py`) — pure-ish module: `list_open_prs`, `is_eligible` (CI + threads), `attempt_count_for_pr` (scans `history.jsonl`), `build_fix_prompt` — no subprocess except via injected `GhClient`/`HerdrClient`, so unit-testable with frozen `now` and fixture history like `schedule.py:83` `decide`.
- `src/herdr_routines/history.py` — no schema change; reuse `HistoryRecord.extra: dict[str, Any]` (`history.py:49`) with `pr_number`/`attempt` keys for the new job; helper `attempt_count_for_pr(path, job_name, pr_number)` to avoid duplicating scan logic.
- `src/herdr_routines/schedule.py` / `src/herdr_routines/scheduled.py` — untouched except that `scheduled`/`ps` already surface `last_state`/`next_fire` (`scheduled.py:35` `ScheduledRow`); auto-fix job's schedule row is visible there with no code change.
- `tests/test_config.py` — validation for new keys: `max_prs_per_tick`/`max_attempts_per_pr`/`branch_prefix` parsing, defaults, `HerdrCliError` on bad values (mirror `test_failure_markers` `test_config.py:296`).
- `tests/test_history.py` — `attempt_count_for_pr` counting, `max_attempts_exceeded` → `skipped` path, stale-run still blocks eligibility.
- `tests/test_auto_fix.py` (new) — tier-1/2 matrix: eligible filter (red CI only, unresolved thread only, both, neither), branch-prefix+author confinement (human PR with red CI not touched), cap `max_prs_per_tick` (oldest-first), `max_attempts_per_pr` skip after N, `gh` unreachable → empty with warning not crash, GraphQL 404 fallback (stage-6 verified: REST `threads` 404s `design.md:211` comment).
- `tests/test_tick.py` — live-agent confinement (`tick.py:275` `_live_agent_exists` filters `working` only, not `idle`/`done` — same fix as `test_tick.py:53` regression); auto-fix dispatch capped call-count assertions via `ScriptedClient`/`FakeFullClient`.
- `docs/process/issues/015-auto-fix-pull-requests.md` — flip `status: open` → `done` or `in-progress` when implemented (out of scope for this spec file itself).
- No change: `src/herdr_routines/gc.py` (must keep `auto/pipeline-*` excluded `gc.py:17` `PIPELINE_PREFIX`), `deploy/systemd/herdr-routines.service` `TimeoutStartSec` (`cli.py:294` `SYSTEMD_TIMEOUT_MARGIN_SECONDS`) — bump only if auto-fix workers run sequentially within the same tick and worst-case sequential time exceeds current `required_s` sum; otherwise keep as is.

## Risks

- **Wrong-PR writes (highest).** Enumerating all open PRs and pushing fixes is a write-amplification of the single-user threat model (`docs/pipeline/design.md:254` blast radius). Mitigation: strict `branch_prefix` (`auto/`) + `author` provenance gate, fail-closed on ambiguous identity (`gh api user` failure → no dispatch), tier-2 tests that assert a red-CI PR on `feature/not-auto` is not touched even when eligible.
- **Infinite fix loop on one PR.** An agent could push a "fix" that re-introduces the same CI failure, making every tick re-eligible. Mitigation: `max_attempts_per_pr` counted from `history.jsonl` (append-only, same durability as `schedule.py:100` `since` derivation), capped per run (`max_prs_per_tick`) plus per-PR cap, and `skipped` with `reason: max_attempts_exceeded` + `notification_show --sound request` (`tick.py:264`) so it surfaces rather than silently retrying.
- **Overlapping ticks dispatch same PR twice.** 5-min `herdr-routines.timer` `OnCalendar=*:0/5` (`docs/plan-v1.md:262`) plus `tick_lock` (`tick.py:46` `flock`) prevents two ticks running concurrently, but sequential ticks 5 min apart could both dispatch the same PR if the first worker is still `working`. Mitigation: per-PR live-agent check (`rt-<job>-pr<n>` `working` in `LIVE_AGENT_STATUSES` `herdr.py:54`) before dispatch, plus `is_currently_running` (`history.py:166`) for the auto-fix job itself; both are the same guards that fixed `test_tick.py:164` recurring-root skip-forever bug.
- **GH CLI / GraphQL shape drift.** `gh pr list --json` and GraphQL `reviewThreads` field names are pinned only in `herdr.py:431` fixture `tests/fixtures/api-schema.json` style; `gh api repos/.../pulls/.../threads` already 404s (`design.md:211`). Mitigation: `_try_parse_json` (`herdr.py:573`) + shape guards, fail-open to empty eligibility with warning (exit 0, no dispatch) when `gh` shape is unexpected, add argv assertions (`test_herdr.py` pattern) for every `gh` invocation.
- **CI signal is flaky or incomplete.** `statusCheckRollup` can be empty while checks are still pending, or red on infra flakes unrelated to code. Mitigation: treat `PENDING`/`IN_PROGRESS` as ineligible (not failing), only `FAILURE|ERROR|TIMED_OUT` qualifies; do not treat a pending run as a fix trigger. Follow-up: optional `ci_required_contexts` allowlist if flaky contexts need exclusion (defer to v2).
- **Branch checkout race / existing `auto/*` branch collision.** `herdr worktree create --branch` semantics for an existing branch are explicitly called out as invalid in `design.md:68` (`G-6`); pipeline's fix is `git worktree add` against the parent clone. Mitigation: build-time empirical check (`herdr worktree create` against an existing `auto/*` branch) and a deterministic fallback to `git worktree add`, verified once on the Pi where the job will actually run.
- **Timeout budget / `systemd TimeoutStartSec` trap.** A tick that sequentially fixes 3 PRs × 30-min workers occupies the `herdr-routines.service` `oneshot` for 90 min; `TimeoutStartSec=infinity` would wedge forever on a dead `herdr-server` (`docs/plan-v1.md:279` `TimeoutStartSec must be finite`). Mitigation: `validate --systemd-unit` (`cli.py:294` `_check_systemd_timeout`) must sum all enabled jobs' `start_timeout_ms+timeout_ms` including auto-fix's `max_prs_per_tick * per_pr_timeout`; keep `WATCHDOG_POLL_INTERVAL_S` fast-fail on quota (`runner.py:54`) to shorten, never lengthen, the budget.
- **Auth / allowlist on the Pi.** `gh` needs `GH_TOKEN` + `herdr`/`git`/`gh`/`uv`/`jq` allowlist in `~/.config/opencode/opencode.json` (`design.md:118`), same as orchestrator allowlist — an un-allowlisted `gh api graphql` wedges as `blocked`. Mitigation: one manual dry-run of `herdr-routines run auto-fix-prs --dry-run` + `gh auth status` on the Pi before enabling the timer.
- **Spec-path hygiene (G-15).** Must stay at `docs/pipeline/runs/<run_id>/spec.md` (`design.md:79`); writing to sibling `spec.md` paths reintroduces PR #28/#29 full-file merge conflict.
