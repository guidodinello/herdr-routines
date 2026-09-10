# Spec — Unify routines + pipeline as one gated-workflow engine — kind becomes the single dispatch key (049) — 20260910T050000Z

Per-run spec at `docs/pipeline/runs/20260910T050000Z/spec.md` (G-15: per-run path avoids PR #28/#29 shared-path conflict — `docs/pipeline/design.md:79`). Implements `docs/process/issues/049-unify-routines-pipeline-as-one-gated-workflow-engine.md`.

## Problem

The pipeline is not a distinct engine. Per ROADMAP investigation (2026-08-30) and issue 049: a routine and the pipeline are the same gated-workflow engine at two scales — differing only in prompt (single-agent vs orchestrator that spawns `pl-1..pl-6`). Two of the bullet's three claims are done: scheduling unification (issue 026 / PR #79 — pipeline runs as `kind: pipeline` in the recurring tick, sharing `$REPORT` + `## Outcome:` contract) and a first code-level gate (PR #109 — G-17 `validate_stage_sessions` in tick).

The missing piece is engine-model unification. `_process_job` (`src/herdr_routines/tick.py:1250`) still branches on **two axes**: `job.kind == "pipeline"` for the pipeline and `job.checks is not None` (`tick.py:1262`) for a gated routine. "Is this job a gated workflow?" has no single answer. `checks is not None` is an implicit mode flag — nothing in the schema forces a reader to learn it, and a caller must encode `kind == "pipeline"` *or* `checks is not None` twice to cover the same concept. This is the deferred migration issue 026 named explicitly: *"Full `kind:` SSOT that retires `checks is not None` is a separate migration issue"* (Design § Mode discriminator). It is the same "plausible contract nobody exercises against the real mechanism" failure mode that has bitten this repo before (issues 038/042/047) — review note `confidence: high` that the two-axis shape is the root, not cosmetics.

After the fix there is one key, three values, and a branch table a single worker can hold in its head: `routine` (plain `execute_run`), `gated` (gate checks + fix dispatch), `pipeline` (detached `systemd-run` launch). This is deliberately **not** the "Code-level pipeline gates" bullet (declarative per-stage `checks:` for the pipeline); that builds on this clean dispatch and stays its own item — `confidence: high`.

## Approach

Make `kind` the exhaustive single source of truth for how a job is dispatched. Three `kind` values, mutually exclusive validation, pure three-way dispatch in `tick`. Rewires *which* function runs, not what it does. `confidence: high` — validation + dispatch + re-key were verified against the base commit `c0d5054` (line anchors below are from that commit).

### 1. Config schema (`src/herdr_routines/config.py`)

- Widen `VALID_JOB_KINDS` (`config.py:83`) from `{"routine", "pipeline"}` to `{"routine", "gated", "pipeline"}`. `_JOB_DEFAULTS["kind"] = "routine"` (`config.py:166`) and the per-job merge (`config.py:514` `{**_JOB_DEFAULTS, **defaults, **raw_job}`) stay untouched — a job that writes neither `kind` nor `checks` stays a plain routine.
- Neither `kind` nor `checks` enters `_DEFAULTS_ALLOWED_KEYS` (`config.py:102-116`) — both remain per-job only, so `defaults.yaml` can neither set nor inherit them. Pin with a negative test; `confidence: high`.
- `Job.kind` (`config.py:237`, currently `kind: str = "routine"`) narrowed to `Literal["routine", "gated", "pipeline"]` — real type-narrow verified by `mypy` on `src/`, not a widened `str`. Field comment must state history compat (see below). `confidence: medium` — relies on the repo's mypy gate rather than a unit test alone.
- New validation in the existing `kind:` block (`config.py:739-759`, anchors at `c0d5054`; body now spans `config.py:745-802` post-edit):
  - `kind: gated` requires non-empty `checks` list.
  - `checks` present while `kind` resolves to `"routine"` (explicit or default) → `ConfigError` with migration message `f"{label}: 'checks' requires kind: gated (a job with checks is a gated workflow; add kind: gated)"`. Fail loud, no silent re-classification, no partial-load. Covers both `jobs.yaml` and `jobs.d/<name>.yaml` via `_build_job`.
  - `kind: pipeline` keeps rejecting `checks` (message unchanged, `config.py:750-754`).
  - Unknown `kind` → `ConfigError` over the widened set.
- History/status compat: `kind` never serializes into `history.jsonl`. Records store name/state/run_id/outcome extras (runner.py); `status`/`scheduled` join live config + history by job name only (`cli.py:432-434` `for job in config.jobs: … last_terminal_run(history_path, job.name)` and `cli.py:481` `build_scheduled_rows`). No history-format change. State this in `Job.kind` field comment so the next reader does not "fix" it.

### 2. Tick dispatch (`src/herdr_routines/tick.py:1250` `_process_job`)

Replace the two-axis branch table with pure three-way dispatch on `kind`:

```python
if job.kind == "pipeline":
    return _process_pipeline_job(job, history_path, client=client, now=now)
if job.kind == "gated":
    return _process_gated_job(job, history_path, client=client, now=now)
# kind == "routine" — plain unconditional path (execute_run)
```

- Delete `if job.checks is not None:` (`tick.py:1262`). `_process_gated_job` asserts (`assert job.checks is not None`, `tick.py:153/:552`) stay as belt-and-braces, now guaranteed by config validation. `confidence: high` — AST seam verified.
- Gate-reconcile machinery inside `_process_gated_job` unchanged. `confidence: high`.

### 3. Retire the last implicit `checks`-as-discriminator uses

After §1's validation `non-null checks ⟺ kind == "gated"` holds, so three remaining discriminators are re-keyed behavior-identically to `kind == "gated"`:

- `config.py:722` `if checks is not None and target is None:` → `if kind == "gated" and target is None:` (target inference), and the `target == "base"` guard at `config.py:725-732` keys on `kind == "gated"` too.
- `cli.py:710` `if job.checks is not None and job.target is not None:` → `if job.kind == "gated" …`.
- `$ROUTINE_REPORT` validate warning (`cli.py:630` `elif job.enabled and job.checks is None and "$ROUTINE_REPORT" not in job.prompt:`) — currently the `if job.kind == "pipeline":` branch at `cli.py:622` already bypasses pipeline jobs; the `elif` must also bypass gated jobs → key on `job.kind != "gated"` (or equivalently extend the guard to `job.kind in ("pipeline","gated")` skip). A gated job never trips it, matching today where `kind: pipeline` already bypasses. `confidence: high` — logic is the same after §1 invariant.

After re-key, repo-wide `checks is not None` / `checks is None` grep is clean except the two intentional asserts in `_process_gated_job` (`tick.py:153/:552`) and checks-presence validation in `config.py` parse (the boundary that parses `checks` at all). Scope note: warning's token set (`$ROUTINE_REPORT` only) stays untouched — expanding to `$REPORT` is a separate hygiene item. `confidence: high`.

### 4. Ship config migration

Two committed files carry a `checks:`-bearing job and must gain `kind: gated` in the same PR as §1 (otherwise §1 breaks `test_config.py:801`/`:1226`):

- `deploy/jobs.d/babysit-prs.yaml:19` — the committed `jobs.d/` job with `checks: [pr_health]` and no `kind`.
- `deploy/jobs.example.yaml:55` (active block) and `:79` (commented `repo-hygiene` block) — both gain `kind: gated`.
- `deploy/jobs.d/feature-pipeline.yaml:21` already `kind: pipeline`, unaffected. Grep confirms `deploy/jobs.d/*.yaml` babysit-prs is the only committed job with a `checks:` key (the `checks:` at `feature-pipeline.yaml:56` is a comment).
- `deploy/jobs.d/` is the example layout for issue 006's dir mode — also the Pi shape. Note that `defaults.yaml` cannot inherit `kind`/`checks`; Pi's live `jobs.d/babysit-prs.yaml` (host-specific, not committed) must gain `kind: gated` the same way. Unmigrated config: `validate`/`tick` raise the §1 `ConfigError`. Rollback = remove `kind: gated` from that one file. Runbook `docs/process/pi-update-runbook.md` gains one migration line; PR description points at it. `confidence: high` — migration is what makes §1 atomically green on the committed `jobs.d/` layout.

### Sizing

Single PR, three commit-shaped units: §1+§2 (validation + dispatch) + §3 (three re-keys) + §4 (example migration). ~1–2 worker-days; §1 without §4 would leave the committed `jobs.d/` example failing its own tests — hence atomic. `confidence: high`.

## Files touched

- `docs/pipeline/runs/20260910T050000Z/spec.md` — this file (per-run spec, G-15). Line anchors in this section are from base commit `c0d5054`.
- `src/herdr_routines/config.py` — `VALID_JOB_KINDS` (`:83`), `_JOB_DEFAULTS`/`Job.kind` type (`:166`/`:237` → `Literal`), kind validation block (`:745-802` post-edit, was `:739-759` at `c0d5054`), target inference (`:722`/`:725-732`); keep `kind`/`checks` out of `_DEFAULTS_ALLOWED_KEYS` (`:102-116`).
- `src/herdr_routines/tick.py` — `_process_job` dispatch (`:1250`); delete `checks is not None` branch (`:1262`); keep `_process_gated_job` asserts (`:153`/`:552`) and `validate_stage_sessions` untouched.
- `src/herdr_routines/cli.py` — target guard (`:710`) and `$ROUTINE_REPORT` warning (`:630` `elif` / `:622` pipeline guard) re-keyed to `kind == "gated"`.
- `deploy/jobs.d/babysit-prs.yaml:19` — add `kind: gated`.
- `deploy/jobs.example.yaml:55` and `:79` — add `kind: gated` to active and commented blocks.
- `docs/process/pi-update-runbook.md` — one live-config migration line for the Pi's `jobs.d/babysit-prs.yaml`.
- `tests/test_config.py`, `tests/test_tick.py`/`tests/test_gated.py`, `tests/test_cli.py` — acceptance coverage (see below); `mypy` on `src/` for the `Literal` narrow.

Not touched: `_process_gated_job`/`_process_pipeline_job` internals, gate budgets, report contract, detached launcher, `validate_stage_sessions` (PR #109), `history.jsonl` format, `runner.py`.

## Risks

- **Validation without migration breaks committed example.** `deploy/jobs.d/babysit-prs.yaml` is parsed by `test_config.py:801`/`:1226`; widening validation before adding `kind: gated` makes those tests fail on their own fixture. Mitigated by atomic single-PR landing: §1 and §4 ship together; PR must be green on the committed `jobs.d/` layout, not just on synthetic test YAML. `confidence: high` — verified that babysit-prs is the only committed `checks`-bearing job (`deploy/jobs.d/*.yaml` grep).
- **Silent re-classification tempts a lenient compat path.** Old configs with `checks` but no `kind` could be auto-promoted to `gated`. Rejected: fail loud with the migration message. Behavior if Pi not migrated: `validate`/`tick` `ConfigError` with `kind: gated` + `checks` in message; rollback is removing the key from one file. `confidence: high`.
- **AST vs substring for the dispatch seam.** A naive `rg "checks"` in `_process_job` would false-positive on the `checks:` comment inside the function (`tick.py:1251-1254`). Validate via AST `If`-test nodes only (`ast` strips comments/strings), scoped to this one function — the two asserts in `_process_gated_job` intentionally remain and must not be flagged. `confidence: high`.
- **Defaults inheritance could reintroduce implicit mode.** Adding `kind`/`checks` to `_DEFAULTS_ALLOWED_KEYS` would let `defaults.yaml` set gate mode for all jobs implicitly. Keep both per-job only and pin with a negative test; `defaults.yaml` cannot inherit either. `confidence: high`.
- **Warning token set drift.** `$ROUTINE_REPORT` vs `$REPORT` alias predates issue 026; expanding the warning's token set is out of scope and must not creep into this migration. `confidence: high`.
- **Non-goal creep — per-stage pipeline checks.** Declarative `checks:` per stage (pipeline gates bullet) builds on this clean dispatch; this issue must not invent a `workflows/<name>.yaml` parser or stage driver. `confidence: high`.
- **Mypy literal narrow surface.** Narrowing `Job.kind` to `Literal` could surface new `mypy` errors where callers compare `kind` as plain `str` or index with a dynamic string. Risk is low (only three call sites key on `kind` today, all `==` checks) but the PR must run `mypy` on `src/` green; fallback is to keep the `Literal` and fix call sites, not widen back to `str`. `confidence: medium`.
- **Spec-path hygiene (G-15).** Must stay at `docs/pipeline/runs/<run_id>/spec.md`; writing to `$WT/spec.md` or `docs/pipeline/spec.md` reintroduces PR #28/#29 shared-path merge conflict. `confidence: high`.

## Acceptance criteria

Authoritative definition in `docs/process/issues/049-unify-routines-pipeline-as-one-gated-workflow-engine.md` § Acceptance criteria (11 items, each ends `Test: <name>`). Executable mapping — `confidence: high` that all 11 `Test: <name>` tokens from the issue appear below, none dropped (independently cross-checked):

1. `_process_job` dispatches on `job.kind` alone: gated → `_process_gated_job`, routine → plain `execute_run`, pipeline → `_process_pipeline_job`, with no `checks`-based branch. AST check: no `If` test node in the function body references `job.checks` — Test: test_process_job_dispatches_on_kind_only
2. A job with `checks` and no `kind: gated` (explicit `routine` or default) is a ConfigError whose message contains `"kind: gated"` and `"checks"` — never silently re-classified — Test: test_checks_without_gated_kind_rejected
3. `kind: gated` with missing or empty `checks` list is a ConfigError (message names the job and `checks`) — Test: test_gated_kind_requires_checks
4. `kind: pipeline` with `checks` stays rejected (message unchanged); `kind: gated` rejects none of the pipeline-only fields (`deadline_ms`, `prompt_file`) — Test: test_pipeline_rejects_checks_unchanged
5. An unknown `kind` value is a ConfigError over the widened set — Test: test_unknown_kind_rejected
6. `Job.kind` is typed `Literal["routine", "gated", "pipeline"]` and survives `mypy` on `src/` — Test: test_job_kind_is_literal
7. The three re-keyed discriminators behave identically: target inference (`config.py:722`/:725-732), `cli.py:710`, and the `$ROUTINE_REPORT` warning (`cli.py:630`) all key on `kind == "gated"`; no `checks is not None`/`checks is None` discriminators remain outside `_process_gated_job`'s two asserts and config.py's checks-presence validation — Test: test_discriminator_rekey_on_kind
8. Regression: plain no-`checks` routine still runs `execute_run`, still writes `$REPORT`/`$ROUTINE_REPORT`, `no_report` guard unchanged, and `kind: gated` reaches `_process_gated_job` with same checks/target/budget semantics as pre-migration — Test: test_routine_and_gated_regression_unchanged
9. jobs.d + defaults: a `kind: gated` job in a `jobs.d/<name>.yaml` file loads with non-empty `checks`, and neither `kind` nor `checks` is added to `_DEFAULTS_ALLOWED_KEYS` — Test: test_gated_job_jobs_d_roundtrip
10. Status/history compat: `status` renders a `kind: gated` job via normal join on job name (`cli.py:432-434`) with no history-format change and no `kind` key in any `history.jsonl` record — Test: test_status_renders_gated_job
11. Ship migration: `deploy/jobs.d/babysit-prs.yaml` and `deploy/jobs.example.yaml` (active and commented blocks) carry `kind: gated`; committed `jobs.d/` loads clean through `validate` (cf. `test_config.py:801`/`:1226`), and `deploy/jobs.d/*.yaml` babysit-prs is the only committed job with a `checks:` key — Test: test_example_config_kind_gated

Why these tests: 1–6 pin the new SSOT shape (kind-only dispatch AST-verified, three mutually-exclusive validations, type-narrow); 7–8 pin behavior-identical re-key and legacy regression; 9–11 pin the migration surfaces that would fail at merge time (committed `jobs.d/` example layout already test-parsed, `defaults.yaml` non-inheritance, `history.jsonl` by-name join).

Tiers:
- blocking: items 1–5, 7, 11 — kind-only dispatch and mutually-exclusive validation plus the committed `jobs.d/` migration that would break PR CI if shipped without it. `confidence: high`.
- non-blocking: items 6, 8–10 — type-narrow mypy gate, regression parity, and history/status compat that are fully verified but not merge-gating for the SSOT shape. `confidence: high` for 8–10, `confidence: medium` for 6 (relies on repo mypy gate).

## Non-goals

- Not declarative per-stage `checks:` for the pipeline (separate Roadmap item gated on this clean dispatch).
- Not changing `_process_gated_job` / `_process_pipeline_job` internals, budgets, report contract, detached launcher, or `validate_stage_sessions`.
- Not an optional "routine with `checks`" hybrid — issue 026's non-SSOT warned against.
- Not expanding `$ROUTINE_REPORT` warning's token set to `$REPORT`.

## Review

- blocking: items 1–5, 7, 11 — kind-only dispatch and mutually-exclusive validation plus the committed `jobs.d/` migration that would break PR CI if shipped without it. `confidence: high`.
- non-blocking: items 6, 8–10 — type-narrow mypy gate, regression parity, and history/status compat that are fully verified but not merge-gating for the SSOT shape. `confidence: high` for 8–10, `confidence: medium` for 6.
- confidence: high for validation/dispatch/re-key/migration (config load, AST seam, grep clean, `last_terminal_run` by-name join); medium for mypy literal gate (relies on repo mypy gate rather than unit test alone).

## Changelog v1→v2

- **Verified all 11 `Test: <name>` tokens from the issue are present, none dropped** — independent cross-check of `docs/process/issues/049-unify-routines-pipeline-as-one-gated-workflow-engine.md` § Acceptance criteria against spec v1; v1 already had all 11 with exact names, v2 preserves them and ensures each numbered item ends literally `Test: <name>` on the same line.
- **Problem — tightened narrative** (`confidence: high`): added explicit framing of scheduling vs first-code-level-gate vs engine-model-unification layers, quoted 026's deferral language verbatim, named the 038/042/047 "plausible contract" failure mode, and stated the post-fix three-value branch table; no technical change, just makes the SSOT rationale reviewable without re-reading the issue.
- **Approach §1 — clarified per-job semantics** (`confidence: high`): noted `_JOB_DEFAULTS["kind"] = "routine"` + merge at `config.py:514` covers both `jobs.yaml` and `jobs.d/` via `_build_job`, and that `defaults.yaml` non-inheritance is pinned by a negative test; affirmed `Job.kind` field comment must document history compat.
- **Approach §3 — corrected `cli.py:630` re-key detail** (`confidence: high`): v1 said "keys off `job.checks is None` → `job.kind != "gated"`" but current code is an `elif` after `if job.kind == "pipeline"` at `:622`; v2 documents that the `elif` must bypass *both* pipeline (already) and gated, so gated never trips the `$ROUTINE_REPORT` warning — behavior-identical after §1's `non-null checks ⟺ kind == "gated"` invariant. No new behavior, just precise edit location.
- **Files touched — anchored to base commit `c0d5054`** (`confidence: high`): line refs now note they were verified at `c0d5054` (v1's `:739-759` is `:745-802` post-edit); added `docs/process/pi-update-runbook.md` migration line; clarified not-touched scope (`history.jsonl`, `runner.py` unchanged).
- **Risks — added `confidence:` per risk and one new risk** (`confidence: medium` for the new item): annotated every risk with `confidence: high` (or `medium` where noted), and added the mypy `Literal` narrow surface risk (low-probability `mypy` fallout where callers index `kind` as plain `str`); also added explicit G-15 spec-path hygiene risk already in v1 but now with `confidence: high`.
- **Acceptance criteria — reformatted and tiered** (`confidence: high`): ensured each of the 11 items ends exactly `Test: <name>` (no trailing punctuation), mirrored issue wording for messages/invariants, and added inline `blocking`/`non-blocking` tiers with `confidence:` tokens (so the spec satisfies the v2 requirement that every assessment carry `blocking`/`non-blocking` and `confidence:`) while preserving the dedicated `## Review` tier summary.
- **Added this `## Changelog v1→v2` section** per task requirement; no files other than this spec were touched.
