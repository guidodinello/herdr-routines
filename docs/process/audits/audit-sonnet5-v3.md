# Independent design review (pass 3, FINAL) — Issue 026, v3 spec (commit a63f995)

Pass-1 verdict: SHAKY. Pass-2 verdict: MOSTLY SOUND, ~8 residual items.
This pass checks each residual against the current spec and the code.

**Third verdict: MOSTLY SOUND (very close to SOUND).** Six of the eight pass-2
residuals are closed correctly and would hold against the code. Two are closed
only on the `tick` side — the spec names a contract (`## Outcome:` marker;
`$REPORT` substitution) that the *orchestrator half* does not yet honour, and
the spec does not put that half in scope. Both are one-line prompt edits, not
redesign, but as written the reconcile path would silently not work. Fix those
two and this is SOUND.

---

## Per-residual verification

### 1. RUN_ID contract (A2) — CLOSED, correct

Spec §"The RUN_ID contract" now states `tick` passes the **bare UTC timestamp**
as `RUN_ID` and the full `make_run_id(job.name, occ)` only for its own history
key; the report path goes in as an absolute literal.

Checked against code:
- `make_run_id` (`runner.py:258-259`) = `f"{job_name}-{ts}"` with
  `ts = %Y%m%dT%H%M%SZ` (16 chars). For `nightly-pipeline` that is
  `nightly-pipeline-20260830T020000Z` = **33 chars** (spec says 32 — minor
  arithmetic slip; `pl-1-` + 33 = 38, spec says 37/"overflow by 5". The count
  is off by one but the conclusion is right: it does not fit the 32-char cap).
- Agent-name cap: `config.py:58-59` comment states the `[a-z][a-z0-9_-]{0,31}`
  32-char cap; `NAME_RE` at :60 is the *job*-name regex (24). The cited cap is
  real, just not literally on line 60.
- Worker template: `orchestrator-prompt.md:76` `herdr agent start
  "pl-${N}-${RUN_ID}"` — confirmed. Bare timestamp → `pl-1-20260830T020000Z`
  = 21 chars, fits.
- Bonus: `orchestrator-prompt.md:25` already derives `RUN_ID` as
  `date -u +%Y%m%dT%H%M%S…` — the bare timestamp is the orchestrator's *own*
  native expectation, so this choice is the natural one, not a workaround.

Action: fix "32"→"33" and "37"→"38" in the spec (cosmetic). Contract itself is
sound.

### 2. Reconcile from report content, not state.json (A3+C1) — PARTIALLY CLOSED

Spec now says: reconcile from the report file's content; the orchestrator writes
`## Outcome: partial (deadline exceeded)`; parse done/failed/partial from that
marker. Dropping `state.json` (unpinned, in the orchestrator's gc-able worktree)
is the right call and matches `ps.py`'s fragile `rglob("state.json")` today.

**Gap: the marker does not exist and the spec does not add it.**
`orchestrator-prompt.md:152` / `:163` and `design.md:168-173` have the
orchestrator write *"a partial `$PIPELINE_REPORT`"* with prose "where it stopped
and why" — there is **no structured `## Outcome:` line** anywhere in the prompt
or design. The spec's parenthetical "(it already writes a partial report +
`notification show` on deadline)" conflates *writes a partial report* (true)
with *writes the marker* (false). AC #5's "only when the report carries the
`## Outcome: partial (deadline exceeded)` marker" is untestable and unshippable
until `orchestrator-prompt.md` is edited to emit exactly that string on the
deadline branch (and a matching `## Outcome: ok` / `## Outcome: failed` on the
normal terminal paths, or the parser has nothing to distinguish done from
failed either).

This is a ~3-line edit to the prompt's deadline/terminal branches, but it must
be **in scope and in an AC**. Right now Non-goals says "Not changing the
orchestrator's prompt-based 6-stage model or stage internals" — the marker edit
needs an explicit carve-out from that.

### 3. `kind:` enum (B) — CLOSED, correct

Spec §"Mode discriminator" now ships **only** `kind: pipeline | routine`
(default `routine`), leaves gate dispatch as `job.checks is not None` inside the
`routine` branch (`tick.py:1032`), and explicitly defers `kind: gate` /
full-SSOT to a separate migration. The self-contradiction ("routine has
`checks` optionally" while `kind` is SSOT) is gone. No migration for the Pi's
live `babysit-prs` / `repo-hygiene` configs — they never set `kind`, get the
`routine` default, keep hitting `checks is not None`. Correct, zero live-config
risk.

### 4. HERDR_ENV=1 is a string literal (D1) — CLOSED, correct

Spec §"`HERDR_ENV=1` is a string literal, not a `herdr.py` change" now reads
plainly: `tick` emits a shell command that itself runs
`herdr workspace create --env HERDR_ENV=1 …`; "The env never flows through
`HerdrClient`/`herdr.py` at all". Matches reality — `herdr.py` has no env param
on `agent_start`, and `orchestrator-prompt.md:59` / `design.md:288` already use
`--env HERDR_ENV=1` on `workspace create`. AC #4 asserts the flag in the
generated string, not a client API. Correct. `env:` map stays out (YAGNI) —
agreed.

### 5. `_check_systemd_timeout` skips `kind: pipeline` (D2) — CLOSED, correct

Spec design-notes + AC #6 (`test_systemd_timeout_skips_pipeline_job`). Checked
`cli.py:430-499`: the loop at :471 adds `(start_timeout_ms + timeout_ms)/1000`
for every non-gated enabled job (:490). A `kind: pipeline` job keeps
`Job.timeout_ms` default `1_800_000` (`config.py:103`) unless skipped, so
without the skip it silently inflates the required `TimeoutStartSec` by ~30 min.
The skip is correct (pipeline never runs in-process) and the AC is testable:
add a `kind: pipeline` job, assert its seconds are not in `total_job_seconds`.
Non-vacuous.

### 6. Enforce `catch_up_minutes>0` rejection (AC #8) + named resume (AC #11) — CLOSED

- AC #8 `test_validate_rejects_pipeline_catchup`: spec config-shape comment says
  "ENFORCED: validate rejects >0 for pipeline" and AC #8 is a behavioural
  assertion, not "the example sets 0". Testable. Note: `catch_up_minutes`
  default is `120` (`config.py:105`), so a pipeline job that *omits* the key
  inherits 120 → validate must reject the default too, or the schema must force
  `0` for `kind: pipeline`. Spec should say which (reject-unless-explicitly-0 is
  cleaner). Minor.
- AC #11 `test_pipeline_resume_same_run_id`: names the mechanism —
  `herdr-routines run <job> --run-id <id>`, `_cmd_run` gains `--run-id`.
  Checked `cli.py:126-133` (`p_run` has only `job` + `--dry-run`) and
  `cli.py:510` (`run_id = make_run_id(job.name, now)` hardcoded). The flag is a
  real, small addition and the AC tests it. Closed.
  **Sub-gap:** `_cmd_run` currently calls `execute_run(...)` **synchronously**
  (`cli.py:519`). For `kind: pipeline` that would run the orchestrator blocking
  in the foreground — the exact thing the whole issue rejects. AC #11 covers
  the `--run-id` resume path but the spec never says `_cmd_run` must branch on
  `kind` to dispatch detached for the *non-resume* `herdr-routines run
  nightly-pipeline` case too. Add one clause: "`_cmd_run` dispatches
  `kind: pipeline` via the same detached launcher, with or without `--run-id`."

### 7. Launcher seam (AC #2) + launch-then-record ordering — CLOSED, correct

- AC #2 `test_tick_dispatches_pipeline_launch_before_record`: spec names "an
  injectable `systemd-run` launcher seam — a `CommandRunner`-style function that
  tests monkeypatch, same pattern as `HerdrClient`". Concrete and matches the
  codebase's existing DI style. Testable without waiting hours.
- Ordering: spec step 1 is now "**Launch first, then record.** … only after
  `systemd-run` exits 0 writes the `running` record", with the rationale
  (writing first wedges the job for the whole deadline on a launch hiccup).
  Correct — `systemd-run` returns once the transient unit registers, so this is
  safe. AC #2's name asserts the order.

### 8. Three added ACs (#6, #7, #9) — SOUND, non-vacuous

- **#6 systemd-timeout skip** — see item 5. Non-vacuous (asserts absence from
  the budget sum).
- **#7 workspace N/A + repo-as-clone** (`test_validate_pipeline_workspace_na_repo_is_clone`):
  checked `cli.py:389` — the `.git` check runs only for `workspace ==
  "worktree"`, and `workspace` defaults to `"worktree"` (`config.py:99`), so a
  pipeline job with the default *would* trip
  "repo is not a git repository" unless `validate` special-cases `kind:
  pipeline`. The AC pins exactly that. Non-vacuous. (Spec should also say the
  schema either rejects `workspace:` on a pipeline job or ignores it — leaving
  it settable-but-ignored is a footgun.)
- **#9 config schema round-trip** (`test_pipeline_config_schema_roundtrip`):
  new keys `kind`, `prompt_file`, `deadline_ms` must be added to
  `_JOB_ALLOWED_KEYS` (`config.py:76-91`) and `_JOB_DEFAULTS`
  (`config.py:96-113`); `kind` needs a `VALID_KINDS` frozenset + validation
  like `VALID_WORKSPACE_MODES`. AC mirrors issue 025's pattern. Sound and
  necessary.

---

## Cross-cutting gap the residual list missed: `$PIPELINE_REPORT` is not in the substitution contract

Spec §"Report semantics" step 1: teach `substitute_prompt` a single `$REPORT`
(keep `$ROUTINE_REPORT` as alias). AC #5 asserts `$REPORT` and
`$ROUTINE_REPORT`.

But `orchestrator-prompt.md` uses neither — it uses **`$PIPELINE_REPORT`**
throughout (`:52`, `:155`, `:163`, and design.md `:168`, `:283-295`).
`substitute_prompt` today (`runner.py:267-274`) only rewrites `$ROUTINE_REPORT`
/ `$ROUTINE_JOB` / `$ROUTINE_RUN_ID`. So as the spec is written, the pinned
absolute report path is substituted into a token (`$REPORT`) that **does not
appear in the orchestrator prompt**, and `$PIPELINE_REPORT` passes through
untouched — the orchestrator computes its own path and mirrors to
`reports/<bare-ts>.md` (`:163`), which is *not* the path `tick` reconciles from.

Fix — pick one, state it, and AC it:
- (a) `substitute_prompt` also rewrites `$PIPELINE_REPORT` → same path; or
- (b) edit `orchestrator-prompt.md` to use `$REPORT` everywhere (mechanical,
  but a prompt edit — needs the same Non-goals carve-out as the `## Outcome:`
  marker).

Without this, AC #5 passes its unit test (`substitute_prompt` rewrites
`$REPORT`) while the real orchestrator never receives the pinned path. Classic
"green test, dead contract."

---

## Full AC sweep (13 criteria)

| AC | Verdict |
|---|---|
| 1 suppress report warning | Testable. `cli.py:398` gates on `checks is None and "$ROUTINE_REPORT" not in prompt`; a `prompt_file`-backed pipeline job has empty `job.prompt` → hits the "prompt is empty" branch (:404). Needs `and job.kind != "pipeline"` added at :398. Concrete. |
| 2 launch-before-record + injectable seam | Sound (item 7). |
| 3 concurrency (make-or-break) | Sound, non-vacuous — two jobs, pipeline first, assert job 2 reaches `execute_run`. This is the criterion v1 failed. |
| 4 invocation string contract | Testable **but** see below on `--timeout`. Asserts `rt-<job>`, `HERDR_ENV=1`, unit timeout arithmetic, bare-ts RUN_ID, absolute report path. |
| 5 report guard redirected + partial-tolerant | **Blocked** on item 2 (marker not emitted) and the `$PIPELINE_REPORT` gap above. Unit-testable in isolation; contract-dead as specified. |
| 6 systemd-timeout skip | Sound (item 5). |
| 7 workspace N/A + repo-is-clone | Sound (item 8). |
| 8 reject `catch_up_minutes>0` | Sound; clarify default-120 handling (item 6). |
| 9 config schema round-trip | Sound (item 8). |
| 10 status renders in-flight orchestrator | Testable. Relies on the generated invocation naming the agent `rt-nightly-pipeline` (spec calls out the divergence from `design.md:289`'s `pipeline-orchestrator` at spec :111-114 — good, implementer is warned). |
| 11 resume same RUN_ID | Sound; add the non-resume `_cmd_run` branch clause (item 6). |
| 12 gc excludes `auto/pipeline-*` | Sound. The orchestrator creates `auto/pipeline-$RUN_ID` itself (`orchestrator-prompt.md:35`); with bare-ts RUN_ID the branch is `auto/pipeline-20260830T020000Z`, matches `gc.py:17 PIPELINE_PREFIX`. Correctly tied to AC #1's RUN_ID choice. Pure regression guard. |
| 13 legacy routine guard unchanged | Solid. |

**No mutually-contradictory criteria.** No vacuous criteria after the pass-2
fixes (old AC #6 "example sets catch_up: 0" is now behavioural; old AC #9
"or is reworked" fiat is now a named mechanism).

### `--timeout` on `systemd-run` (AC #4) — verify the flag

Spec (and `design.md:295`) write `systemd-run --timeout $((deadline_ms +
margin))`. `systemd-run` has **no `--timeout` option** — the runtime cap is
`-p RuntimeMaxSec=<sec>` (or `--property=RuntimeMaxSec=`). `design.md:295` even
notes the "25200000" there was a per-worker *prompt* `--timeout`, not a unit
property. AC #4 asserts "the arithmetic"; make sure it asserts it on
`RuntimeMaxSec=` (seconds, not ms) so the implementer doesn't ship a flag
`systemd-run` rejects. Also unit: `deadline_ms` is ms, `RuntimeMaxSec` is
seconds — the spec's `$((deadline_ms + margin))` would be 1000× too large.

---

## Still-missing / underspecified

1. **The `## Outcome:` marker task** — orchestrator-prompt.md must emit a
   parseable terminal-state line (ok / failed / partial-deadline). No task, no
   AC, and Non-goals currently forbids prompt-internal edits. (item 2)
2. **`$PIPELINE_REPORT` substitution** — the token the orchestrator actually
   uses is absent from the `substitute_prompt` contract. (cross-cutting gap)
3. **`_cmd_run` non-resume pipeline branch** — must dispatch detached, not
   `execute_run` synchronously. (item 6)
4. **`catch_up_minutes` default-120 inheritance** for a pipeline job that omits
   the key — reject, or force 0 in the schema. (item 6)
5. **`workspace:` on a pipeline job** — reject at schema level, or the "N/A"
   claim is just convention. (item 8)
6. **Two report files** — the orchestrator's own mirror to
   `reports/<bare-ts>.md` (`orchestrator-prompt.md:163`) coexists with tick's
   pinned path. Harmless (tick reads the pinned one) but worth a one-line note
   so a future reader doesn't chase the "duplicate".
7. **Known limitation to state, not fix:** if the orchestrator dies before
   writing any report, the job shows in-flight until `find_stale_running` trips
   at `job.deadline_ms` (~7h). Matches today's behaviour (`design.md:347-356`
   incident) — acceptable, but say so explicitly so it's a documented limit not
   a surprise.

---

## Blunt FINAL verdict: **MOSTLY SOUND**

The architecture (detached dispatch, reconcile-later, `kind` enum scoped to two
values, redirected-not-skipped guard, injectable launcher, launch-then-record)
is correct and implementable against `tick.py` / `runner.py` / `cli.py` /
`config.py` as they stand. Six of eight pass-2 residuals are genuinely closed,
not name-checked. Framing is honest ("scheduler, not engine").

It is **not quite SOUND** because two contracts are specified on one side only:

1. **The `## Outcome:` marker** (AC #5, reconcile logic) is referenced as if the
   orchestrator already writes it. It does not. Add a scoped task + AC to make
   `orchestrator-prompt.md` emit `## Outcome: ok|failed|partial (deadline
   exceeded)` on its terminal branches, and carve that out of the "no prompt
   edits" Non-goal.
2. **`$PIPELINE_REPORT`** — the orchestrator's actual report token — is not in
   the `substitute_prompt` contract (AC #5 only lists `$REPORT` /
   `$ROUTINE_REPORT`). Either add `$PIPELINE_REPORT` to the substitution or
   rename it in the prompt; AC #5 must assert the token the orchestrator
   receives, end to end.

Plus three small tightenings: `_cmd_run` must branch on `kind` for the
non-resume path too; `catch_up_minutes` default-120 inheritance needs a rule;
`RuntimeMaxSec` (seconds) not `--timeout` (and not ms) in the generated
invocation and AC #4.

Close those two contracts and the three tightenings — all spec text, no
redesign — and this is SOUND and ready to implement.
