# Spec — Unify routines + pipeline as one gated-workflow engine — kind becomes the single dispatch key (049) — 20260910T050000Z

Per-run spec at `docs/pipeline/runs/20260910T050000Z/spec.md` (G-15: per-run path avoids PR #28/#29 shared-path conflict — `docs/pipeline/design.md:79`). Implements `docs/process/issues/049-unify-routines-pipeline-as-one-gated-workflow-engine.md`.

## Problem

The pipeline is not a distinct engine. Per ROADMAP investigation (2026-08-30) and issue 049: a routine and the pipeline are the same gated-workflow engine at two scales — differing only in prompt (single-agent vs orchestrator that spawns `pl-1..pl-6`). Two of the bullet's three claims are done: scheduling unification (issue 026 / PR #79 — pipeline runs as `kind: pipeline` in the recurring tick, sharing `$REPORT` + `## Outcome:` contract) and a first code-level gate (PR #109 — G-17 `validate_stage_sessions` in tick).

The missing piece is engine-model unification. `_process_job` (`src/herdr_routines/tick.py:1248`) still branches on **two axes**: `job.kind == "pipeline"` for the pipeline and `job.checks is not None` (`tick.py:1260`) for a gated routine. "Is this job a gated workflow?" has no single answer. `checks is not None` is an implicit mode flag — nothing in the schema forces a reader to learn it, and a caller must encode `kind == "pipeline"` *or* `checks is not None` twice to cover the same concept. This is the deferred migration issue 026 named explicitly: *"Full `kind:` SSOT that retires `checks is not None` is a separate migration issue"* (Design § Mode discriminator).

## Approach

Make `kind` the exhaustive single source of truth for how a job is dispatched. Three `kind` values, mutually exclusive validation, pure three-way dispatch in `tick`. Rewires *which* function runs, not what it does.

### 1. Config schema (`src/herdr_routines/config.py`)

- Widen `VALID_JOB_KINDS` (`config.py:83`) from `{"routine", "pipeline"}` to `{"routine", "gated", "pipeline"}`. `_JOB_DEFAULTS["kind"] = "routine"` (`config.py:166`) and the per-job merge (`config.py:514` `{**_JOB_DEFAULTS, **defaults, **raw_job}`) stay untouched — a job that writes neither `kind` nor `checks` stays a plain routine.
- Neither `kind` nor `checks` enters `_DEFAULTS_ALLOWED_KEYS` (`config.py:102-116`) — both remain per-job only, so `defaults.yaml` can neither set nor inherit them.
- `Job.kind` (`config.py:237`) typed `Literal["routine", "gated", "pipeline"]` — real type-narrow verified by mypy, not a widened `str`.
- New validation in the existing `kind:` block (`config.py:739-759`):
  - `kind: gated` requires non-empty `checks` list.
  - `checks` present while `kind` resolves to `"routine"` (explicit or default) → `ConfigError` with migration message `f"{label}: 'checks' requires kind: gated (a job with checks is a gated workflow; add kind: gated)"`. Fail loud, no silent re-classification, no partial-load.
  - `kind: pipeline` keeps rejecting `checks` (message unchanged, `config.py:750-754`).
  - Unknown `kind` → `ConfigError` over the widened set.
- History/status compat: `kind` never serializes into `history.jsonl`. Records store name/state/run_id/outcome extras (runner.py); `status`/`scheduled` join live config + history by job name only (`cli.py:426-427` `for job in config.jobs: … last_terminal_run(history_path, job.name)` and `cli.py:475` `build_scheduled_rows`). No history-format change. State this in `Job.kind` field comment.

### 2. Tick dispatch (`src/herdr_routines/tick.py:1248` `_process_job`)

Replace the two-axis branch table with pure three-way dispatch on `kind`:

```python
if job.kind == "pipeline":
    return _process_pipeline_job(job, history_path, client=client, now=now)
if job.kind == "gated":
    return _process_gated_job(job, history_path, client=client, now=now)
# kind == "routine" — plain unconditional path (execute_run)
```

- Delete `if job.checks is not None:` (`tick.py:1260`). `_process_gated_job` asserts (`assert job.checks is not None`, `tick.py:153/:552`) stay as belt-and-braces, now guaranteed by config validation.
- Gate-reconcile machinery inside `_process_gated_job` unchanged.

### 3. Retire the last implicit `checks`-as-discriminator uses

After §1's validation `non-null checks ⟺ kind == "gated"` holds, so three remaining discriminators are re-keyed behavior-identically to `kind == "gated"`:

- `config.py:722` `if checks is not None and target is None:` → `if kind == "gated" and target is None:` (target inference), and the `target == "base"` guard at `config.py:725-732` keys on `kind == "gated"` too.
- `cli.py:704` `if job.checks is not None and job.target is not None:` → `if job.kind == "gated" …`.
- `$ROUTINE_REPORT` validate warning (`cli.py:624`) keys off `job.checks is None` → `job.kind != "gated"` (a gated job never trips it, matching today where `kind: pipeline` already bypasses via `if job.kind == "pipeline"` at `cli.py:616`).

After re-key, repo-wide `checks is not None` / `checks is None` grep is clean except the two intentional asserts in `_process_gated_job` (`tick.py:153/:552`) and checks-presence validation in `config.py` parse. Scope note: warning's token set (`$ROUTINE_REPORT` only) stays untouched — expanding to `$REPORT` is a separate hygiene item.

### 4. Ship config migration

Two committed files carry a `checks:`-bearing job and must gain `kind: gated` in the same PR as §1 (otherwise §1 breaks `test_config.py:801`/`:1226`):

- `deploy/jobs.d/babysit-prs.yaml:19` — the committed `jobs.d/` job with `checks: [pr_health]` and no `kind`.
- `deploy/jobs.example.yaml:55` (active block) and `:79` (commented `repo-hygiene` block).
- `deploy/jobs.d/feature-pipeline.yaml:21` already `kind: pipeline`, unaffected. Grep confirms `deploy/jobs.d/*.yaml` babysit-prs is the only committed job with a `checks:` key (the `checks:` at `feature-pipeline.yaml:56` is a comment).
- `deploy/jobs.d/` is the example layout for issue 006's dir mode — also the Pi shape. Note that `defaults.yaml` cannot inherit `kind`/`checks`; Pi's live `jobs.d/babysit-prs.yaml` (host-specific, not committed) must gain `kind: gated` the same way. Unmigrated config: `validate`/`tick` raise the §1 `ConfigError`. Rollback = remove `kind: gated` from that one file. Runbook `docs/process/pi-update-runbook.md` gains one migration line; PR description points at it.

### Sizing

Single PR, three commit-shaped units: §1+§2 (validation + dispatch) + §3 (three re-keys) + §4 (example migration). ~1–2 worker-days; §1 without §4 would leave the committed `jobs.d/` example failing its own tests — hence atomic.

## Files touched

- `docs/pipeline/runs/20260910T050000Z/spec.md` — this file (per-run spec, G-15).
- `src/herdr_routines/config.py` — `VALID_JOB_KINDS` (`:83`), `_JOB_DEFAULTS`/`Job.kind` type (`:166`/`:237`), kind validation block (`:739-759`), target inference (`:722`/`:725-732`); keep `kind`/`checks` out of `_DEFAULTS_ALLOWED_KEYS`.
- `src/herdr_routines/tick.py` — `_process_job` dispatch (`:1248`); delete `checks is not None` branch (`:1260`); keep `_process_gated_job` asserts (`:153`/`:552`) and `validate_stage_sessions` untouched.
- `src/herdr_routines/cli.py` — target guard (`:704`) and `$ROUTINE_REPORT` warning (`:624`/` :616`) re-keyed to `kind == "gated"`.
- `deploy/jobs.d/babysit-prs.yaml:19` — add `kind: gated`.
- `deploy/jobs.example.yaml:55` and `:79` — add `kind: gated` to active and commented blocks.
- `docs/process/pi-update-runbook.md` — one live-config migration line for the Pi's `jobs.d/babysit-prs.yaml`.
- `tests/test_config.py`, `tests/test_tick.py`/`tests/test_gated.py`, `tests/test_cli.py` — acceptance coverage (see below); `mypy` on `src/` for the `Literal` narrow.

Not touched: `_process_gated_job`/`_process_pipeline_job` internals, gate budgets, report contract, detached launcher, `validate_stage_sessions` (PR #109).

## Risks

- **Validation without migration breaks committed example.** `deploy/jobs.d/babysit-prs.yaml` is parsed by `test_config.py:801`/`:1226`; widening validation before adding `kind: gated` makes those tests fail on their own fixture. Mitigated by atomic single-PR landing: §1 and §4 ship together; PR must be green on the committed `jobs.d/` layout, not just on synthetic test YAML.
- **Silent re-classification tempts a lenient compat path.** Old configs with `checks` but no `kind` could be auto-promoted to `gated`. Rejected: fail loud with the migration message. Behavior if Pi not migrated: `validate`/`tick` `ConfigError` with `kind: gated` + `checks` in message; rollback is removing the key from one file.
- **AST vs substring for the dispatch seam.** A naive `rg "checks"` in `_process_job` would false-positive on the `checks:` comment inside the function (`tick.py:1251-1254`). Validate via AST `If`-test nodes only (`ast` strips comments/strings), scoped to this one function — the two asserts in `_process_gated_job` intentionally remain and must not be flagged.
- **Defaults inheritance could reintroduce implicit mode.** Adding `kind`/`checks` to `_DEFAULTS_ALLOWED_KEYS` would let `defaults.yaml` set gate mode for all jobs implicitly. Keep both per-job only and pin with a negative test; `defaults.yaml` cannot inherit either.
- **Warning token set drift.** `$ROUTINE_REPORT` vs `$REPORT` alias predates issue 026; expanding the warning's token set is out of scope and must not creep into this migration.
- **Non-goal creep — per-stage pipeline checks.** Declarative `checks:` per stage (pipeline gates bullet) builds on this clean dispatch; this issue must not invent a `workflows/<name>.yaml` parser or stage driver.
- **Spec-path hygiene (G-15).** Must stay at `docs/pipeline/runs/<run_id>/spec.md`; writing to `$WT/spec.md` or `docs/pipeline/spec.md` reintroduces PR #28/#29 shared-path merge conflict.

## Acceptance criteria

Authoritative definition in `docs/process/issues/049-unify-routines-pipeline-as-one-gated-workflow-engine.md` § Acceptance criteria (11 items, each ends `Test: <name>`). Executable mapping:

1. `_process_job` dispatches on `job.kind` alone: gated → `_process_gated_job`, routine → plain `execute_run`, pipeline → `_process_pipeline_job`, with no `checks`-based branch. AST check: no `If` test node in the function body references `job.checks` — Test: test_process_job_dispatches_on_kind_only
2. A job with `checks` and no `kind: gated` (explicit `routine` or default) is a ConfigError whose message contains `"kind: gated"` and `"checks"` — never silently re-classified — Test: test_checks_without_gated_kind_rejected
3. `kind: gated` with missing or empty `checks` list is a ConfigError (message names the job and `checks`) — Test: test_gated_kind_requires_checks
4. `kind: pipeline` with `checks` stays rejected (message unchanged); `kind: gated` rejects none of the pipeline-only fields (`deadline_ms`, `prompt_file`) — Test: test_pipeline_rejects_checks_unchanged
5. An unknown `kind` value is a ConfigError over the widened set — Test: test_unknown_kind_rejected
6. `Job.kind` is typed `Literal["routine", "gated", "pipeline"]` and survives `mypy` on `src/` — Test: test_job_kind_is_literal
7. The three re-keyed discriminators behave identically: target inference (`config.py:722`/:725-732), `cli.py:704`, and the `$ROUTINE_REPORT` warning (`cli.py:624`) all key on `kind == "gated"`; no `checks is not None`/`checks is None` discriminators remain outside `_process_gated_job`'s two asserts and config.py's checks-presence validation — Test: test_discriminator_rekey_on_kind
8. Regression: plain no-`checks` routine still runs `execute_run`, still writes `$REPORT`/`$ROUTINE_REPORT`, `no_report` guard unchanged, and `kind: gated` reaches `_process_gated_job` with same checks/target/budget semantics as pre-migration — Test: test_routine_and_gated_regression_unchanged
9. jobs.d + defaults: a `kind: gated` job in a `jobs.d/<name>.yaml` file loads with non-empty `checks`, and neither `kind` nor `checks` is added to `_DEFAULTS_ALLOWED_KEYS` — Test: test_gated_job_jobs_d_roundtrip
10. Status/history compat: `status` renders a `kind: gated` job via normal join on job name (`cli.py:426-427`) with no history-format change and no `kind` key in any `history.jsonl` record — Test: test_status_renders_gated_job
11. Ship migration: `deploy/jobs.d/babysit-prs.yaml` and `deploy/jobs.example.yaml` (active and commented blocks) carry `kind: gated`; committed `jobs.d/` loads clean through `validate` (cf. `test_config.py:801`/`:1226`), and `deploy/jobs.d/*.yaml` babysit-prs is the only committed job with a `checks:` key — Test: test_example_config_kind_gated

Why these tests: 1–6 pin the new SSOT shape (kind-only dispatch AST-verified, three mutually-exclusive validations, type-narrow); 7–8 pin behavior-identical re-key and legacy regression; 9–11 pin the migration surfaces that would fail at merge time (committed `jobs.d/` example layout already test-parsed, `defaults.yaml` non-inheritance, `history.jsonl` by-name join).

## Non-goals

- Not declarative per-stage `checks:` for the pipeline (separate Roadmap item gated on this clean dispatch).
- Not changing `_process_gated_job` / `_process_pipeline_job` internals, budgets, report contract, detached launcher, or `validate_stage_sessions`.
- Not an optional "routine with `checks`" hybrid — issue 026's non-SSOT warned against.
- Not expanding `$ROUTINE_REPORT` warning's token set to `$REPORT`.

## Review

- blocking: items 1–5, 7, 11 — kind-only dispatch and mutually-exclusive validation plus the committed `jobs.d/` migration that would break PR CI if shipped without it.
- non-blocking: items 6, 8–10 — type-narrow mypy gate, regression parity, and history/status compat that are fully verified but not merge-gating for the SSOT shape.
- confidence: high for validation/dispatch/re-key/migration (config load, AST seam, grep clean, `last_terminal_run` by-name join); medium for mypy literal gate (relies on repo mypy gate rather than unit test alone).
