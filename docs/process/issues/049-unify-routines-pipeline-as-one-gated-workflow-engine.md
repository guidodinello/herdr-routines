---
id: "049"
title: "Unify routines + pipeline as one gated-workflow engine — kind becomes the single dispatch key"
status: open
priority: medium
area: pipeline
---

## Description

Promotes the ROADMAP Parking Lot bullet **"Unify routines + pipeline as one
'gated workflow' engine"** (2026-08-30 investigation). Its conclusion: the
pipeline is *not* a distinct engine — it is a single `herdr` agent whose
`docs/pipeline/orchestrator-prompt.md` spawns `pl-1..pl-6` workers via its own
`herdr` tool calls; a routine and the pipeline are **the same engine at two
scales**, differing only in the prompt. Both = a **gated workflow** (routine
= single-agent gated [has `checks`]; pipeline = multi-agent gated [has
`stages`, each with a gate]).

Two of the bullet's three claims are already built:

- **Scheduling unification** — issue 026 / PR #79: the pipeline runs as
  `kind: pipeline` in the recurring tick (detached systemd-run dispatch),
  sharing the routine report contract (`$REPORT` + `## Outcome:` marker).
- **A first code-level gate** — PR #109: the G-17 stage-independence gate is
  enforced in `tick` (`validate_stage_sessions`), no longer prompt-only.

What the bullet claims and *isn't* built is the **engine-model unification**:
the two scales are still discriminated by **two different axes**. `_process_job`
(tick.py:1248) branches on `job.kind == "pipeline"` for the pipeline and on
`job.checks is not None` for a gated routine (tick.py:1260). "Is this job a
gated workflow?" has no single answer — it is "pipeline", or "has checks",
depending on which branch of the code asks. Issue 026 explicitly deferred
this: *"Full `kind:` SSOT that retires `checks is not None` is a separate
migration issue"* (026, Design §"Mode discriminator").

This issue is that deferred migration: make **`kind` the exhaustive, single
source of truth** for *how* a job is dispatched, so `tick` behaves identically
toward a gated routine and a pipeline (both are a "gated workflow"; the only
difference is one agent vs. an orchestrator that spawns several). It is the
code shape of the bullet, and it is deliberately **not** the "Code-level
pipeline gates" bullet (declarative per-stage `checks`) — that one builds on
this clean dispatch, and stays its own item.

### Why it is a real refactor, not cosmetics

The two-axis dispatch is exactly the kind of "plausible contract nobody
exercises against the real mechanism" that has bitten this repo before
(issues 038 / 042 / 047). `checks is not None` is an *implicit* mode flag:
nothing in the config schema forces a reader to learn that gate mode is
expressed by presence of `checks`, and a caller has to encode
`kind == "pipeline"` *or* `checks is not None` to cover the same conceptual
path twice. After this issue there is one key, three values, and a branch
table a single worker can hold in its head.

## Design (proposal)

Extend `kind` (today `VALID_JOB_KINDS = {"routine", "pipeline"}`, config.py:83)
to `{"routine", "gated", "pipeline"}` and make the three paths mutually
exclusive by validation, so `tick` never inspects `checks` to decide *how* to
run a job. The value is **locked to `kind: gated`** (matching
`_process_gated_job` and the bullet's "both = a gated workflow"); issue 026's
note floated `kind: gate` as a strawman, not a contract — it is not an option
here.

### 1. Config schema (`config.py`)

- `kind` gains the value `"gated"` (widened `VALID_JOB_KINDS`,
  config.py:83). `_JOB_DEFAULTS["kind"] = "routine"` (config.py:166) and the
  per-job merge (config.py:514, `{**_JOB_DEFAULTS, **defaults, **raw_job}`)
  stay untouched — a job that writes neither `kind` nor `checks` stays a plain
  routine. Neither `kind` nor `checks` is in `_DEFAULTS_ALLOWED_KEYS`
  (config.py:102-116) and this issue keeps it that way: both are per-job keys
  only, so `defaults.yaml` can neither set nor inherit them.
- `Job.kind` (config.py:237) is typed `Literal["routine", "gated", "pipeline"]`
  — a real type-narrow, verified by mypy on the diff, not just a widened string
  set. `Job.checks` keeps `tuple[GateCheck, ...] | None` (None only until
  `gated` guarantees non-empty).
- New validation rules, applied in the existing `kind:` block
  (config.py:739-759, which already mixes `raw_job`-keyed and merged-value
  checks appropriately):
  - `kind: gated` **requires** a non-empty `checks` list.
  - `checks` present while `kind` resolves to `"routine"` — whether written
    explicitly or left at the `_JOB_DEFAULTS` dataclass default — is a
    **ConfigError** whose message is the migration instruction:
    `f"{label}: 'checks' requires kind: gated (a job with checks is a gated workflow; add kind: gated)"`.
    No silent re-classification: the error is how an old config learns it must
    be migrated. Load **fails loudly** (the existing ConfigError path); there
    is no partial-load and no silent fallback.
  - `kind: pipeline` keeps rejecting `checks` (message unchanged,
    config.py:750-754).
  - Any other `kind` value stays a ConfigError (config.py:746-747), now over
    the widened set.
- History/status compat: `kind` never serializes into `history.jsonl`. History
  records store job name / state / run_id / outcome extras (runner.py), and the
  render commands join live config + history **by job name only** —
  `_cmd_status` (cli.py:426-427 `for job in config.jobs: …
  last_terminal_run(history_path, job.name)`) and `_cmd_scheduled`
  (cli.py:475 `build_scheduled_rows(config, default_history_path(), …)`). So no
  history-format change, no `status`/`digest` migration. State this in the
  `Job.kind` field comment so the next reader does not "fix" it.

### 2. Tick dispatch (`tick.py` `_process_job`, :1248)

Replace the two-axis branch table with a pure three-way dispatch on `kind`:

```python
if job.kind == "pipeline":
    return _process_pipeline_job(job, history_path, client=client, now=now)
if job.kind == "gated":
    return _process_gated_job(job, history_path, client=client, now=now)
# kind == "routine" — the plain unconditional path below
```

- `if job.checks is not None:` (tick.py:1260) is deleted; `_process_gated_job`'s
  asserts (`assert job.checks is not None`, tick.py:153/:552) stay as belt-and-
  braces now guaranteed by config validation.
- The gate-reconcile machinery inside `_process_gated_job` is unchanged — this
  issue rewires *which* function runs, not what it does.

### 3. Retire the last implicit `checks`-as-discriminator uses

`checks is not None` remains as a *discriminator* in three places beyond
`_process_job`; all are re-keyed to `kind == "gated"` (behavior-identical after
§1's validation, which guarantees non-null `checks` ⟺ `kind == "gated"`):

- `config.py:722` — `if checks is not None and target is None:` → `if kind ==
  "gated" and target is None:` (target inference), and the `target == "base"`
  guard at config.py:725-732 keys on `kind == "gated"` too.
- `cli.py:704` — `if job.checks is not None and job.target is not None:` →
  `if job.kind == "gated" ...`.
- The `$ROUTINE_REPORT` validate warning (cli.py:624) keys off
  `job.checks is None` to identify "plain, report-producing" jobs → `job.kind !=
  "gated"`, so a gated job never trips it, exactly as today (`kind: pipeline`
  already bypasses via the enclosing `if job.kind == "pipeline"` at cli.py:616).

After the re-key, a repo-wide `checks is not None` / `checks is None` grep is
clean **except** for the two intentional asserts inside `_process_gated_job`
(tick.py:153/:552) and the checks-presence validation in config.py's parse.

> Scope note: the warning's *token set* (`$ROUTINE_REPORT` only, predating
> issue 026's `$REPORT` alias) is left untouched — expanding it to also check
> `$REPORT` is a separate hygiene item, not this issue's discriminator fix.

### 4. Ship config migration

Two committed files carry a `checks:`-bearing job and must gain `kind: gated`;
there are **no other committed `checks`-bearing jobs** (grep `deploy/`):

- `deploy/jobs.d/babysit-prs.yaml:19` — the **committed `jobs.d/` job with
  `checks: [pr_health]` and no `kind`**. It is parsed by
  `test_config.py:801`/`:1226` (the committed `jobs.d/` example layout), so
  after §1's validation an unpatched copy would **fail those tests' load** —
  it must be patched in the same PR as the validation change.
- `deploy/jobs.example.yaml:55` — the monolith example's `babysit-prs` block,
  plus the commented-out `repo-hygiene` block at :79.
- `deploy/jobs.d/feature-pipeline.yaml:21` already carries `kind: pipeline`
  and is unaffected.
- `deploy/jobs.d/` is the **example layout for issue 006's dir mode** — this is
  also the host shape the Pi runs. Sufficient as a commented note:
  `defaults.yaml` cannot inherit `kind`/`checks` (not in
  `_DEFAULTS_ALLOWED_KEYS`); the Pi's live `jobs.d/babysit-prs.yaml` (host-
  specific, not committed) must gain `kind: gated` the same way. Behavior if
  not migrated on any host: **fail loud** — `validate`/`tick` raise the §1
  ConfigError carrying the migration message. Rollback = remove `kind: gated`
  from that one file; nothing is scripted at runtime. The merge-order runbook
  ([`docs/process/pi-update-runbook.md`](../pi-update-runbook.md)) is where
  this live-config step is recorded — the PR description points at it; the
  runbook gains one line.

### Sizing

A single PR: §1 + §2 (validation + dispatch) are one commit-sized unit, §3
(the three re-keys) a second, §4 (example migration) a third. ~1–2 worker-
days; the migration files are what make it atomic — §1 without §4 leaves the
committed `jobs.d/` example failing its own tests.

Files: `src/herdr_routines/config.py` (`VALID_JOB_KINDS`, kind validation
block, `Job.kind` type), `src/herdr_routines/tick.py` (`_process_job`
dispatch), `src/herdr_routines/cli.py` (`:704` target guard, `:624` report
warning), `deploy/jobs.d/babysit-prs.yaml` (+`kind: gated`),
`deploy/jobs.example.yaml` (active + commented blocks), the corresponding
tests, `docs/process/pi-update-runbook.md` (one migration line).

## Non-goals

- **Not** the "Code-level pipeline gates" bullet: declarative per-stage
  `checks:` for the pipeline (a `workflows/<name>.yaml` parser / Python stage
  driver) stays its own Roadmap item, gated on this issue's clean dispatch.
- **Not** changing `_process_gated_job` or `_process_pipeline_job` internals:
  gate budgets, the report contract, the detached launcher, and the stage-
  independence reconcile (PR #109) are untouched.
- **Not** a "routine with `checks` optionally" clause — that is the non-SSOT
  hybrid issue 026 warned about (tick would still need `checks is not None`).
- **Not** expanding the `$ROUTINE_REPORT` warning's token set to `$REPORT`
  aliases (separate hygiene item).

## Acceptance criteria

Each ends `Test: <name>`:

1. `_process_job` dispatches on `job.kind` alone: `gated` → `_process_gated_job`,
   `routine` → the plain `execute_run` path, `pipeline` → `_process_pipeline_job`,
   with no `checks`-based branch. Checkable seam (fragile-guard): parse
   `inspect.getsource(_process_job)` with `ast` and assert **no `If` test node in
   the function's body references `job.checks`** (comment/string mentions are
   irrelevant — AST strips them; the current in-function comment at
   tick.py:1251-1254 mentions `checks:` and must not trip the test). Scoped to
   this one function — a wider "no `checks` in `tick.py`" grep is *not* the
   contract (the two `assert job.checks is not None` inside `_process_gated_job`,
   tick.py:153/:552, intentionally remain).
   `Test: test_process_job_dispatches_on_kind_only`
2. A job with `checks` and no `kind: gated` (explicit `routine` or left at the
   `_JOB_DEFAULTS` default) is a ConfigError whose message contains
   `"kind: gated"` and `"checks"` — never silently re-classified.
   `Test: test_checks_without_gated_kind_rejected`
3. `kind: gated` with missing or empty `checks` list is a ConfigError (message
   names the job and `checks`).
   `Test: test_gated_kind_requires_checks`
4. `kind: pipeline` with `checks` stays rejected (message unchanged);
   `kind: gated` rejects none of the pipeline-only fields (`deadline_ms`,
   `prompt_file`) it doesn't own.
   `Test: test_pipeline_rejects_checks_unchanged`
5. An unknown `kind` value is a ConfigError over the widened set.
   `Test: test_unknown_kind_rejected`
6. `Job.kind` is typed `Literal["routine", "gated", "pipeline"]` and the change
   survives `mypy` on `src/` (no `str`-index fallback needed by callers).
   `Test` (mypy via the repo gate, plus a static src assertion):
   `Test: test_job_kind_is_literal`
7. The three re-keyed discriminators behave identically: target inference
   (config.py:722/:725-732), `cli.py:704`, and the `$ROUTINE_REPORT` warning
   (cli.py:624) all key on `kind == "gated"`; a grep of `src/herdr_routines/`
   finds no `checks is not None`/`checks is None` discriminators left outside
   `_process_gated_job`'s two intentional asserts and config.py's checks-
   presence validation (the boundary that parses `checks` at all).
   `Test: test_discriminator_rekey_on_kind`
8. Regression: a plain no-`checks` routine still runs `execute_run`, still
   writes `$REPORT`/`$ROUTINE_REPORT`, the `no_report` guard is unchanged, and
   a `kind: gated` job reaches `_process_gated_job` with the same
   checks/target/budget semantics as pre-migration (existing gated-path tests
   pass against the re-keyed discriminator).
   `Test: test_routine_and_gated_regression_unchanged`
9. jobs.d + defaults: a `kind: gated` job in a `jobs.d/<name>.yaml` file loads
   with a non-empty `checks`, and neither `kind` nor `checks` is added to
   `_DEFAULTS_ALLOWED_KEYS` (`defaults.yaml` cannot inherit or set either).
   `Test: test_gated_job_jobs_d_roundtrip`
10. Status/history compat: `status` renders a `kind: gated` job through the
    normal join on job name (cli.py:426-427) with no history-format change and
    no `kind` key in any `history.jsonl` record.
    `Test: test_status_renders_gated_job`
11. Ship migration: `deploy/jobs.d/babysit-prs.yaml` and
    `deploy/jobs.example.yaml` (both the active block and the commented
    `repo-hygiene` block) carry `kind: gated`; the committed `jobs.d/`
    directory loads clean through `validate` (cf. `test_config.py:801`/`:1226`),
    and a grep of `deploy/jobs.d/*.yaml` confirms babysit-prs is the **only**
    committed job with a `checks:` key (the `checks:` mention in
    `feature-pipeline.yaml:56` is a comment), so no other file needs the key.
    `Test: test_example_config_kind_gated`

## Why these tests

- 1–6 pin the new SSOT shape itself: kind-only dispatch (AST-verified, immune
  to comment drift), the three mutually-exclusive validation rules, and the
  type-narrow — the things a naive port of `checks is not None` would quietly
  undo.
- 7–8 pin that the re-key is provably behavior-identical (the whole point of
  an SSOT refactor) and that no legacy routine/gated behavior moved.
- 9–11 pin the two migration surfaces that would otherwise fail at merge time:
  the committed `jobs.d/` example layout (already test-parsed) and the
  `defaults.yaml` non-inheritance.

## Log

- **2026-09-08**: refined from the ROADMAP Parking Lot bullet "Unify routines +
  pipeline as one 'gated workflow' engine" (issue 029). Scope narrowing derived
  from the repo record: scheduling + report unification landed (026 / PR #79),
  first code-level gate landed (PR #109); the remaining buildable slice is the
  `kind:` single-source-of-truth dispatch 026 explicitly deferred, plus the
  example-config migration.
- **2026-09-08** (review pass 1, muse-spark, `confidence: medium`): folded in —
  locked `kind: gated` (dropped the open naming question), AST-based seam for
  AC 1 (a substring check would false-positive on the `checks:` comment inside
  `_process_job`), quoted ConfigError message contracts, fail-loud + rollback
  behavior for unmigrated configs, `$REPORT`-token-set scope note, history/
  status citation, and the denied `_DEFAULTS_ALLOWED_KEYS` inheritance.
- **2026-09-08** (review pass 2, muse-spark, `confidence: medium`): folded in —
  the missed committed `deploy/jobs.d/babysit-prs.yaml` (a `checks:` job with
  no `kind`, parsed by `test_config.py:801`/`:1226`, so it would break the
  tests' load); rewrote the Pi migration narrative from a monolithic `jobs.yaml`
  to issue 006's dir mode (`jobs.d/<name>.yaml`, `defaults.yaml`
  non-inheritance); tightened AC 1's seam to AST `If`-test nodes (comments are
  stripped) instead of a naive substring; cited `_cmd_status`/`_cmd_scheduled`
  (cli.py:426-427/:475) for the by-name history join; added the
  `Literal["routine","gated","pipeline"]` type-narrow as AC 6 with a mypy
  gate; consolidated 13 ACs down to 11 (regression + re-key grouped).
- **2026-09-08** (review pass 3, muse-spark, `confidence: medium`, loop capped):
  folded in — `Files:` inventory line + single-PR sizing note (the three
  commit-shaped units and why §1 without §4 breaks the committed example's own
  tests), AC 1/AC 7 negative guards scoped to what is actually the contract
  (the one function / `src/herdr_routines/`, with the two intentional
  `_process_gated_job` asserts called out rather than forbidden), AC 11's grep
  narrowed to `deploy/jobs.d/*.yaml`, and the Pi live-config migration tied to
  `docs/process/pi-update-runbook.md`. Pass-3's residual notes (overall size
  vs. peer issues, title length) are carried in the refining PR's body.