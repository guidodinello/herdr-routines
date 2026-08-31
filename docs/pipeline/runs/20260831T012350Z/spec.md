# spec: Split jobs.yaml into one file per job — jobs.d/<name>.yaml (20260831T012350Z) — v1

Per-run spec at `docs/pipeline/runs/20260831T012350Z/spec.md` (G-15: per-run path avoids PR #28/#29 shared-path full-file conflict — `docs/pipeline/design.md:79`). Implements `docs/process/issues/006-jobs-dir-per-file.md` as indexed in `ROADMAP.md:49` Next §. `docs/plan-v1.md:174` documents the monolith this replaces (`jobs.yaml` with top-level `defaults:` + `jobs:` list).

## Problem

`jobs.yaml` is a monolithic list (`src/herdr_routines/config.py:185` `load_config`, `src/herdr_routines/config.py:221` `raw_jobs` list). Editing one job means hand-editing a block inside a bigger file — fragile for scripted edits (`docs/process/issues/006-jobs-dir-per-file.md:19` — flipping `enabled` needs regex/line-number substitution rather than a plain file write; `issue 006 Log 2026-08-27` trimming `fitted-pr-review-4/-5` to `enabled: false` by `sed`), disable-by-rename or `git mv` is illegible, and one YAML syntax error breaks parsing of every job (`src/herdr_routines/config.py:193` `yaml.safe_load` fail-closed on the whole file). `ROADMAP.md:49` Next § promotes this to `status: open` after a few weeks of real runs.

Goal: directory-discovered, one file per job at `jobs.d/<name>.yaml` with filename doubling as (or validated against) `name`, shared fields (today's top-level `defaults:` — `agent_kind`, `workspace`, `timezone`, etc.) moved to sibling `defaults.yaml` merged under each job's own fields.

## Approach

Replace single-file load with directory discovery + per-file merge. No new daemon/state, no `tick.py`/`history.py` schema change.

### Config layout

```
<config_dir>/jobs.d/          # was <config_dir>/jobs.yaml (src/herdr_routines/config.py:173 default_config_path)
  defaults.yaml               # optional; was top-level `defaults:` in jobs.yaml:176 (all keys optional)
  <name>.yaml                 # one per job; filename (sans .yaml) is the canonical name
```

- `default_config_path()` (`src/herdr_routines/config.py:173`) currently resolves `--config > $HERDR_PLUGIN_CONFIG_DIR/jobs.yaml > ~/.config/herdr-routines/jobs.yaml`. Change to resolve a **directory** base: `--config` may point to a file (legacy) or directory; otherwise `HERDR_PLUGIN_CONFIG_DIR/jobs.d` or `~/.config/herdr-routines/jobs.d`. Document which value `--config` takes (dir vs file) in `cli.py:75` help. Keep XDG + plugin-dir fallback pattern from `docs/plan-v1.md:173`.
- `defaults.yaml` is the same key set as `_DEFAULTS_ALLOWED_KEYS` (`src/herdr_routines/config.py:62`) merged under `_JOB_DEFAULTS` (`src/herdr_routines/config.py:96`). Per-job file fields win over `defaults.yaml` (same precedence as today: `{**_JOB_DEFAULTS, **defaults, **raw_job}` at `src/herdr_routines/config.py:248`). Absent `defaults.yaml` = empty defaults (no error).

### Loader (`src/herdr_routines/config.py`)

- New `load_config_dir(path: Path) -> RoutinesConfig` (or extend `load_config` to branch on `path.is_dir()`). On directory: list `jobs.d/*.yaml` (sorted), exclude `defaults.yaml`; on file: legacy single-file path (see Migration).
- Per-file load: `yaml.safe_load` each file individually. YAML syntax error in one file surfaces **that file by name** (`ConfigError: jobs.d/<name>.yaml: <yaml error>`) and **does not prevent other jobs from loading** for `status`/`scheduled`/`ps`/`validate` diagnostics — but `validate`/`tick` must still fail closed if any file is broken (see Validation). For the happy path used by `tick`/`run`, either (a) fail the tick if any file failed to parse, or (b) load the good subset and report the bad file — pick (a) for `tick`/`run` correctness, (b) for `validate` diagnostics; document the chosen split. Preserve pure posture: no subprocess, no clock.
- Merge: `merged = {**_JOB_DEFAULTS, **defaults_yaml, **raw_job}` after defaults load.
- Filename/name validation: `name` key inside the file (if present) must equal filename stem, or if `name` omitted the filename **is** the name (decide one contract — issue says "filename doubling as (or validated against) name"). Enforce `NAME_RE` (`src/herdr_routines/config.py:60`) on both filename stem and `name` value; reject `jobs.d/Bad-Name.yaml` or `name` mismatch with `ConfigError` naming the file.
- Duplicate detection stays (`src/herdr_routines/config.py:231` `seen_names`) — now across filenames.
- Unknown-key checks (`_JOB_ALLOWED_KEYS`, `_DEFAULTS_ALLOWED_KEYS`) unchanged per file; unknown top-level keys in `defaults.yaml` rejected like today (`src/herdr_routines/config.py:215`).

### Consumers must stop assuming a single file

- `src/herdr_routines/cli.py:75` `--config` help + `src/herdr_routines/cli.py:196` `_load_config_or_exit` (used by `validate`/`status`/`tick`/`run`/`scheduled`/`ps`): pass directory path, not file path.
- `validate` (`src/herdr_routines/cli.py:376`): iterate per-file errors, print `error: jobs.d/<name>.yaml: ...` per file, exit 1 if any; keep existing repo-existence (`src/herdr_routines/cli.py:387` `job.repo.exists()`) and `validate --systemd-unit` checks.
- `status` (`src/herdr_routines/cli.py:221`), `scheduled` (`src/herdr_routines/scheduled.py:1` via `config.load_config`), `ps` (`src/herdr_routines/ps.py:1`), `history` (`src/herdr_routines/history.py:34` — not config-dependent but shares `--config` path contract), `tick` (`src/herdr_routines/tick.py:106`): no logic change beyond the new loader entry point; ensure no hard-coded `jobs.yaml` string remains (grep `jobs\.yaml` across `src/`).
- `deploy/jobs.example.yaml` → `deploy/jobs.d/` example directory + `deploy/jobs.d/defaults.yaml` (or keep example monolith with migration note).

### Migration

Documented path per `docs/process/issues/006-jobs-dir-per-file.md:36` acceptance: loader accepts **both** during a transition — if `jobs.d/` exists, use it; else fall back to legacy `jobs.yaml` with a deprecation warning. Alternatively ship a one-shot `migrate` helper/script that splits an existing `jobs.yaml` into `jobs.d/` + `defaults.yaml`. Pick one, document in `README.md`/`deploy/README.md`. No silent auto-migration that writes without consent.

## Files touched

- `docs/pipeline/runs/20260831T012350Z/spec.md` — this file (per-run spec, G-15).
- `src/herdr_routines/config.py` — directory discovery (`Path.glob("*.yaml")` sorted, exclude `defaults.yaml`), `defaults.yaml` load + merge, filename/`name` consistency validation against `NAME_RE:60`, per-file isolated parse with file-named errors, legacy-file fallback/migration branch, update `default_config_path:173` to resolve `jobs.d` directory; preserve `load_config` pure posture and existing `_JOB_ALLOWED_KEYS`/`_JOB_DEFAULTS`/`VALID_*` contracts.
- `src/herdr_routines/cli.py` — update `--config` help (`cli.py:75`), `_load_config_or_exit:196`, `_cmd_validate:376` per-file error reporting, ensure `status`/`scheduled`/`ps`/`tick`/`run` call the new directory loader; grep-clean `jobs.yaml` single-path assumptions.
- `src/herdr_routines/scheduled.py` / `src/herdr_routines/ps.py` / `src/herdr_routines/tick.py` / `src/herdr_routines/history.py` — reused, not rewritten; only import path changes if loader renamed (no cron/DST/history logic change).
- `tests/test_config.py` — matrix: happy `jobs.d/` load + `defaults.yaml` merge precedence, filename/`name` match vs mismatch, `NAME_RE` violation on filename, duplicate across files, unknown keys per file, per-file YAML syntax error isolated with file name, `defaults.yaml` absent, `checks: []` still plain, legacy `jobs.yaml` fallback.
- `tests/test_cli.py` (or new `tests/test_jobs_dir.py`) — `validate`/`status`/`scheduled`/`ps` work against directory layout, `--config` dir vs file, missing Herdr graceful.
- `deploy/jobs.example.yaml` (or `deploy/jobs.d/` + `deploy/jobs.d/defaults.yaml`) — example directory layout mirroring new loader.
- `docs/pipeline/design.md` / `README.md` — optional note on new `jobs.d/` layout (not required for gate).

## Risks

- **Filename/name divergence.** Two sources of truth (`<name>.yaml` stem vs `name:` key). Mitigation: single contract — filename stem *is* the name (key optional but if present must equal stem); validate both against `NAME_RE:60`; error names the file.
- **Partial load masking broken jobs.** "Syntax error in one file does not prevent others from loading" for diagnostics, but `tick` must not silently run a subset. Mitigation: `validate` loads good subset + reports bad files; `tick`/`run` fail-closed if any file failed (exit 1, no dispatch) — document the split.
- **Migration breakage for deployed Pi.** Existing `~/.config/herdr-routines/jobs.yaml` stops working if loader only looks for `jobs.d/`. Mitigation: dual-mode loader (dir preferred, file fallback with warning) for one release, or documented `jobs.yaml -> jobs.d/` split script; flip Pi config atomically.
- **Defaults merge semantics change.** Moving `defaults:` block to `defaults.yaml` changes include path. Mitigation: same key set (`_DEFAULTS_ALLOWED_KEYS:62`), same precedence (`_DEFAULTS` < `defaults.yaml` < job file), absent `defaults.yaml` = empty; test precedence explicitly.
- **Path assumptions across CLI.** `validate`/`status`/`history`/`scheduled`/`ps` currently assume single file path (`docs/process/issues/006-jobs-dir-per-file.md:26`). Mitigation: grep `jobs\.yaml`/`default_config_path`/`load_config` across `src/`, update every caller; add test that each command works with `--config <tmpdir>/jobs.d`.
- **Filesystem portability (case, sorting).** `jobs.d/*.yaml` glob order non-deterministic on some FS; case-sensitive `NAME_RE` vs case-insensitive FS. Mitigation: `sorted()` file list; keep `NAME_RE` lowercase-only; document that two files differing only by case collide.
- **Spec-path hygiene (G-15).** Must stay at `docs/pipeline/runs/<run_id>/spec.md`; writing to repo-root `spec.md` or `docs/pipeline/spec.md` reintroduces PR #28/#29 full-file merge conflict. This spec is already at the per-run path (`mkdir -p .../20260831T012350Z` per task).
