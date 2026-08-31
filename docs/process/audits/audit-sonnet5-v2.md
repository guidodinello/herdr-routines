# Independent design review (pass 2) — Issue 026, revised spec (commit 11ab2f5)

First pass verdict: **SHAKY**, 5 required changes. This pass checks each one against the
revised spec and the actual code.

**Second verdict: MOSTLY SOUND.** The core redesign (detached dispatch instead of a
blocking 7 h tick) is correct and would work against `tick.py` / `runner.py` as they stand.
All five structural problems are genuinely addressed, not just name-checked. What remains is
a tier of concrete-but-bounded gaps that should be closed in spec refinement before
implementation — none of them require rethinking the approach. They are listed per-item and
collected at the end.

---

## A) "Kill the synchronous model" → dispatch detached, reconcile later

**Adopted, and coherent.** The mechanism (spec §"First increment", steps 1–3) matches how
the code already works: `run_tick` loops jobs calling `_process_job` (`tick.py:113-119`); a
`_process_pipeline_job` that fires `systemd-run` and returns in milliseconds leaves the rest
of the loop free, so AC #3 (concurrency) is actually satisfiable now — it was not under v1.
Reusing `_live_agent_exists` (`tick.py:1194`, checks `job.agent_name` = `rt-nightly-pipeline`)
and `is_currently_running` / `find_stale_running` for the "still running / went stale"
guards is the right instinct — those are exactly the primitives `_process_gated_job` leans
on (`tick.py:131-163`).

### Residual A1 — record-then-launch ordering is backwards

Step 1 writes the `running` record, step 2 launches the unit. If `tick` dies (or
`systemd-run` fails) between them, the next tick sees a `running` record, `_live_agent_exists`
returns `False` (no agent was ever started), and `is_currently_running`
(`tick.py:150`/`1054`, bounded by the staleness timeout) keeps the job wedged until the
deadline elapses — a whole pipeline night lost to a launch hiccup, recorded as
`interrupted_unknown` only ~7 h later via `find_stale_running`. `systemd-run` returns
essentially synchronously once the transient unit is registered, so **launch first, then
write the `running` record** (or write `running` only after `systemd-run` exits 0). Cheap fix,
should be in the spec.

### Residual A2 — the RUN_ID contract is unspecified, and the naive choice overflows the agent-name cap

The spec pins `$PIPELINE_REPORT` to `default_reports_dir()/f"{run_id}.md"` but never says
what `run_id` the orchestrator receives. `tick` mints run ids via
`make_run_id(job.name, occ)` = `nightly-pipeline-20260830T020000Z` (`runner.py:258-259`).
The orchestrator uses its `RUN_ID` to build the branch `auto/pipeline-$RUN_ID`, the spec
path `docs/pipeline/runs/$RUN_ID/spec.md`, **and the worker agent names
`pl-<stage>-<RUN_ID>`** (`orchestrator-prompt.md` Worker spawn template; `design.md:215`).
Herdr caps agent names at 32 chars (`[a-z][a-z0-9_-]{0,31}`, cited `design.md:215`,
mirrored in `config.py:58-60`). `pl-1-` (5) + `nightly-pipeline-20260830T020000Z` (32) = 37
— **overflow by 5**, every worker spawn fails at stage 1.

Fix: `tick` must pass the **bare UTC timestamp** (`20260830T020000Z`, 16 chars → `pl-1-…` =
21, fits) as the orchestrator's `RUN_ID`, and use `make_run_id(job.name, occ)` only for its
own history keying. The report path it pins must then be passed as an **absolute path** in
the generated invocation (not reconstructed from either run_id on the orchestrator side), so
the two namespaces never need to agree. The spec should state this explicitly — it's the
one place tick/unit/orchestrator must be wired consistently and right now it's a gap.

### Residual A3 — completion reconciliation reads `state.json`, whose location is not pinned

Step 3 says a later tick "reconciles completion from `state.json` (`current_stage`,
terminal) + the report file." But the orchestrator writes `state.json` to `$WT/state.json`
— its **own** worktree (`orchestrator-prompt.md` Prerequisite 2), not under
`default_reports_dir()`. `ps.py` only finds pipeline state by `rglob("state.json")` under
`default_scan_dirs()` = `[reports, reports.parent]` (`ps.py:56-73`), so today it relies on
the orchestrator *also* dropping a copy there (or on `artifact_paths.report`,
`ps.py:_resolve_report_path`). `tick`'s reconciliation inherits that ambiguity.

Two clean options, pick one in the spec: (a) pin `STATE_JSON` in the generated invocation
the same way `$PIPELINE_REPORT` is pinned, to a path under `default_reports_dir()`; or (b)
reconcile from the **report file only** — its existence = terminal; parse a status line /
`## Outcome` heading from its content for done-vs-failed-vs-partial. Option (b) is simpler
and avoids depending on the orchestrator's worktree still existing at recontile time
(a human may have `gc`'d it). AC #4 already implies the report carries partial-vs-final
state, so (b) is nearly free.

---

## B) "Sibling code path + `kind:` enum as SSOT"

**Sibling path: adopted cleanly.** `_process_pipeline_job` alongside `_process_gated_job`,
branched in `_process_job` — this mirrors `tick.py:1032` exactly and is the right structure.

**The enum: adopted but self-contradictory as written.** Spec §"Mode discriminator":

> - `kind: routine` (default) — plain 1-agent job, **has `checks` optionally**.
> - `kind: gate` — 1-agent job with `checks` (the old implicit gate mode).

If `routine` may carry `checks`, then `kind` is **not** a single source of truth — `tick.py`
still needs `if job.checks is not None` to decide between the plain and gated paths, and the
enum adds a third label that doesn't partition the space. That's the implicit switch plus a
new redundant field, i.e. worse than today.

Fix — choose one:
1. **Minimal (recommended for this increment):** introduce only `kind: pipeline | routine`
   (default `routine`). Gate mode stays `checks is not None` inside the `routine` branch,
   untouched. No migration for the Pi's live `babysit-prs` / `repo-hygiene` configs. The
   `kind: gate` label is scope creep this issue doesn't need.
2. **Full SSOT:** `kind: routine` **forbids** `checks` (validation error), `kind: gate`
   **requires** non-empty `checks`, `tick.py:1032` switches on `job.kind` not
   `job.checks is not None`, and `_process_gated_job`'s asserts (`tick.py:127-128`) key off
   `kind`. This is a real migration (every existing gate job's YAML gains `kind: gate`) and
   should be its own issue if wanted.

Either way, delete the "has `checks` optionally" clause under `routine` — that's the bug.

---

## C) "Redirect the report guard, don't skip it"

**Adopted, sound.** Collapsing to a single `$REPORT` placeholder (with `$ROUTINE_REPORT`
kept as an alias) in `substitute_prompt` (`runner.py:267-274`) is clean and also fixes the
run_id-namespace mismatch for the report specifically. Keeping the exists-and-non-empty
check (`runner.py:617-623`) for pipeline jobs preserves the silent-orchestrator-death
detector that `design.md:347-356` documents as a real incident. Good.

### Residual C1 — "partial tolerated only on deadline-exceeded" needs a signal tick can read

AC #4 requires the guard to "tolerate a **partial** report only when the deadline was
exceeded." For `_process_pipeline_job` to make that distinction at reconcile time it needs
to *know* the deadline was exceeded. Candidates: (a) the report's own content (orchestrator
writes `## Outcome: partial (deadline exceeded)` — `design.md:170-173` already has it write
a partial report + `notification show` on deadline); (b) `state.json` (`deadline_epoch` vs
the reconcile clock and `current_stage < 6`); (c) tick killed the `rt-` agent itself at
`deadline_ms + margin`. Option (a) is consistent with C above (reconcile from report
content) — spec should name it. Without a named signal, AC #4's "only when" is untestable.

---

## D) "Plumb `HERDR_ENV=1` and pin the report path"

**Adopted as a hard prerequisite with its own AC (#5).** Correct call — `herdr.py` has zero
env support (grep confirms; nearest is `agent_start` at `herdr.py:284` with no env param),
and `design.md:131-134` is unambiguous that the orchestrator is dead without it.

### Residual D1 — the spec says "plumb through herdr.py" but the mechanism is actually the generated shell command

Spec §"Hard runtime prerequisite": *"Plumb an env-injection path through the launch
(pane/workspace creation)."* That reads as a `HerdrClient` / `herdr.py` change. But under
the dispatch-detached design, `tick` **generates a `systemd-run` invocation that itself
runs `herdr workspace create --env HERDR_ENV=1 …`** (that flag already exists —
`design.md:133`, `plan:63`). The env never flows through `HerdrClient` at all — it's a
literal in the emitted command string. That's *simpler* than the spec implies and worth
stating plainly, because "modify herdr.py's agent_start to accept env" is unnecessary work
for this path. If any non-pipeline caller ever needs env injection through `HerdrClient`,
that's a separate concern. Reword so the implementer builds the string, not the client API.

### Residual D2 — `_check_systemd_timeout` must exclude `kind: pipeline`

The spec claims (design notes) that dropping the blocking `timeout_ms` "removes the whole
systemd `TimeoutStartSec` conflict." It doesn't, automatically: `_check_systemd_timeout`
(`cli.py:430-499`) iterates **every** enabled job and, for a non-gated one, adds
`(start_timeout_ms + timeout_ms)/1000` to the required unit budget (`cli.py:490`). A
`kind: pipeline` job still has a `Job.timeout_ms` (default `1_800_000`, `config.py:103`)
unless the schema drops it — so it silently inflates the required `TimeoutStartSec` by
~30 min, or worse if someone sets `timeout_ms` high by analogy with `deadline_ms`. There
should be an explicit AC: **`_check_systemd_timeout` skips `kind: pipeline` jobs entirely**
(they don't run in the tick's process). Issue 025 has the analogous AC for the gate formula;
026 needs its negative counterpart.

---

## E) Acceptance criteria — testability, non-vacuity, completeness

The 11 ACs are a large improvement. Most are now concrete and mechanically checkable with
the existing fake-`HerdrClient` + `tmp_config_path` fixtures. Per-item:

| AC | Assessment |
|---|---|
| 1 suppress report warning | Testable. `cli.py:398` currently gates the warning on `job.checks is None and "$ROUTINE_REPORT" not in job.prompt` — add `and job.kind != "pipeline"`. Fine. |
| 2 dispatch detached, return immediately | Testable with a stubbed launcher seam. **Needs** the spec to name that seam (a `subprocess.run(["systemd-run", …])` wrapper that tests monkeypatch) — otherwise "a stubbed unit short-circuits" is hand-waving. Add: the launcher is an injectable function/CommandRunner, same pattern as `HerdrClient`. |
| 3 concurrency (make-or-break) | Now genuinely testable and non-vacuous: two jobs in one `run_tick`, pipeline first, assert the second still reaches `execute_run`. This is the criterion v1 could not meet — good. |
| 4 guard redirected + partial-tolerant | Testable **once C1's signal is named.** As written, the "only when the deadline was exceeded" half has no defined input. |
| 5 `HERDR_ENV=1` reaches the orchestrator | Testable against the generated invocation string (assert `--env HERDR_ENV=1` present). Reword per D1 — it's a string assertion, not a client-API assertion. |
| 6 `catch_up_minutes: 0` | **Weak as written** — "a pipeline job sets `catch_up_minutes: 0`" tests that the *example config* sets it, not that the system guarantees it. Make it an invariant: `validate` rejects `kind: pipeline` with `catch_up_minutes > 0` (or `_process_pipeline_job` ignores catch-up unconditionally). Then the test asserts behavior, not a config convention. |
| 7 deadline vs kill margin | Testable in principle but under-specified: *who* enforces `deadline_ms + margin`? If `tick` never blocks, `tick` isn't watching a clock. The margin belongs to whatever kills the orchestrator — either the transient unit's own `--timeout` (set to `deadline_ms + margin`) or nothing kills it and the orchestrator self-terminates at `deadline_epoch` (`design.md:170-173`). Spec should say the generated `systemd-run` uses `--timeout $((deadline_ms + margin))` and the test asserts that arithmetic. |
| 8 `scheduled` + `status` render | Testable. `scheduled` listing the cron is trivial. "`status` renders `rt-nightly-pipeline` sanely" — note the generated invocation **must name the orchestrator agent `rt-nightly-pipeline`**, diverging from `design.md:289`'s `pipeline-orchestrator`; the spec implies this (via the `_live_agent_exists` reuse) but should call the divergence out so the implementer doesn't copy the doc verbatim. |
| 9 resume same RUN_ID | **Still a defer.** "(or is explicitly replaced/reworked in this issue)" lets the AC pass by fiat. The concrete gap: `_cmd_run` (`cli.py:502-510`) hardcodes `make_run_id(job.name, now)` — there is no way to re-run with a prior run_id. Pick one: add `herdr-routines run <job> --run-id <id>`, or a dedicated `herdr-routines pipeline-resume <run_id>` that regenerates the same `systemd-run` invocation. The AC should name the chosen mechanism and test *it*. |
| 10 `gc` excludes `auto/pipeline-*` | Testable and already true — `gc.py:17` `PIPELINE_PREFIX = "auto/pipeline-"`, `gc.py:57` filters it. Pure regression guard, fine. Valid only while the orchestrator keeps naming the branch `auto/pipeline-<RUN_ID>` (ties back to A2). |
| 11 legacy routine guard unchanged | Solid. Keep. |

### Missing ACs

- **`_check_systemd_timeout` skips `kind: pipeline`** (D2) — currently no AC covers it; without one the "no `TimeoutStartSec` conflict" claim is unverified and probably false.
- **`validate` handles `kind: pipeline` repo/workspace checks** — `cli.py:389` runs the
  `.git` check only when `job.workspace == "worktree"`, and `workspace` defaults to
  `"worktree"` (`config.py:99`). A pipeline job with the default workspace trips a check the
  spec says doesn't apply. Need an AC that `validate` treats `workspace` as N/A for
  `kind: pipeline` and validates `repo` as "a git repo (parent clone)" only.
- **Config schema round-trip** — new keys (`kind`, `deadline_ms`, `env`, `prompt_file`)
  parse, get defaults, and reject unknown/malformed values (`config.py:76` `_JOB_ALLOWED_KEYS`,
  `config.py:96` `_JOB_DEFAULTS`). Issue 025 has an equivalent `test_..._config_validation`;
  026 should too.

---

## Top-level framing

**Now honest and correct.** "Move pipeline *scheduling* into `tick`", engine unification
explicitly punted to issue 013, the scope-honesty callout in the Description — all good.
The Description accurately states that stages stay hardcoded either way. No objection.

---

## Config-shape critique

- **`env:` as a free-form map is over-built (YAGNI).** `HERDR_ENV=1` is the *only* value
  that is ever needed and it is **mandatory** for `kind: pipeline` (AC #5: "without it the
  launch is blocked"). A mandatory constant is not configuration. Drop the `env` block;
  make `HERDR_ENV=1` implicit in the generated invocation for `kind: pipeline`. A generic
  `env` map also invites "why not for routines" and needs shell-escaping/validation you
  otherwise don't.
- **`catch_up_minutes: 0` should be enforced, not conventional** (see AC #6). Either
  `validate` rejects a non-zero value for `kind: pipeline`, or `_process_pipeline_job`
  ignores catch-up. Don't rely on the operator copying the example.
- **`deadline_ms`** — good name (unambiguously distinct from `timeout_ms`), sensible as an
  override of `design.md`'s launch+7 h default.
- **`prompt_file`** — clean axis separation from `prompt`, agreed. Note it's a second
  feature riding in this issue (it's useful for plain routines too); fine, but the ACs
  don't cover `prompt_file` for a non-pipeline job — add one, or state it's pipeline-only
  for now.
- **`prompt_file` resolution location** — `config.py` is documented as pure "no filesystem
  access beyond reading the one YAML file" (`config.py:3-4`). Reading `prompt_file` from
  disk must happen in `tick`/`runner` (or `_cmd_validate`), not in `load_config`, or that
  invariant breaks. Worth a one-line note in the spec.
- **`timeout_ms` for a pipeline job** — the schema should either drop it for `kind: pipeline`
  or define it as the per-*reconcile-staleness* bound. `find_stale_running` /
  `is_currently_running` need *some* bound; the spec says "against the deadline", so
  `_process_pipeline_job` passes `job.deadline_ms` there. Make that explicit — a reader
  currently has to infer that `Job.timeout_ms` is unused on this path.

---

## Blunt second verdict: **MOSTLY SOUND**

The approach is right and implementable against the current code. The revision fixed every
structural defect from pass 1. What's left is spec-refinement, not redesign:

**Residual bits to close before implementation (all concrete, none architectural):**

1. **RUN_ID contract (A2)** — `tick` passes the bare UTC timestamp as the orchestrator's
   `RUN_ID` (the full `make_run_id` string overflows the 32-char `pl-<N>-<run_id>` agent cap
   at stage 1) and passes the report path as an absolute literal. State this explicitly.
2. **Reconciliation source (A3 + C1)** — reconcile completion and partial-vs-final from the
   **report file's content**, not `state.json` (whose location isn't pinned and whose
   worktree may be gc'd). Name the marker the orchestrator writes.
3. **`kind:` enum (B)** — drop the "routine has `checks` optionally" clause. For this
   increment, ship only `kind: pipeline | routine` and leave gate mode as `checks is not
   None`; defer `kind: gate` to its own migration issue.
4. **Env injection is a string literal, not a `herdr.py` change (D1)** — the generated
   `systemd-run` command runs `herdr workspace create --env HERDR_ENV=1`; no `HerdrClient`
   API change. Reword.
5. **Add the three missing ACs (D2 + E):** `_check_systemd_timeout` skips `kind: pipeline`;
   `validate` treats `workspace` as N/A and repo-checks a plain clone for `kind: pipeline`;
   config schema round-trip / validation for the four new keys.
6. **Enforce, don't convention (AC #6, #9):** `catch_up_minutes: 0` becomes a validation
   rule; AC #9 names the actual resume mechanism (`run --run-id` or `pipeline-resume`) and
   tests it instead of deferring.
7. **Launcher seam (AC #2)** — declare the `systemd-run` call an injectable
   function/runner so the detached-dispatch tests can stub it, same pattern as
   `HerdrClient`.
8. **Minor:** launch-then-record ordering (A1); generated invocation names the orchestrator
   agent `rt-<job>` not `pipeline-orchestrator` (AC #8); `prompt_file` read happens outside
   `load_config` to preserve its purity.
