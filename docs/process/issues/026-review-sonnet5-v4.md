# Independent design review (pass 4, CONFIRMATION) — Issue 026, v4 spec (commit 85bf5c3)

Pass-3 verdict: MOSTLY SOUND (close to SOUND) — 2 one-sided contracts + 3
tightenings + 2 doc items. This pass checks each against the current spec text
and the code.

**Verdict: SOUND — ready to implement.**

Every pass-3 residual is now closed on *both* sides of its contract, in scope
(not merely mentioned), and with an AC that asserts the real end-to-end
behaviour. A competent implementer can ship this without rediscovering a
contract. Remaining nits are cosmetic / determinism polish, listed at the end —
none makes an implementation silently wrong.

---

## 1. The two one-sided contracts

### (a) `## Outcome:` marker — NOW IN SCOPE, both sides specified

- **Emit side:** spec §"Report semantics" step 3 now says *"this issue adds a
  small, scoped task to `orchestrator-prompt.md` to write an explicit outcome
  line on its terminal branches — `## Outcome: ok`, `## Outcome: failed`, or
  `## Outcome: partial (deadline exceeded)`"*, and Non-goals is amended:
  *"Not changing the orchestrator's … stage internals, **except** the scoped
  `## Outcome:` terminal-status line"*. The carve-out is explicit — the
  implementer will not skip it as out-of-scope.
- **Read side:** step 3 + AC #5 — `tick` reconciles done/failed/partial *from
  the marker, not the clock*; partial tolerated **only** when the report
  carries `## Outcome: partial (deadline exceeded)`.
- **Against the code/docs:** `orchestrator-prompt.md:163` ("Always write
  `$PIPELINE_REPORT` … where it stopped and why") is the natural single
  insertion point; the deadline branch (`:152`) and abort branches (`:108`,
  `:148`) are the `failed` / `partial` cases; stage-6 completion is `ok`. No
  structured status line exists today (confirmed: `grep -n Outcome
  docs/pipeline/orchestrator-prompt.md docs/pipeline/design.md` → nothing) — so
  the task is real and now owned. Reconcile logic is well-defined: exact-match
  `## Outcome: partial (deadline exceeded)` → tolerate; `## Outcome: ok` →
  done; `## Outcome: failed` or missing/empty → failed.

Ships. Contract is specified on both sides.

### (b) `$PIPELINE_REPORT` substitution — NOW IN THE CONTRACT

- Spec step 1 now explicitly: *"**Also rewrite `$PIPELINE_REPORT` to the same
  pinned path** — the orchestrator prompt uses `$PIPELINE_REPORT` throughout
  (orchestrator-prompt.md:52/:155/:163), not `$REPORT` … `AC #5` asserts the
  token the orchestrator receives, i.e. `$PIPELINE_REPORT` too."*
- AC #5: *"`substitute_prompt` rewrites `$PIPELINE_REPORT` (the token the
  orchestrator actually uses) **and** `$REPORT` / back-compat `$ROUTINE_REPORT`
  … Assert the token the orchestrator receives, end to end."*
- Verified against code: `runner.py:267-274` `substitute_prompt` today only
  does `$ROUTINE_REPORT` / `$ROUTINE_JOB` / `$ROUTINE_RUN_ID`; the prompt file
  uses `$PIPELINE_REPORT` (`grep -n PIPELINE_REPORT
  docs/pipeline/orchestrator-prompt.md` → :52, :155, :163). The pinned absolute
  path (from `default_reports_dir()/f"{run_id}.md"` with tick's own run_id) now
  provably reaches the orchestrator via `$PIPELINE_REPORT`.

The "green test, dead contract" failure mode from pass 3 is closed.

---

## 2. The three tightenings

### (c) `_cmd_run` branches on `kind` — CLOSED

AC #11: *"`_cmd_run` gains the `--run-id` flag, and **branches on `kind`** so
that `herdr-routines run nightly-pipeline` (with *or* without `--run-id`)
dispatches via the same detached launcher rather than calling `execute_run`
synchronously (cli.py:519). The test drives both the resume and the
plain-detached paths."*

Verified: `cli.py:502-521` `_cmd_run` currently does
`run_id = make_run_id(job.name, now)` (:510) then `execute_run(job, client,
run_id=run_id)` (:519) unconditionally — a `kind: pipeline` job there would run
the orchestrator blocking in the foreground, exactly what the issue forbids.
The AC now covers the non-resume path too, and names the seam
(shared detached launcher). Non-vacuous — the test asserts both paths reach the
launcher, not `execute_run`.

### (d) `catch_up_minutes` default-120 inheritance — CLOSED

- Design notes: *"`catch_up_minutes` defaults to 120 (config.py:105); …
  `validate` **rejects** a pipeline job whose effective `catch_up_minutes != 0`
  — whether set or inherited from the default. The schema defaults it to 0 for
  `kind: pipeline`."*
- AC #8: *"`validate` **rejects** a `kind: pipeline` job whose effective
  `catch_up_minutes != 0` — whether explicitly set or inherited from the 120
  default … Enforced (a validation rule + a pipeline-specific default of 0), not
  conventional."*
- Verified: `config.py:105` `"catch_up_minutes": 120` in `_JOB_DEFAULTS` —
  confirmed, so a pipeline job omitting the key would inherit 120 without this
  rule. Both the kind-specific default and the reject rule are now specified.
  The "missed 02:00 fires the 7h run mid-morning" hazard is genuinely blocked.

### (e) `RuntimeMaxSec=` in seconds, not `--timeout`, not ms — CLOSED

- Design notes: *"the generated `systemd-run` unit cap is set via
  `-p RuntimeMaxSec=$(( (deadline_ms + margin) / 1000 ))` (systemd property,
  **seconds**, not the non-existent `--timeout` flag and not ms — `RuntimeMaxSec`
  is 1000× coarser than `deadline_ms`)"*.
- AC #4: *"sets the unit cap `-p RuntimeMaxSec=$(( (deadline_ms + margin) /
  1000 ))` (seconds, not `--timeout`, not ms)"*.
- Correct: `systemd-run` has no `--timeout`; `RuntimeMaxSec` is the right
  transient-unit property and takes a seconds value (or a time span). The `/1000`
  conversion is explicit in the AC assertion. An implementer copying
  `design.md:295`'s loose `--timeout 25200000` is now steered off it.

---

## 3. Cosmetic + documented items

- **RUN_ID length:** spec §"The RUN_ID contract" now reads *"`pl-1-` + the full
  run id (33 chars for `nightly-pipeline-20260830T020000Z`) = 38 chars —
  overflow by 6"*. Correct: `nightly-pipeline` (16) + `-` (1) +
  `%Y%m%dT%H%M%SZ` (16) = 33; `pl-1-` (5) + 33 = 38; cap is 32
  (`config.py:58-59` comment, `[a-z][a-z0-9_-]{0,31}`). Bare timestamp path:
  `pl-1-20260830T020000Z` = 21, fits. Also consistent with the orchestrator's
  own `RUN_ID` derivation (`orchestrator-prompt.md:25`, `date -u
  +%Y%m%dT%H%M%S…`). Fixed correctly.
- **Explicit `workspace:` on a pipeline job:** design notes — *"the schema
  **rejects** an explicit `workspace:` on a `kind: pipeline` job (or ignores it
  with a warning) rather than leaving it settable-but-ignored."* Stated. (Pick
  one of the two for determinism — see nits.)
- **Known limitation:** new section *"If the orchestrator dies before writing
  *any* report, the job shows in-flight until `find_stale_running` trips at
  `job.deadline_ms` (~7h) — matching today's behaviour (design.md:347-356
  incident). Accepted; not fixed by this issue."* Stated plainly as accepted.

---

## 4. Full AC sweep (13)

| AC | Verdict |
|---|---|
| 1 suppress report warning | Testable. `cli.py:398` gates on `job.checks is None and "$ROUTINE_REPORT" not in job.prompt`; a `prompt_file`-backed pipeline job has empty `job.prompt` → would hit the "prompt is empty" warning (:404). Needs `and job.kind != "pipeline"` at :398. Concrete, non-vacuous. |
| 2 launch-before-record + injectable seam | Testable; seam named (`CommandRunner`-style, monkeypatched like `HerdrClient`). Order asserted by name. |
| 3 concurrency (make-or-break) | Non-vacuous — two jobs, pipeline first, assert job 2 reaches `execute_run`. The criterion v1 could not meet. |
| 4 invocation string contract | Testable string assertion: `rt-<job>`, `HERDR_ENV=1`, `RuntimeMaxSec` seconds arithmetic, bare-ts RUN_ID, absolute report path. |
| 5 marker emit + `$PIPELINE_REPORT`/`$REPORT` rewrite + guard partial-tolerance | Testable end to end. Bundles three assertions (prompt emits marker; substitute covers all three tokens; guard tolerates partial only on the marker) — implementer may split into sub-tests; not vacuous, not contradictory. |
| 6 systemd-timeout skip | Verified against `cli.py:471-490` (adds `(start_timeout_ms + timeout_ms)/1000` per non-gated job); skip is correct and the AC asserts absence from the sum. |
| 7 workspace N/A + repo-is-clone | Verified `cli.py:389` runs the `.git` check only for `workspace == "worktree"` (the `config.py:99` default) — a pipeline job with the default would trip "not a git repository" without the special-case. AC pins exactly that. |
| 8 reject `catch_up_minutes != 0` | Behavioural, covers set *and* inherited-120. Non-vacuous. |
| 9 config schema round-trip | `kind` / `prompt_file` / `deadline_ms` into `_JOB_ALLOWED_KEYS` (`config.py:76-91`) + `_JOB_DEFAULTS` (`config.py:96-113`); `kind` needs a `VALID_KINDS` frozenset like `VALID_WORKSPACE_MODES` (`config.py:55`). Mirrors issue 025. Necessary. |
| 10 status renders in-flight orchestrator | Testable; relies on the invocation naming the agent `rt-nightly-pipeline` (spec §note at :124-127 calls out the divergence from `design.md:289`). |
| 11 resume + plain-detached both branch on kind | Verified against `cli.py:510/:519`. Covers both paths. |
| 12 gc excludes `auto/pipeline-*` | Branch is `auto/pipeline-<bare-ts>` (`orchestrator-prompt.md:35`), matches `gc.py:17 PIPELINE_PREFIX`. Correctly tied to the RUN_ID choice. Regression guard. |
| 13 legacy routine guard unchanged | Solid — `$REPORT`/`$ROUTINE_REPORT` still written, `no_report` guard + `checks is not None` dispatch untouched. |

**No vacuous criterion** (old "example sets catch_up: 0" is now behavioural;
old "or is reworked" fiat is now a named mechanism tested on both paths).
**No untestable criterion** (AC #5's "only when the deadline was exceeded" now
has a defined input: the emitted marker).
**No mutually-contradictory criterion** — AC #7 (default `workspace` treated as
N/A) and the design-note "reject explicit `workspace:`" are consistent: default
is tolerated-and-ignored, an *explicit* value is rejected.
**Nothing missing** that would block implementation.

---

## Non-blocking nits (polish, not defects)

1. **AC #5 bundles three assertions.** Fine to ship, but the implementer should
   split into `test_orchestrator_prompt_emits_outcome_marker`,
   `test_substitute_prompt_rewrites_pipeline_report`, and
   `test_pipeline_report_guard_partial_tolerant` for clean failure attribution.
2. **`margin` is unnamed.** `deadline_ms + margin` appears in AC #4 and design
   notes with no value — define a module constant (e.g.
   `PIPELINE_UNIT_MARGIN_MS = 600_000`) so the RuntimeMaxSec arithmetic is
   deterministic and testable to an exact number.
3. **"rejects … (or ignores it with a warning)"** for explicit `workspace:` —
   pick one. Reject is the more consistent choice (matches the `catch_up`
   treatment) and gives a deterministic AC #7.
4. **`## Outcome:` insertion point** — worth one sentence naming
   `orchestrator-prompt.md:163` ("Always write `$PIPELINE_REPORT` …") as where
   the line goes, so the prompt edit is a lookup not a hunt. Not essential;
   "terminal branches" is findable.

None of these change behaviour or hide a contract. Ship it.

---

## FINAL: **SOUND (ready to implement).**
