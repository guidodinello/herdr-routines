# spec: `self-update` — keep the runner checkout current (run 20260929T050000Z)

Implements `docs/process/issues/053-runner-checkout-self-update.md`. No code here;
this is the plan the implementer stage codes against.

**v2** — revised after an independent review of v1 against the code on disk. Every
finding, its tier and its fix are in `## Review findings`; the delta is summarised in
`## Changelog v1->v2`. Read the findings before the approach — five of them change the
design.

Tier vocabulary used throughout, so it is greppable and unambiguous:

- **`[blocking]`** — v1 as written is wrong, unimplementable, or silently unsafe. v2
  changes the design; coding v1 verbatim produces a red CI, a feature that never fires,
  or a false "updated" report.
- **`[non-blocking]`** — verified correct as written, or a considered-and-accepted
  detail recorded so the implementer does not re-derive or re-litigate it.

## Problem

The Pi runs herdr-routines from **two** checkouts, and only one of them maintains itself:

| Checkout | Used by | Updated by |
| --- | --- | --- |
| `~/projects/herdr-routines` | `herdr-routines.service`, `-watchdog.service`, `-digest.service` (`WorkingDirectory=`, `uv run herdr-routines …`) | **a human, by hand** |
| `~/.local/state/herdr-routines/repos/herdr-routines` | the `feature-pipeline` job's `repo:` → `scripts/pipeline-launch.sh` + the orchestrator's `uv run herdr-routines gate/pick-feature` | `ensure_repo` (`repos.py:42`) → `_fetch_and_fast_forward` (`repos.py:93`), every run |

The second row is automatic; the first is the one that actually executes `tick`. So a
fix merged to `main` reaches the pipeline launcher the next night while the tick,
watchdog and digest keep running whatever someone last pulled. The two halves drift
apart and disagree — `docs/process/pi-update-runbook.md:8` already calls the
runner "**Manual**".

**Incident 2026-09-27.** The runner was stuck at `01e6fd9` (PR #116, 2026-09-10) for 17
days — missing #117–#127, including #121 (routine-job retry), #122 (retention), #123
(model validation) and #125 (pipeline quota fast-fail + `fallback_model` retry). On
2026-09-20 the pipeline hit the OpenCode quota; the **new** `pipeline-launch.sh` wrote
`## Outcome: failed (quota_exhausted)`, but the **old** tick had no `quota_exhausted`
branch in `_classify_pipeline_outcome` (`tick.py:1614`) so it recorded
`reason: orchestrator_failed` and never launched the `fallback_model` retry #125 had
added 8 days earlier. #125 shipped, was green, was merged — and silently did not run on
the only host it was written for.

Two structural causes, both fixable by one command:

1. The manual fast-forward. `docs/process/pi-update-runbook.md` exists precisely to
   document it, including its two manual guards: **green CI** (step 0, "never ship a red
   runner") and **config migrations** (a reshaped schema can make an old job entry
   *silently ignored* rather than rejected, dropping the job with exit 0 — the PR #56
   and issue 049 examples). Both guards are worth keeping; neither is worth a human
   remembering them 17 times in a row.
2. Nothing in the running system notices it. The digest reports job outcomes, not the
   version of the code that produced them. The staleness is invisible until it bites.

The failure mode is worse than a missing feature: it is a *plausible* one. The host
looks healthy, the unit is green, jobs run — they just run last month's code. That is
the design constraint this whole command inherits, and it is why F3 (a false "updated"
report) and F5 (an unremembered rejected SHA) are both `[blocking]`: the easy way to
implement this feature produces exactly the class of failure the issue was filed about.

## Review findings

Every finding below was checked against the code on disk, not from memory. Findings
that say "verified" were verified by running the thing (a `git` sandbox, a `mypy`
sandbox copy, or reading the file at the cited line).

---

**F1 — `[blocking]` — widening the `GhClient` protocol breaks a required CI gate, so
the commit that ships this feature would refuse to self-install.**
confidence: high

v1 says: *"A new `commit_check_runs(...)` goes on the `GhClient` protocol and
`RealGhClient`; the existing test fakes are duck-typed and need no change"* and, in
Files touched, *"the four duck-typed test fakes need no edit."*

That is wrong. `GhClient` (`auto_fix.py:32`) is a `typing.Protocol`, and
`.github/workflows/ci.yml:68-85` runs `typecheck-python` → `uv run mypy` with
`[tool.mypy] files = ["src", "tests"]`. Protocol conformance is checked **statically**;
"duck-typed" only describes the runtime, and mypy is the runtime the gate cares about.

Verified empirically. On a clean copy of this tree (`uv run mypy` currently reports
`Success: no issues found in 49 source files`), adding `commit_check_runs` to the
protocol **and** to `RealGhClient` yields:

```
Found 19 errors in 2 files (checked 49 source files)
tests/test_gates.py:132,155,181,272: error: Argument 1 to "run_ci_gate"/"run_gate6"
  has incompatible type "FakeGhClient"; expected "GhClient"  [arg-type]
tests/test_auto_fix.py: (15 sites) same, via "list_open_prs"/"is_eligible"
```

(Adding it to the protocol alone yields 25 errors — the extra 6 are `src/` call sites in
`tick.py:363,368,412,415` and `cli.py:817,819` passing `RealGhClient`, which clear once
`RealGhClient` gets the method too. Only the two test files remain.)

The blast radius is the whole point: the `typecheck-python` job is red on the commit
that introduces `self-update`, so step 4 of the design — *require green CI before
updating* — will defer forever and the host will never install the very commit that
adds the feature. The fix is the CI gate working as intended; v1 just did not know it
was there.

**Fix.** Add `commit_check_runs` to `tests/test_gates.py::FakeGhClient` and
`tests/test_auto_fix.py::FakeGhClient` (both return `[]`, or a per-instance canned
list). `tests/test_tick.py::MockGhClient` and `EligiblePrGhClient` are installed by
`monkeypatch.setattr("herdr_routines.tick.RealGhClient", …)` and are never passed to a
`GhClient`-annotated parameter, so they need no edit — verified by the error list
above, which contains no `test_tick.py` entry. Add both fakes to Files touched and add
a test that pins the protocol surface (F1's acceptance item).

---

**F2 — `[blocking]` — the post-`validate` rollback is re-applied to the same bad SHA
every night, forever.**
confidence: high

v1's failure path is: ff to `new` → validate fails → `git reset --keep old` → notify →
exit 1. Nothing records that `new` was already rejected.

If `new` is genuinely bad (a schema migration the host's `jobs.d/` has not been
migrated for — the runbook's own "Config migrations" section says this happens), then
every night at 21:30 the command re-fetches, re-ff's, re-validates, re-rolls-back and
re-notifies, indefinitely. The host is correctly frozen on `old`, but the operator
gets an identical notification every evening with no escalation, no "we already
rejected this", and no way to tell "the timer is broken" from "this commit is bad".
That is the same *unattended-and-plausible* failure the issue is about, one layer up.

**Fix.** Persist the last rejected SHA and defer on it without re-testing. New
`self_update.default_state_path()` → `$HERDR_PLUGIN_STATE_DIR/self-update.json`, else
`~/.local/state/herdr-routines/self-update.json` (same convention as
`tick.default_lock_path` / `history.default_history_path`). Written **only** on a
rollback, read **only** in step 5. Payload:

```json
{"last_rejected_sha": "abc123", "reason": "validate exited 1", "ts": "2026-09-29T21:31:02Z"}
```

On a run where `origin/main` resolves to `last_rejected_sha`, log
`self-update: deferred (already rolled back: abc123 — validate exited 1)` at `warning`
and exit 0 **without** the ff, the validate, the state rewrite, or a second
notification. The check is keyed on the SHA, so it clears itself for free the moment
`main` moves. One notification per bad commit instead of one per night — which is
also the difference between "the operator knows" and "the operator mutes it".

---

**F3 — `[blocking]` — when the fast-forward is a no-op, v1 reports "updated" anyway.**
confidence: high

v1's step 4 compares `new = rev-parse origin/main` against `old = rev-parse HEAD` and
returns early only when they are equal. But `git merge --ff-only origin/main` is a
**no-op whenever `HEAD` already contains `origin/main`** — including when `HEAD` is
*strictly ahead* of it. In that case `new != old`, v1 sails through the CI gate and
the ff, and step 8 logs `self-update: <old>..<new> (0 commits)` **and sends the
success notification**, having changed nothing.

Verified in a `git` sandbox: runner checkout `main` with two **unpushed local commits**
on top of an `origin/main` that has not moved since:

```
merge: Already up to date.
OLD=d9355b4  origin/main=b21beb0  head_after_ff=d9355b4  MOVED=NO
rev-list --count old..new = 0
```

This is not a hypothetical state. It is the *most likely* real one: the issue's own
incident narrative is a hand-applied hotfix on the Pi ("Fixed by hand: `flock tick.lock
git merge --ff-only origin/main`"), and the whole reason step 3 tolerates local state is
that hosts get hand-fixed. A hand fix that was **committed** (rather than pulled)
leaves exactly this state, and the command would then cheerfully report success every
night while the host stays pinned to code nobody upstream has.

**Fix.** Make the post-ff `HEAD` re-read authoritative, and let *it* decide the
outcome. After `_fetch_and_fast_forward`, re-read `git rev-parse HEAD`:

- `HEAD_after == old` → status `up_to_date`, exit 0, **no notification**, and this is
  now the only path that reports "up to date" (step 5's `new == old` check becomes a
  cheap short-circuit before the CI query, not the decision point).
- `HEAD_after != old` → that is the `new` to report, diff and log. Use `HEAD_after`,
  not the pre-fetch `rev-parse origin/main`, for `deploy_changes` and the commit count.

And when `HEAD_after == old`, if `git rev-list --count origin/main..HEAD > 0`, log a
`warning`-level `self-update: up to date; N unpushed commit(s) on <path> (hand-hotfixed
host?)` — **log, not notify**: it would otherwise be a nightly nag. Note the
complementary case is already covered: unpushed commits *plus* an upstream move diverges,
and `_fetch_and_fast_forward` raises `RuntimeError` on it (verified: `fatal: Not
possible to fast-forward, aborting.`) → step 7's notify-and-exit-1. So after v2 the
matrix has no silent case: `{up to date, updated, diverged}` covers it.

---

**F4 — `[blocking]` — the "compare job **name sets**" safeguard is not obtainable from
`validate`; the mechanism does not exist.**
confidence: high

v1 leans on name-set comparison three times — "or having **dropped a job name** it
previously reported", "Comparing **name sets**, not just counts, is what makes the
migration hazard … visible: a single dropped job is a rollback even if a concurrently
added one masks the count", and Risk 4's "Name-set comparison (not count alone) keeps
the signal sharp". An implementer who tries to build that will find there is nothing to
build it from.

`_cmd_validate` (`cli.py:583-661`) prints exactly one line on stdout,
`ok: N job(s) valid` (`cli.py:660`), plus per-job `warning:`/`error:` lines on stderr.
There is **no job name on stdout, and no flag that emits one**. The only existing
command that prints loaded job names is `scheduled --json` (`cli.py:484-497`, one
`{"name": …}` object per job in `config.jobs`); the alternative is a new
`validate --json`, which this issue does not ask for and which would land a flag on the
new code that the *old* code — the thing actually running the pre-update baseline —
does not have.

**Fix.** v2 compares **counts only**, which is what issue 053's acceptance criterion 8
actually specifies ("reports **fewer** jobs than before"). The masked-drop case (one job
dropped, one added in the same commit) is a real residual and is recorded as
`[non-blocking]` in Risks with the exact mechanism a follow-up would use
(`scheduled --json` in the same cwd, parsed for names, as a second subprocess; or a new
`validate --json` once every checkout has it). Do **not** add a flag to `_cmd_validate`
in this change.

---

**F5 — `[blocking]` — reusing `gates._is_pending_check` on a REST check-runs payload
classifies every check as pending, so the CI gate defers forever.**
confidence: high

v1 specifies querying `gh api repos/{owner}/{repo}/commits/<new>/check-runs` (the
**REST** endpoint) and evaluating it with `gates._is_pending_check`
(`gates.py:101`) and a tolerated set of `{success, skipped, neutral}`.

Those two shapes do not match, and the mismatch fails *closed*:

- `gates.py:101` tests `check.get("status") != "COMPLETED"` and `_conclusion_of`
  (`gates.py:108`) reads `conclusion`/`state`. Those are the **GraphQL** shapes, which
  is what `gh pr view --json statusCheckRollup` feeds `evaluate_ci_checks` — and
  `gates.py:46-47` accordingly hard-codes **uppercase**
  (`FAILURE_CI_CONCLUSIONS = {"FAILURE"}`, `TOLERATED_CI_CONCLUSIONS = {"SKIPPED",
  "NEUTRAL"}`).
- The REST `check-runs` response puts the list under a `check_runs` key and uses
  **lowercase** `status` (`"completed"`) / `conclusion` (`"success"`, `"cancelled"`,
  `"timed_out"`).

Feed a REST payload to `_is_pending_check` and `"completed" != "COMPLETED"` is true for
every check, so `evaluate_commit_checks` defers on a fully green commit. The feature
then never updates, silently, and the only evidence is a daily
`deferred (<reason>)` line. That is a plausible-but-wrong failure by construction.

**Fix.** `commit_check_runs` returns `json.loads(stdout).get("check_runs", [])` (never
the top-level parse — a bare `json.loads(stdout)` yields a dict, not a list), and
`evaluate_commit_checks` **normalises case itself** (`str(v).strip().upper()` for both
`status` and `conclusion`) before comparing. Then it is correct for both vocabularies
and can be unit-tested with each. Do not call `_is_pending_check` on the raw REST dict;
either normalise first and reuse it, or write the two-line extraction locally with a
comment naming which API shape it is for. Whichever is chosen, the docstring must say
"case-normalised; safe for both the REST check-runs shape and the GraphQL rollup shape".

Two consequences worth stating: REST check-runs do **not** include legacy commit
statuses, so a repo that only uses statuses yields an empty list — which v1 already
treats as defer (correct, fail-closed). And `RealGhClient.commit_check_runs` must
raise `RuntimeError` on a non-zero exit, which v1 already maps to defer.

---

**F6 — `[non-blocking]` — `git reset --keep` is sound; verified, with one addition
that removes the main hazard rather than mitigating it.**
confidence: high

v1's Risk 1 worries that `uv run` may rewrite `uv.lock` and make `--keep` refuse. The
`--keep` half is right — verified in a `git` sandbox:

- backwards reset over a clean ff: rewinds HEAD, removes files the ff added
  (`src/new.py`), leaves `git status --porcelain` empty. Correct.
- with a local edit to a file that differs across `old..new`: **refuses**, rc 128,
  `fatal: Could not reset index file`, and HEAD stays on `new`. Correct — and it proves
  v1's "notify loudly with the SHA it is stuck on" mitigation is reachable, not
  theoretical.

The `uv.lock` half is avoidable. `.venv` is gitignored (`.gitignore:10`, confirmed with
`git check-ignore`) so the venv re-sync never dirties the tree — but **`uv.lock` is
tracked**, and `uv run` will rewrite it if it decides the lock is stale.

**Fix.** Use `uv run --frozen` for both validate subprocesses ("Run without updating the
uv.lock file", confirmed present in `uv 0.12.5`). It still syncs `.venv` from the
committed lock — so the new `uv.lock` is genuinely exercised, which is the point — but
`uv` can no longer mutate a tracked file mid-sequence. (`--locked` is the stricter
sibling: it *errors* if the lock is inconsistent with `pyproject.toml`. Tempting, and it
would catch a genuinely stale lock, but it risks a false rollback for a legitimate
lock-drift; not adopted here, named as a follow-up.)

Keep v1's post-rollback check as a hard requirement: re-read `git rev-parse HEAD` and
`git status --porcelain` after any failed rollback, and put both the actual HEAD SHA
and the raw git stderr in the notification. Never `--hard`, never force.

---

**F7 — `[non-blocking]` — `--config` is a top-level flag, so `self-update` must honour
it.**
confidence: high

v1 says config comes from `load_config(default_config_path())`. But `--config` is
declared on the **top-level** parser (`cli.py:103-108`), so every subcommand receives
`args.config`, and every existing handler resolves `args.config or default_config_path()`
(`cli.py:399` in `_load_config_or_exit`, `cli.py:585` in `_cmd_validate`). v1's
`--path` argument name also collides conceptually with `--config`'s intent.

**Fix.** `_cmd_self_update` uses `args.config or default_config_path()`. The spec still
wins over `_load_config_or_exit` (`cli.py:398`) for one reason: `_load_config_or_exit`
turns a `ConfigError` into `SystemExit(1)` with no notification, and a host whose config
no longer loads is exactly the condition this command must report loudly. So: call
`load_config` directly, catch `ConfigError` → `refused` + notify + exit 1, **and also
check `config.errors`** (the `jobs.d/` per-file soft errors that `load_config_dir`
accumulates — `_load_config_or_exit` exits on those too, and skipping the check would
mean `self-update` runs happily on a config half of which is broken).

---

**F8 — `[non-blocking]` — `TimeoutStartSec` must exceed the sum of the subprocess
timeouts, or systemd can kill the process mid-`git merge`.**
confidence: high

`repos.REPO_TIMEOUT_S = 120` (`repos.py:26`) bounds every git call in
`_fetch_and_fast_forward`, and v1's flow makes at least four git calls (repo_state ×3,
fetch, merge, `rev-list`, `diff`) plus two `uv run` subprocesses plus one `gh` call. A
host on a degraded network link — precisely the 2026-09-27 incident's "5 GHz link
degraded" — can spend 120 s per git call. v1's `TimeoutStartSec=600` can be *shorter*
than the worst case, and a SIGTERM landing between "git wrote file A" and "git wrote
file B" leaves a half-updated worktree with no live process to roll it back.

**Fix.** `validate_subprocess` passes an explicit `timeout=` (300 s) to
`subprocess.run` and treats `subprocess.TimeoutExpired` as a validate **failure** (→ the
same rollback path as a non-zero exit, not a pass). The unit's `TimeoutStartSec` is
`1800`, with a comment naming the budget: two `uv run` invocations + up to five bounded
git calls + one `gh` call + slack. The git window is the one that must not be
interrupted, so the unit is sized to never reach it.

---

**F9 — `[non-blocking]` — `--dry-run` semantics need pinning.**
confidence: high

**Fix.** `--dry-run` takes the same `tick_lock` and performs steps 1–5 (including the
fetch and the CI query — both are needed to produce a truthful `old..new` and deploy
diff), prints the intended `old..new`, the deploy diff, and the CI verdict, then stops
before step 7. No ff, no `validate`, no state-file write, no notification. A `--dry-run`
issued while a tick holds the lock legitimately reports `deferred`; that is correct and
should be said in `--help` so the manual runbook path is not confusing.

---

**F10 — `[non-blocking]` — `uv` must be resolvable, and that must be checked *before*
any git change.**
confidence: high

`docs/process/pi-update-runbook.md:84` is explicit: "`uv` is not on PATH in
non-interactive ssh — use `~/.local/bin/uv`". The update unit's `Environment=PATH=`
covers it, but a manual run (or a host where `uv` lives elsewhere) will not. v1's
ordering discovers a missing `uv` *after* the ff and then rolls back for nothing.

**Fix.** `resolve_uv()` runs as step 4, before the fetch: `shutil.which("uv")`, else
`Path.home() / ".local" / "bin" / "uv"` if it exists, else `refused` + notify + exit 1
with no git operation performed. The resolved path is passed to both validate calls.

---

**F11 — `[non-blocking]` — every symbol and line citation in v1 is real. Verified, no
change needed to the design.**
confidence: high

Checked against the files, not from memory:

| v1 claim | verified |
| --- | --- |
| `tick.tick_lock`, non-blocking, yields bool | `tick.py:85`, `@contextmanager`, `LOCK_EX｜LOCK_NB`, `mkdir(parents=True, exist_ok=True)` |
| `tick.default_lock_path()` | `tick.py:74`, `$HERDR_PLUGIN_STATE_DIR` → `…/tick.lock` |
| `_cmd_tick` holds it at `cli.py:416` | `cli.py:416` is literally `with tick_lock(lock_path) as acquired:` |
| `tick._open_pipeline_run` | `tick.py:1637` |
| `_build_pipeline_launch_argv`, detached `systemd-run --user` | `tick.py:1674`, argv contains `systemd-run`, `--user`, `--collect` |
| `_classify_pipeline_outcome` has a `quota_exhausted` branch | `tick.py:1614` |
| `tick._notify`, best-effort | `tick.py:2030`, swallows `HerdrCliError` |
| `is_currently_running` is a *different* predicate | `history.py:166`, re-exported into `tick` |
| `repos.ensure_repo` / `_fetch_and_fast_forward` | `repos.py:42` / `repos.py:93`; the ff is `fetch --prune origin` + detached-HEAD short-circuit + `merge --ff-only origin/<base>`, `RuntimeError` on failure |
| `gates.remote_owner_and_repo` reads `git remote get-url origin` | `gates.py:82` → `auto_fix.repo_owner_and_name` (`:159`), which handles `git@host:owner/repo.git` and `https://…` |
| `gates.evaluate_ci_checks` / `_is_pending_check` | `gates.py:117` / `gates.py:101` (see F5 for the shape problem) |
| `auto_fix.GhClient` / `RealGhClient` | `auto_fix.py:32` / `auto_fix.py:72` (see F1) |
| `ok: N job(s) valid` | `cli.py:660`, stdout; `warning:`/`error:` go to stderr; warnings do not change the exit code |
| `refine-issue --repo` defaults to `Path.cwd()` | `cli.py:333-336` |
| `_cmd_sync_repo` wraps `_fetch_and_fast_forward` | `cli.py:911` |
| systemd unit modelled on the digest unit | see F12 |

**One correction, non-blocking:** `is_currently_running` is not in `tick` as a definition
— it is defined in `history.py:166` and imported into `tick` (`tick.py:49`). v1's
sentence is about the *predicate*, not the module, so this is a wording fix only.

**One strengthening, non-blocking:** v1's reasoning for using `_open_pipeline_run` over
`is_currently_running` is right (stale-folded-in would let a genuinely open run be
treated as closed). It is worth stating *why the "newest running wins" scan is
acceptable here*: `_process_pipeline_job` refuses to launch a second pipeline run while
one is open, so "an earlier `running` record is open while the newest is not" is not a
reachable state, and the only true orphan is a stale run the watchdog and
`_pipeline_host_rebooted_mid_run` (`tick.py:1603`) already own. `ps.scan_pipeline_runs`
+ `PipelineRun.in_progress` (`ps.py:55`, `:45`) is the richer liveness signal — it keys
on "the final report does not exist yet" and so survives a lost or rotated
`history.jsonl` — and is the right answer to a *future* strengthening. Named in Risks,
not required here.

---

**F12 — `[non-blocking]` — the systemd unit modelling in v1 is accurate; two amendments.**
confidence: high

Verified line-for-line against `deploy/systemd/herdr-routines-digest.service` and
`.timer`: `Type=oneshot`; `WorkingDirectory=%h/projects/herdr-routines`;
`After=`/`Wants=herdr-server.service`; the identical
`Environment=PATH=%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`
with the same explanatory comment (a `--user` unit does not inherit the login PATH, and
`herdr.py` invokes the bare command name); `%h/.local/bin/uv run herdr-routines <cmd>`;
a finite `TimeoutStartSec`. The timer shape matches too: `OnCalendar=*-*-* 21:30:00`,
`Persistent=true`, `AccuracySec=5min` (the digest timer's value), `[Install]
WantedBy=timers.target`.

Amendments: (i) `TimeoutStartSec` is `1800`, not 600 — see F8. (ii) The digest and
watchdog **services carry no `[Install]` section** and neither should this one; only the
timer gets `[Install] WantedBy=timers.target`. (iii) `deploy/README.md:3` says "Two
systemd **user** units drive this tool" while listing four; this change makes six, so
that sentence is part of the docs edit, not just the `cp` block and the `enable --now`
list that v1 names.

---

**F13 — `[non-blocking]` — the lock protocol is correct as specified, with one boundary
worth naming explicitly.**
confidence: high

`tick_lock` is a non-blocking exclusive `flock` that yields a bool; v1's "did not acquire
→ log `deferred`, exit 0, no wait loop" is exactly right, and holding it across
fetch → CI → ff → validate → rollback is the right scope (a `validate` must never run
against a checkout a concurrent tick is mid-way through using).

**Fix (clarification only):** the lock excludes `tick` and nothing else.
`_cmd_digest` (`cli.py:965`) and `_cmd_pipeline_watchdog` (`cli.py:935`) take **no**
lock. See Risks R4.

---

**F14 — `[non-blocking]` — `cli.py`'s module docstring is already a partial list; add
`self-update` and do not try to fix the whole list.**
confidence: high

`cli.py:1` reads `tick | status | ps | scheduled | history | validate | run | gc` — 8
of 15 subparsers. `gate`, `pick-feature`, `gc`, `tmp-hygiene`, `sync-repo`,
`refine-issue`, `digest` and `pipeline-watchdog` are all missing from it. v1's "update
the module docstring's subcommand list" is fine; doing the other seven in this PR is
unrelated churn. Add `self-update` and move on.

---

## Approach

Add a `herdr-routines self-update` subcommand, driven by its own systemd timer
(`herdr-routines-update.{service,timer}`, once a day at 21:30, `Persistent=true`,
before the nightly jobs). New module `src/herdr_routines/self_update.py` holds the
logic; `cli.py` gets a thin handler, following the `sync-repo` / `pipeline-watchdog`
precedent (a small module + a `_cmd_*` in `cli.py`).

The process runs from the **currently installed (old) code** and exercises the **new**
code **out of process**, so a commit that doesn't even import is caught and rolled back
rather than wedging the host. Each step below is one small, separately-testable function
in `self_update.py`.

### Sequence

```
run_self_update(*, checkout, lock_path, history_path, config, gh, notify, state_path, runner=…)
  1.  tick_lock(lock_path)                     not acquired -> "deferred",           rc 0
  2.  pipeline_run_open(config, history_path)  open run     -> "deferred",           rc 0
  3.  repo_state(checkout)                     dirty | detached | not main -> notify, rc 1
  4.  resolve_uv()                             missing      -> notify,               rc 1
  5.  git fetch origin; new = rev-parse origin/main
        new == old                             -> "up to date",                     rc 0
        new == state.last_rejected_sha         -> "deferred (already rolled back)", rc 0
        commit_ci_state(gh, owner, repo, new)  not green    -> "deferred",           rc 0
  6.  baseline = validate_subprocess(checkout)          best-effort; failure => no baseline
  7.  _fetch_and_fast_forward(checkout, base)  RuntimeError (diverged) -> notify,     rc 1
  8.  head_after = rev-parse HEAD
        head_after == old                      -> "up to date" (+ warn if unpushed), rc 0
  9.  post = validate_subprocess(checkout)
        rc != 0 | timeout | unparseable | count < baseline
                                                -> git reset --keep old,
                                                   record state, notify,              rc 1
 10.  deploy_changes(old, head_after)         list in the notification, never applied
 11.  log "self-update: old..head_after (n commits)"; notify
```

Steps 5 and 8 both decide "up to date"; step 8 is the **authoritative** one because it
reports what actually happened on disk. Step 5's `new == old` is a cheap short-circuit
that avoids a pointless `gh` call when `origin/main` has not moved. See F3.

**1. `tick_lock`.** Reuse `tick.tick_lock` (`tick.py:85`) on
`tick.default_lock_path()` (`tick.py:74`) — the *same* exclusive `flock` on
`~/.local/state/herdr-routines/tick.lock` that `_cmd_tick` holds (`cli.py:416`). It is
already non-blocking (`LOCK_NB`), so "did not acquire" is immediate and needs no timeout
loop: log `self-update: deferred (tick lock held)` and return 0. The lock is held for
the **whole** fetch → CI → fast-forward → validate → (rollback) sequence, not just the
git step, so `validate` can never run against a checkout a concurrent tick is mid-way
through using. The lock excludes `tick` only — see R4.

**2. Defer while a pipeline run is open.** `pipeline_run_open(config, history_path)`
iterates `config.jobs` where `job.kind == "pipeline"` and calls
`tick._open_pipeline_run(history_path, job.name)` (`tick.py:1637`) for each. Deliberately
that function and **not** `is_currently_running` (`history.py:166`, re-exported into
`tick` at `tick.py:49`): `_open_pipeline_run` asks "is there a run in flight" without
folding in a staleness clock, which is exactly the question — and its own docstring says
why the two are separate predicates. Log `deferred`, rc 0.

This guard is not redundant with the lock. A `kind: pipeline` job's real work runs in a
**detached** `systemd-run --user` unit (`_build_pipeline_launch_argv`, `tick.py:1674`)
that holds no tick lock for the whole (hours-long) orchestrator run; the lock is released
as soon as `tick` returns. So the lock protects the launch/reconcile boundary and this
guard protects the in-flight run. Swapping the reconcile code mid-run is the one moment
worth avoiding. F11 records why the "newest `running` wins" scan is sufficient here, and
R5 names the richer alternative.

**3. Refuse a dirty / detached / non-`main` checkout.** `repo_state(checkout)` runs
`git rev-parse HEAD`, `git rev-parse --abbrev-ref HEAD` and `git status --porcelain` and
returns `(old_sha, branch, dirty)`. Non-`main` — **including a detached `HEAD`, which
`rev-parse --abbrev-ref HEAD` reports as the literal string `HEAD`** — or dirty →
**notify and exit 1, touching nothing**. The detached case is a separate check, not a
nicety: `_fetch_and_fast_forward` (`repos.py:107-116`) short-circuits on detached HEAD
and returns after fetching, having merged nothing, so without this the command would
report a successful "update" of a checkout it never moved — the same false-success class
as F3, reached by a different road. A hand-hotfixed host is a legitimate state (the
2026-09-27 incident was fixed by hand) and clobbering it is worse than staying stale. No
`reset --hard`, no force, anywhere in this command.

**4. `resolve_uv`.** `shutil.which("uv")`, else `Path.home() / ".local" / "bin" / "uv"`
if it exists. Neither → `refused` + notify + exit 1, **before any git operation**
(`pi-update-runbook.md:84`). See F10.

**5. Green-CI gate on the target SHA.** `git fetch --prune origin`, then
`new = git rev-parse origin/main`. `new == old` → log and exit 0 quietly, no
notification (an unchanged SHA is not news). `new == state.last_rejected_sha` → log
`deferred (already rolled back: <sha> — <reason>)` and exit 0 without touching the tree
(F2). Otherwise query the *commit's* check runs:
`gh api repos/{owner}/{repo}/commits/<new>/check-runs`, with `owner`/`repo` from
`gates.remote_owner_and_repo(checkout)` (`gates.py:82`, which already reads
`git remote get-url origin` and delegates to `auto_fix.repo_owner_and_name`,
`auto_fix.py:159` — verified to handle both `git@host:owner/repo.git` and
`https://host/owner/repo.git`).

A new `commit_check_runs(*, owner, repo, sha) -> list[dict[str, object]]` goes on the
`GhClient` protocol **and** `RealGhClient` (`auto_fix.py:32` / `auto_fix.py:72`), plus
**both test fakes** (F1). It returns `json.loads(stdout).get("check_runs", [])` and
raises `RuntimeError` on a non-zero `gh` exit.

Evaluation is `gates.evaluate_commit_checks` — a **new, deliberately stricter and
case-normalised** function sitting next to `gates.evaluate_ci_checks` (`gates.py:117`) so
the difference is reviewable side by side:

- any check still pending → defer. Pending is read **case-insensitively**
  (`status.strip().upper() != "COMPLETED"`), and the *legacy status* shape
  (`state == "PENDING"`) is accepted too, so the function is correct against both the
  REST check-runs payload and a GraphQL rollup. Do not feed the raw REST dict to
  `_is_pending_check` (F5).
- an **empty** check list → defer (`run_ci_gate` treats "no checks" as pass, which is
  right for a PR whose CI hasn't been *created* yet and wrong for "we could not confirm
  this commit is green"). Note REST check-runs omit legacy commit statuses, so a
  statuses-only repo lands here — correctly, fail-closed.
- a conclusion outside `{SUCCESS, SKIPPED, NEUTRAL}` → defer. This is the meaningful
  divergence: `evaluate_ci_checks` fails only on `FAILURE` and tolerates everything else,
  but for a commit we are about to *deploy* a `CANCELLED` / `TIMED_OUT` /
  `ACTION_REQUIRED` / `STALE` runner is not green, and an unrecognised conclusion string
  is not evidence of anything.
- `gh` raising (`RuntimeError` from `RealGhClient._run`, expired auth, no network) →
  defer.

Every deferral logs a `self-update: deferred (<reason>)` line at `warning` and exits
**0**. Fail-closed: an unverifiable commit is not updated to. The human merge to `main`
is already the real gate; this only makes sure the host never runs something red.

**6. Out-of-process `validate`, and rollback.** `validate_subprocess(checkout, uv)` runs
`[uv, "run", "--frozen", "herdr-routines", "validate"]` with `cwd=checkout` and an
explicit `timeout=`, so the *new* code, the *new* import graph, the *new* config schema
and the *new* `uv.lock` are all exercised against the host's live `jobs.d/`. This is the
whole point of validating out of process: the parent already has the old code imported,
so an import error in the new code is data, not a crash. `--frozen` still syncs `.venv`
from the new lock, so the new lock really is exercised, while making it impossible for
`uv` to rewrite a tracked `uv.lock` mid-sequence (F6).

Two notes on the invocation. `validate`'s `--systemd-unit` defaults to the **relative**
`deploy/systemd/herdr-routines.service` (`cli.py:155-156`), which resolves against
`cwd=checkout` — so the check reads the *new* unit file, not the host's installed one.
That is correct and mildly desirable (a new commit that lowers `TimeoutStartSec` below
the live jobs' worst case fails validation and rolls back); just do not pass an absolute
path to the host's unit, which would defeat it. And the pass line is the existing
`ok: N job(s) valid` on **stdout** (`cli.py:660`); warnings go to stderr and do not
change the exit code, so a run full of pre-existing `$ROUTINE_REPORT` warnings still
parses.

A **baseline** count is captured by the same subprocess call *before* the
fast-forward. If that pre-update call fails, log a warning and continue with **no**
baseline — the count-drop check is then skipped and only the exit-code check applies
(exit 0 here, not a permanent block: the host was already broken and staying frozen would
help nobody).

On failure — non-zero exit, `TimeoutExpired`, unparseable output, or the new code
reporting **fewer** jobs than the baseline — roll back:

```
git -C <checkout> reset --keep <old>
```

`--keep` (never `--hard`) rewinds HEAD, index and worktree, and *refuses* rather than
discarding if anything would be overwritten. Verified in both directions: over a clean
ff it rewinds cleanly and removes files the ff added; with a local edit to a file
differing across `old..new` it exits 128 and leaves HEAD on `new`. Then record the
rejected SHA in the state file (F2), re-read `git rev-parse HEAD` and
`git status --porcelain`, notify with the validate stdout/stderr **and the actual HEAD
SHA it is stuck on**, and exit 1. A stuck-on-`new` checkout is loud and manual, which is
the correct outcome for a host we could not prove healthy.

**Comparison is on counts, not name sets** — `validate` emits no job names on any flag
(F4). Issue 053's acceptance criterion 8 is the count check and that is what ships.

**7. `deploy/` changes: report, never apply.** `deploy_changes(checkout, old, new)` runs
`git diff --name-only <old>..<new> -- deploy/`. Non-empty → include the file list in the
success notification. Systemd units, `opencode.pipeline.json` and the example
`jobs.d/` are **host config** with their own install steps (`deploy/README.md`); applying
them from a timer is out of scope and is the one thing here that could take the scheduler
down.

**8. Success logging.** `log.info("self-update: %s..%s (%d commits)", old, new, n)` with
`n` from `git rev-list --count old..new` and `new == head_after`. Notify only when the
SHA actually moved (steps 5 and 8 both returned earlier otherwise). Every notification
and log line carries the absolute path acted on (see R6).

### Why a separate timer, not a step inside `tick`

- The code changes at one predictable time, before the night's runs, rather than between
  a pipeline launch and its reconcile.
- A failing update cannot block a tick: `tick_lock` is non-blocking both ways, so ticks
  keep running the old code and the update simply retries tomorrow.
- `digest` and `pipeline-watchdog` share the same checkout; updating from inside any one
  of them would be arbitrary.

### Why no restart

Each tick is a fresh `uv run herdr-routines tick` process, so the next tick picks up the
new code with no `daemon-reload`; `uv run` re-syncs `.venv` when `uv.lock` changed
(`docs/process/pi-update-runbook.md` lines 16–18 already documents this for the manual
path).

### CLI surface

`cli.py::_build_parser`, next to `sync-repo` (a pure-git, Herdr-free command):

- `self-update [--path PATH] [--base main] [--dry-run]`

`--path` defaults to `Path.cwd()` — same convention as `refine-issue`'s `--repo`
(`cli.py:333-336`) — and the unit supplies `WorkingDirectory=%h/projects/herdr-routines`,
exactly as the other three units do. Config is resolved as `args.config or
default_config_path()` (F7), with `ConfigError` and `config.errors` both mapped to
`refused` + notify + exit 1. Notifications reuse `tick._notify` (`tick.py:2030`), which
is best-effort by contract. `--dry-run` is pinned per F9.

`_cmd_self_update` builds `HerdrClient()` for notifications (as `_cmd_digest` does) and
`RealGhClient()` for the CI query, wraps everything in `tick_lock`, and maps
`SelfUpdateResult.status` to an exit code.

Exit codes: `up_to_date` / `updated` / `deferred` → **0**; `refused` (dirty, detached,
non-main, diverged, `uv` missing) and `rolled_back` → **1**.

### systemd

`deploy/systemd/herdr-routines-update.service`, modelled line-for-line on
`herdr-routines-digest.service` (F12): `Type=oneshot`,
`WorkingDirectory=%h/projects/herdr-routines`, `After=/Wants=herdr-server.service`, the
same explicit `Environment=PATH=%h/.local/bin:...` (a user unit does not inherit the
login PATH — the comment in the digest unit explains why),
`ExecStart=%h/.local/bin/uv run herdr-routines self-update`, no `[Install]`, and a
**finite** `TimeoutStartSec=1800` (F8: never `infinity`, per `docs/plan-v1.md` §3, and
large enough that systemd cannot land a SIGTERM inside the `git merge` window).

`deploy/systemd/herdr-routines-update.timer`: `OnCalendar=*-*-* 21:30:00`,
`Persistent=true`, `AccuracySec=5min` — before the nightly jobs, and the same
`AccuracySec` the digest timer uses — with `[Install] WantedBy=timers.target`.

### Docs

- `deploy/README.md` — add the unit pair to the `cp` block and the
  `systemctl --user enable --now` list, with a paragraph in the style of the
  watchdog/digest ones explaining why it is a standalone unit (it dispatches no job and
  spawns no agent), and fix the "Two systemd user units" sentence at line 3 (F12).
- `docs/process/pi-update-runbook.md` — "When to run" points at the timer once it ships.
  **The runbook's closing "Notes / gotchas" bullet must change too**: lines 92–94
  currently state the manual step "is the deliberate design: the release/update strategy
  on `ROADMAP.md` Parking lot carves out the runner fast-forward as a human/`update`
  action, keeping self-update off the always-on Pi." Issue 053 reverses exactly that
  decision, so leaving the bullet would leave the repo contradicting itself. The runbook
  stays as the manual fallback and as the home for config-migration notes.

## Acceptance criteria

Derived from issue 053's own acceptance criteria 1–9 (test names 1–8 verbatim from the
issue) plus the items this review adds. Every item is checkable by the named test.

1. `self-update` on a checkout behind `origin/main` fast-forwards it, runs `validate` as
   an out-of-process subprocess, and exits 0; the log/notification shows `old..new` with a
   commit count, and the checkout's real `HEAD` is the new SHA afterwards.
   — [blocking] confidence: high
   Test: test_self_update_fast_forwards_and_validates

2. When the post-update `validate` subprocess exits non-zero (including an import error
   in the new code, simulated by a `validate` that exits 1), the checkout is back at
   `old` and a notification carrying the validate output is sent. No `reset --hard` and
   no force appear anywhere in the argv the rollback builds.
   — [blocking] confidence: high
   Test: test_self_update_rolls_back_on_validate_failure

3. With an open `kind: pipeline` run, `self-update` makes no git change, exits 0, and
   logs a `deferred` line. The fixture is a `running` history record for a
   `kind: pipeline` job with no terminal record for that `run_id`.
   — [blocking] confidence: high
   Test: test_self_update_defers_while_pipeline_run_open

4. A dirty working tree, a non-`main` branch, a detached `HEAD`, or a diverged history
   each leave the checkout untouched (`HEAD` unchanged, `status --porcelain` unchanged),
   notify, and exit non-zero. The diverged case is exercised for real: the checkout
   carries an unpushed commit and the bare repo has moved, so
   `git merge --ff-only origin/main` fails in the sandbox. The detached case is
   exercised because `_fetch_and_fast_forward` short-circuits on detached HEAD and would
   otherwise produce a false "updated".
   — [blocking] confidence: high
   Test: test_self_update_refuses_dirty_or_diverged

5. `self-update` holds `tick_lock` for the whole fetch → CI → fast-forward → validate →
   (rollback) sequence, and when the lock is already held it is a no-op exit 0 with no
   git operation. The test takes a real `flock` on a temp lock path and asserts the
   checkout did not move.
   — [blocking] confidence: high
   Test: test_self_update_respects_tick_lock

6. Changed `deploy/` paths are reported in the notification and not applied: after a run
   that fast-forwarded past a commit touching `deploy/systemd/…`, the file appears in the
   notification body and the on-disk unit file is unchanged.
   — [blocking] confidence: high
   Test: test_self_update_reports_deploy_changes

7. When `origin/main`'s check runs are failing, pending or unqueryable, no git change is
   made and the exit code is 0 with a `deferred` log line. All three are exercised: a
   `FAILURE` conclusion, a non-`COMPLETED` status, and a `gh` client that raises.
   — [blocking] confidence: high
   Test: test_self_update_defers_on_non_green_ci

8. A post-update `validate` that exits 0 but reports **fewer** jobs than the pre-update
   baseline is treated as a failure and rolled back. The comparison is on the count
   parsed from `ok: N job(s) valid` (stdout) — `validate` emits no job names on any flag
   (F4), so no name-set comparison is attempted.
   — [blocking] confidence: high
   Test: test_self_update_rolls_back_on_job_count_drop

9. `deploy/systemd/herdr-routines-update.{service,timer}` exist and are modelled on the
   digest unit: `Type=oneshot`, the same `WorkingDirectory=`, `After=`/`Wants=herdr-server.service`,
   an `Environment=PATH=` line, `ExecStart=` ending in `herdr-routines self-update`, a
   **finite** `TimeoutStartSec` with no `infinity` anywhere in the file, `Persistent=true`
   and `OnCalendar=*-*-* 21:30:00` on the timer, and no `[Install]` on the service.
   `deploy/README.md` documents enabling the pair, and
   `docs/process/pi-update-runbook.md`'s "When to run" points at the timer.
   — [blocking] confidence: high
   Test: test_self_update_units_and_docs_are_shipped

10. The new command runs entirely on the **already-imported old code**: the validate
    subprocess is a real `subprocess.run` (fakeable through the injected `runner=`), and
    no module from the new checkout is imported after the fast-forward. The test asserts
    the validate argv is `[uv, "run", "--frozen", "herdr-routines", "validate"]` with
    `cwd` set to the checkout, and that `--frozen` is present so `uv` cannot rewrite the
    tracked `uv.lock` (F6).
    — [blocking] confidence: high
   Test: test_self_update_validates_out_of_process_with_frozen_uv

11. `evaluate_commit_checks` is case-normalised and correct for **both** payload shapes:
    the lowercase REST `check-runs` shape (`status: "completed"`,
    `conclusion: "success"`) and the uppercase GraphQL rollup shape
    (`status: "COMPLETED"`, `conclusion: "SUCCESS"`). Without this, every real REST
    check is classified pending and the feature defers forever (F5).
    — [blocking] confidence: high
   Test: test_evaluate_commit_checks_accepts_rest_and_graphql_shapes

12. `evaluate_commit_checks` defers on an empty list, on any pending check, and on a
    conclusion outside `{SUCCESS, SKIPPED, NEUTRAL}` — specifically `CANCELLED`,
    `TIMED_OUT`, `ACTION_REQUIRED` and an unrecognised string — while
    `evaluate_ci_checks` keeps its existing FAILURE-only semantics unchanged.
    — [blocking] confidence: high
   Test: test_evaluate_commit_checks_defers_on_empty_pending_and_bad_conclusions

13. `GhClient.commit_check_runs` exists on the protocol, on `RealGhClient`, and on **both**
    test fakes (`tests/test_gates.py::FakeGhClient`,
    `tests/test_auto_fix.py::FakeGhClient`), so `uv run mypy` — a required CI job that
    checks `tests/` — stays green. Verified in a sandbox copy: without the fakes the
    protocol change produces 19 mypy errors in 2 files (F1).
    — [blocking] confidence: high
   Test: test_gh_client_fakes_implement_commit_check_runs

14. When the fast-forward cannot move `HEAD` (the checkout carries unpushed local
    commits and `origin/main` has not moved), the result is `up_to_date` with **no**
    notification — not a success report — and a `warning`-level log line naming the
    checkout path and the unpushed-commit count. A clean `HEAD == old` case is the
    control and must also report `up_to_date` quietly.
    — [blocking] confidence: high
   Test: test_self_update_reports_up_to_date_when_ff_is_a_noop

15. After a rollback, the rejected SHA is persisted to
    `$HERDR_PLUGIN_STATE_DIR/self-update.json` (else
    `~/.local/state/herdr-routines/self-update.json`) and a **second** run while
    `origin/main` is still that SHA is a no-op: `deferred (already rolled back: …)`,
    exit 0, no ff, no validate call, no second notification. The state is keyed on the
    SHA, so a subsequent `main` move clears it (F2).
    — [blocking] confidence: high
   Test: test_self_update_does_not_retry_a_rejected_sha

16. If `uv` cannot be resolved (neither on `PATH` nor at `~/.local/bin/uv`), the command
    notifies and exits 1 having performed **no** git operation — no fetch, no ff, no
    rollback (F10).
    — [non-blocking] confidence: high
   Test: test_self_update_refuses_when_uv_is_missing

17. A `validate` subprocess that exceeds its `timeout=` raises `TimeoutExpired` and is
    treated as a validate **failure** (rollback + notify + exit 1), not as a pass (F8).
    — [non-blocking] confidence: high
   Test: test_self_update_treats_validate_timeout_as_failure

18. When `git reset --keep` itself refuses (a local edit to a file differing across
    `old..new`), the notification carries the SHA the checkout is actually stuck on, read
    back from `git rev-parse HEAD`, and the command still exits 1. This is the one
    reachable path where the checkout is **not** back at `old` (verified: `reset` exits
    128, `fatal: Could not reset index file…`), so it must never be silent.
    — [non-blocking] confidence: high
   Test: test_self_update_reports_head_sha_when_rollback_fails

19. `--dry-run` performs steps 1–5, prints the intended `old..new` and deploy diff, and
    makes **no** git change: `HEAD` and `status --porcelain` are identical before and
    after, and the `validate` subprocess is never invoked. Under a held `tick_lock` it
    reports `deferred` (F9).
    — [non-blocking] confidence: high
   Test: test_self_update_dry_run_makes_no_git_change

20. `self-update` honours the top-level `--config` override (`args.config or
    default_config_path()`, F7), and a `ConfigError` **or** a non-empty `config.errors`
    from the `jobs.d/` loader is a `refused` result with a notification and exit 1 —
    not a silent pass, and not a bare `SystemExit` with no notification.
    — [non-blocking] confidence: high
   Test: test_self_update_honors_config_override_and_reports_config_errors

21. Git state is exercised for real, not mocked: a temp bare repo plus a clone, following
    `tests/test_sync_repo.py::_init_bare_git_repo` (already proven in this suite). The
    *lock*, the *branch state*, the *fast-forward*, the *rollback*, the *deploy diff* and
    the *divergence refusal* are real; only `validate`, the CI query and the
    notification are faked behind their seams.
    — [non-blocking] confidence: high
   Test: test_self_update_git_operations_are_real_not_mocked

## Files touched

- `src/herdr_routines/self_update.py` — **new**. `SelfUpdateResult` dataclass
  (`status: up_to_date|updated|deferred|refused|rolled_back`, `old`, `new`, `reason`,
  `deploy_changes`, `validate_output`) plus `run_self_update` and the small helpers
  `repo_state`, `pipeline_run_open`, `commit_ci_state`, `evaluate_commit_checks`'s
  sibling input normaliser, `resolve_uv`, `validate_subprocess`, `deploy_changes`,
  `default_state_path`. Every impure call goes through an injectable `runner=`
  (defaulting to `subprocess.run`, matching `gates`' `runner=` convention) and an
  injectable `gh`/`notify`, so the whole flow is testable without a real host.
- `src/herdr_routines/auto_fix.py` — add `commit_check_runs(*, owner, repo, sha) ->
  list[dict[str, object]]` to the `GhClient` protocol and to `RealGhClient`
  (`gh api repos/{owner}/{repo}/commits/<sha>/check-runs`, returning
  `json.loads(stdout).get("check_runs", [])`, raising `RuntimeError` on non-zero exit).
  No behavior change to existing methods.
- `tests/test_gates.py` — add `commit_check_runs` to `FakeGhClient` (**required**, F1) and
  unit tests for `evaluate_commit_checks` (empty, pending, `CANCELLED`, `TIMED_OUT`,
  mixed, and both the REST-lowercase and GraphQL-uppercase shapes).
- `tests/test_auto_fix.py` — add `commit_check_runs` to `FakeGhClient` (**required**,
  F1).
- `src/herdr_routines/gates.py` — add `evaluate_commit_checks(checks) -> GateVerdict`
  beside `evaluate_ci_checks` (`gates.py:117`): case-normalised, pending / empty /
  non-`{SUCCESS, SKIPPED, NEUTRAL}` all defer. `evaluate_ci_checks` stays untouched and
  keeps its FAILURE-only semantics for PRs; the divergence is deliberate and documented
  in both docstrings.
- `src/herdr_routines/cli.py` — register the `self-update` subparser in
  `_build_parser` and add `_cmd_self_update`; add `self-update` to the module docstring's
  subcommand list on line 1 (F14). `_cmd_validate` is **not** modified (F4).
- `deploy/systemd/herdr-routines-update.service` — **new**.
- `deploy/systemd/herdr-routines-update.timer` — **new**.
- `deploy/README.md` — install + enable instructions, rationale paragraph, and the
  "Two systemd user units" sentence on line 3.
- `docs/process/pi-update-runbook.md` — "When to run" → the timer; amend the
  "manual step is the deliberate design" note.
- `tests/test_self_update.py` — **new**. The tests named in `## Acceptance criteria`
  above (the eight from issue 053 verbatim, plus the additions), built on a real temp bare
  repo + clone per `tests/test_sync_repo.py::_init_bare_git_repo`, with `validate`, the
  `gh` client and the notification faked behind their seams.
- `docs/pipeline/runs/20260929T050000Z/spec.md` — this file.

Not touched: `tick.py` (its `tick_lock` / `_open_pipeline_run` are imported, not
modified), `config.py`, `history.py`, `runner.py`. The issue file's `status:` flips in
the implementing PR, per `docs/process/pi-update-runbook.md`.

## Risks

R1. **`git reset --keep` can leave residue.** — [non-blocking] confidence: high
The `--keep` half is verified sound in both directions (see F6); the residue risk was
`uv run` rewriting the tracked `uv.lock` between the ff and the rollback, which would
make `--keep` either refuse or leave a modified lock. `--frozen` removes that. The
remaining reachable case is a genuine local edit, which `--keep` refuses — the test is
criterion 18, and the mitigation is to report the HEAD SHA the checkout is stuck on
rather than try anything stronger. Never `--hard`. The stale `.venv` self-heals: the
next `uv run` re-syncs from whatever the checkout contains.

R2. **The venv mutates under a running process.** — [non-blocking] confidence: high
The parent holds old code in memory while the child re-syncs `.venv` in place. Safe by
construction (Python does not re-read imported modules), but it constrains the
implementation: after the fast-forward, `run_self_update` must **not** import or lazily
load any module whose on-disk version has changed — which includes anything that was
imported lazily before. Everything needed afterwards is computed before the ff, or comes
from the subprocess output. Worth a test.

R3. **A permanently-deferring host looks green.** — [non-blocking] confidence: high
`gh` unauthenticated, offline, or a commit with no checks all exit **0** with a
`warning`-level log line, so systemd shows the unit as succeeded and nothing notifies. A
host that never self-updates for a month looks exactly like a host that self-updates
every day. Mitigations: the `deferred (<reason>)` line is written daily to
`herdr-routines.log` (`cli.py:73` `default_log_path`, verified) so it is greppable, and
the manual runbook remains the fallback. **Residual risk, named rather than hidden** — a
natural follow-up is surfacing "runner not updated in N days" in the digest, which is
out of scope here.

R4. **`tick.lock` does not exclude the other three units.** — [non-blocking]
confidence: high
`_cmd_digest` (`cli.py:965`) and `_cmd_pipeline_watchdog` (`cli.py:935`) take no lock,
and `herdr-routines-watchdog.timer` fires at `*:0/15` with `AccuracySec=30s`
(i.e. :00/:15/:30/:45) while the update timer fires anywhere in 21:30–21:35 — so a
watchdog sweep at 21:30 is *likely* to overlap the fast-forward. The package is
installed **editable** into `.venv` (`.venv/.../_editable_impl_herdr_routines.pth`,
verified), so imports read `src/` live and a process starting during the ff can see a
half-updated tree. Accepted: the window is the sub-second `git merge` checkout, the only
damage is a crashed sweep that the next 15-minute fire repeats, and making those two
commands lock-aware is a behavior change to commands this issue does not cover. A
follow-up could wrap both handlers in `tick_lock` and exit 0 when not acquired.

R5. **"Open pipeline run" keys on history, not on liveness.** — [non-blocking]
confidence: medium
Step 2's predicate is `_open_pipeline_run`, which reads `history.jsonl` and ignores
staleness by design — the right choice (a stale-folded-in check would let a genuinely
open run read as closed). Its scan keeps the *newest* `running` record per job, which is
sufficient because `_process_pipeline_job` refuses to launch a second pipeline run while
one is open, so "an older `running` is open while the newest is not" is not reachable.
A stronger predicate exists and is deliberately deferred: `ps.scan_pipeline_runs`
(`ps.py:55`) with `PipelineRun.in_progress` (`ps.py:45`) keys on "the final
`pipeline-<run_id>.md` does not exist yet", so it survives a lost or rotated
`history.jsonl` — a real gap, since the incident that motivated this issue is precisely
a state the history file may not describe. Deferred, named, and cheap to adopt later.

R6. **Job-count drop is a heuristic with false positives.** — [non-blocking]
confidence: high
A legitimate future change could make an old job entry invalid-by-design, and we would
roll back nightly. This is the intended trade (the runbook's failure mode — a silently
dropped job, reported `ok` — is exactly the class of plausible-but-wrong unattended
failure this project keeps designing against), and rolling back is the safe direction.
F2 bounds the cost: one rollback and one notification per bad commit, not per night. The
**masked** case — one job dropped, one added in the same commit, count unchanged — is a
known residual with no cheap fix: `validate` emits no job names (F4). The mechanism for a
follow-up is a second subprocess in the same cwd, `herdr-routines scheduled --json`
(`cli.py:484`), parsed for its per-job `{"name": …}`; the cleaner long-term answer is a
`validate --json` once every checkout has it.

R7. **Lock contention is one-sided but complete.** — [non-blocking] confidence: high
`tick_lock` is non-blocking, so `self-update` never delays a tick and a tick never delays
`self-update` — it just skips the day. A tick that runs 8 hours (long gated job) means
the daily window is often missed; that is a correct trade (updating under a live tick is
the thing to avoid) and costs at most one day's staleness.

R8. **The two-checkouts confusion is now in code.** — [non-blocking] confidence: high
`--path` defaults to `Path.cwd()`, so running `self-update` from the `repos/` checkout
would update the wrong tree. Mitigation: every notification and log line carries the
absolute path acted on, and the unit pins `WorkingDirectory=`. Worth a line in
`deploy/README.md` too.

R9. **`deploy/` drift is reported, never applied.** — [non-blocking] confidence: high
A `deploy/` change to a unit that has since been hand-edited on the host will keep being
listed after it is first applied, since the diff is always `old..new` and never a "host
already has this" comparison. Acceptable — the list is advisory and re-listing a
known-applied file is a mild annoyance, not a defect. Auto-detecting already-applied host
config is a non-goal.

R10. **Non-goals, restated so they do not creep in.** — [non-blocking] confidence: high
No automatic `deploy/` application; no pinned deploy branch/tag (the host follows
`main`; CI + the human merge is the gate); no logic-regression detection (`validate`
proves "loads and accepts the live config", not "behaves correctly"); no updating the
`repos/` job checkout (`ensure_repo` already does that); and no `validate --json` /
`scheduled --json` name-set work (F4/R6, deferred by name).

## Changelog v1->v2

### Blocking fixes (the design changed)

- **F1** — removed the claim that adding `commit_check_runs` to the `GhClient` protocol
  needs no test-fake edits, and proved it wrong: `typecheck-python` (`uv run mypy`,
  `files = ["src", "tests"]`) is a required CI job, and a sandbox run of v1's change
  produces **19 mypy errors in 2 files** (`tests/test_auto_fix.py`,
  `tests/test_gates.py`). Both fakes are now in Files touched, with a dedicated
  acceptance item. Without this the commit shipping this feature is red, and this
  feature's own CI gate would then refuse to install it. confidence: high
- **F2** — added the rejected-SHA state file
  (`$HERDR_PLUGIN_STATE_DIR/self-update.json`). v1 re-attempted a known-bad `new` every
  night forever, re-notifying identically each time. confidence: high
- **F3** — added the authoritative **post-ff `HEAD` re-read**. v1 compared
  `rev-parse origin/main` against `HEAD` *before* the ff, so a checkout with unpushed
  local commits and an unmoved `origin/main` produced
  `self-update: old..new (0 commits)` **plus a success notification** having changed
  nothing (verified in a git sandbox: `merge --ff-only` → "Already up to date",
  `MOVED=NO`). `up_to_date` is now decided by what is on disk after the ff, and an
  unpushed-commit warning is logged. `old`/`new` in the deploy diff and commit count are
  now `HEAD_after`. confidence: high
- **F4** — dropped the job **name-set** comparison. `validate` prints only
  `ok: N job(s) valid` (`cli.py:660`) and has no flag that emits names, so v1's
  mechanism does not exist; the count comparison v2 keeps is what issue 053's AC-8
  actually specifies. The masked-drop residual and the two candidate mechanisms
  (`scheduled --json`, a future `validate --json`) are recorded in R6.
  confidence: high
- **F5** — fixed the CI-gate payload mismatch. v1 fed the **REST** `check-runs` response
  into `gates._is_pending_check`, which tests `status != "COMPLETED"` (GraphQL casing,
  matching the uppercase constants at `gates.py:46-47`), so every real check reads as
  pending and the command defers forever — silently. `evaluate_commit_checks` now
  normalises case itself and is correct for both shapes; `commit_check_runs` unwraps the
  `check_runs` key. confidence: high

### Non-blocking corrections and clarifications

- **F6** — `git reset --keep` verified sound in both directions (clean rewind removes
  ff-added files; a local edit makes it exit 128 and leave `HEAD` on `new`). The
  `uv.lock`-rewrite hazard is removed rather than mitigated: both validate calls use
  `uv run --frozen` (flag confirmed on uv 0.12.5), which still exercises the new lock
  via `.venv` but cannot mutate a tracked file. confidence: high
- **F7** — config is now `args.config or default_config_path()` (honouring the
  top-level `--config`), with both `ConfigError` and `config.errors` mapped to
  `refused` + notify + exit 1 rather than a bare `SystemExit`. confidence: high
- **F8** — `TimeoutStartSec` raised 600 → **1800** with an explicit per-call budget;
  `validate_subprocess` now passes an explicit `timeout=` and treats `TimeoutExpired` as
  a validate failure, so systemd cannot land a SIGTERM inside the `git merge` window.
  confidence: high
- **F9** — `--dry-run` pinned: steps 1–5 including the fetch and CI query, no ff, no
  validate, no notification; `deferred` under a held lock is expected. confidence: high
- **F10** — `uv` resolution moved to step 4, before any git operation, so a missing `uv`
  is a `refused` and not an ff-then-useless-rollback (the runbook's own gotcha:
  "`uv` is not on PATH in non-interactive ssh"). confidence: high
- **F12** — systemd modelling verified line-for-line against the digest unit; added the
  "no `[Install]` on the service" requirement, the 1800 s value, and the
  `deploy/README.md:3` "Two systemd user units" sentence to the docs edit.
  confidence: high
- **F13** — recorded the lock's boundary: `tick.lock` excludes `tick` and nothing else;
  promoted to R4 with the editable-install evidence. confidence: high
- **F14** — `cli.py:1`'s subcommand list is already partial (8 of 15); add
  `self-update` only, do not fix the other seven in this PR. confidence: high
- **F11** — every remaining symbol and line citation in v1 was checked against the files
  and is **accurate** (`tick.py:74/85/1614/1637/1674/2030`, `cli.py:398/416/660/911`,
  `repos.py:42/93`, `gates.py:82/101/117`, `auto_fix.py:32/72/159`), so the approach
  around them is unchanged. Two wording fixes: `is_currently_running` lives in
  `history.py:166` and is imported into `tick` at `tick.py:49`; and the reason
  `_open_pipeline_run`'s newest-`running`-wins scan is sufficient is now stated (and its
  stronger alternative, `ps.scan_pipeline_runs`, is named in R5). confidence: high

### New sections and structure

- Added `## Acceptance criteria`: 21 numbered items, each ending in a `Test: <name>`
  line naming a real test function, each tagged `[blocking]`/`[non-blocking]` with a
  `confidence:` value. Items 1–8 are issue 053's own criteria with its verbatim test
  names; item 9 extends its AC-9 (units + docs) into a checkable test; items 10–21
  cover the F1–F6, F8–F10 fixes. confidence: high
- Added `## Review findings` with F1–F14, each tiered and confidence-rated, so the
  v1→v2 delta is auditable without diffing the whole document. confidence: high
- Added a tier legend up front so `blocking` / `non-blocking` mean one thing everywhere
  in the file. confidence: high
- Added R4, R5 and R6 and split the old Risks 4/5/6/7/8 into R6–R10; every risk now
  carries a tier and a `confidence:` value. confidence: high
- `## Files touched` now lists `tests/test_gates.py` and `tests/test_auto_fix.py`, and
  states explicitly that `_cmd_validate` is **not** modified. confidence: high

### Unchanged

The core design is v1's and it survives review: out-of-process validate, `tick_lock`
across the whole sequence, defer on an open pipeline run, refuse dirty/non-`main`,
green-CI gate before deploying, report `deploy/` and never apply it, a standalone timer
rather than a `tick` step, and no restart. Every symbol it names is real and every
mechanism it relies on was verified to work; the five blocking findings are gaps in the
edges (a red CI gate, an unremembered rejection, a false success, an unimplementable
comparison, a payload-shape mismatch), not a wrong architecture. confidence: high
