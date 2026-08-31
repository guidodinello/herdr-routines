---
id: "006"
title: "Split jobs.yaml into one file per job (jobs.d/<name>.yaml)"
status: done
priority: medium
area: config
---

## Description

Replace the monolithic `jobs.yaml` list with a `jobs.d/` directory discovered
by listing, one `jobs.d/<name>.yaml` per job, the filename doubling as (or
validated against) the job's `name`. Shared fields (today's top-level
`defaults:` block — `agent_kind`, `workspace`, `timezone`, etc.) move to a
sibling `defaults.yaml` that the loader merges *under* each job file's own
fields.

Why (from `ROADMAP.md` Next §): editing a single job today means hand-editing
a block inside a bigger file — fragile for scripted edits (flipping an
`enabled` flag needs a regex/line-number substitution rather than a plain
file write), disable-by-rename or `git mv` is more legible, and a syntax
error in one job's file can't break parsing of the others.

Real work, not a config reshuffle: `src/herdr_routines/config.py` needs the
directory-discovery + merge logic and filename/`name` consistency
validation; `validate` / `status` / `history` / `scheduled` / `ps` must stop
assuming a single file path.

## How loading works today (context for the change)

`default_config_path()` (`config.py:173`) resolves the config file:
`--config` > `$HERDR_PLUGIN_CONFIG_DIR/jobs.yaml` > `~/.config/herdr-routines/jobs.yaml`.
`load_config(path: Path)` (`config.py:185`) reads that one YAML file, validates
top-level `{version, defaults, jobs}`, then builds each `Job` from
`_build_job(raw_job, raw_defaults, index)` merging `defaults` under each job.

Every consumer goes through one choke point: `cli.py:_load_config_or_exit`
(`cli.py:196`) → `load_config(args.config or default_config_path())`. Subcommands
using it: `tick` (run_tick), `status`, `scheduled`, `ps`, `history`, `validate`,
`run`. `pick-feature` and `gc` do **not** load config.

## Design

- `default_config_path()` returns a **directory** instead of a file: when resolved
  path is a dir, treat it as the config root; load `<root>/defaults.yaml` (merged
  under each job) + every `*.yaml` in `<root>/jobs.d/`. Backward compatible: when
  the resolved path is still a file, behave exactly as today (single-file mode).
- Each `jobs.d/<name>.yaml` contains **only that job's fields** (no `version`,
  no `defaults`, no `jobs` wrapper). The loader validates filename == `name`
  (subject to the existing `^[a-z][a-z0-9_-]{0,23}$` name regex — `config.py`).
- Duplicate `name`s across the dir are rejected (same rule as today).
- A YAML parse/schema error in one `jobs.d/*.yaml` is reported **naming that
  file**, and does not block other jobs from loading. (Today a single bad file
  fails everything; in dir mode one job's file failing should be isolated and
  reported so the rest still load — this is the isolation win.)
- `--config` CLI flag broadens: a path to a dir (new mode) or a file (legacy).

## Migration path

During a transition window, accept **both**:
- if the config root is a dir → dir mode (`defaults.yaml` + `jobs.d/*.yaml`);
- if it's still a `jobs.yaml` file → legacy single-file mode (unchanged).

Migration helper (optional, `herdr-routines migrate-jobs-dir`): reads the
monolithic `jobs.yaml`, writes `defaults.yaml` + one `jobs.d/<name>.yaml` per
job, refuses to overwrite, prints the diff. `validate` passes in either mode.
The Pi rollout then switches `HERDR_PLUGIN_CONFIG_DIR` (or `--config`) to the dir.

## Acceptance criteria

Each item ends `Test: <name>` — tests authored in `tests/test_config.py`
(and where relevant `tests/test_cli.py`), reusing the `tmp_config_path`
fixture pattern.

1. Directory mode loads a `defaults.yaml` merged under each `jobs.d/*.yaml` job,
   and errs clearly when a file's `name` disagrees with its filename.
   `Test: test_jobs_dir_discovers_and_merges_defaults`
2. Legacy single-file mode still works unchanged.
   `Test: test_single_file_mode_unchanged`
3. A YAML/parse error in one `jobs.d/*.yaml` surfaces that file by name and does
   not prevent the other jobs from loading.
   `Test: test_jobs_dir_surfaces_bad_file_and_loads_others`
4. Duplicate job `name`s across `jobs.d/` are rejected.
   `Test: test_jobs_dir_duplicate_names_rejected`
5. `default_config_path()` returns a dir when `$HERDR_PLUGIN_CONFIG_DIR` (or
   `--config`) points at a dir, else the legacy file.
   `Test: test_default_config_path_dir_vs_file`
6. `validate`, `scheduled`, `ps`, and `history` work against the dir layout with
   no single-file-path assumption left.
   `Test: test_cli_subcommands_work_on_jobs_dir`
7. `validate`'s systemd-timeout check still computes from the dir-loaded jobs.
   `Test: test_validate_systemd_timeout_on_jobs_dir`
8. Migrate helper (if built) round-trips: file mode → dir mode → same set of jobs
   (names + effective fields equal).
   `Test: test_migrate_jobs_dir_roundtrip`

## Why these tests

- 1–2 pin the **dual-mode** contract (the whole point: don't break the existing
  Pi `jobs.yaml` until the rollout flips to dir mode).
- 3 pins the **isolation** property (a bad file names itself; the rest still load)
  — the concrete win over the monolith.
- 4–5 pin name-vs-filename consistency and the path-resolution change.
- 6–8 cover the claim that "this isn't a config reshuffle": every CLI consumer
  and the timeout check keep working under the dir layout.

## Non-goals

- Not introducing `version`, `defaults`, or `jobs` wrappers inside per-job files.
- Not a schema rewrite — job fields are unchanged; only the container changes.
- Not touching `pick-feature`/`gc` (don't load config).

## Log

- **2026-08-27**: curated from `ROADMAP.md` Next §. The scripted-edit friction
  was hit again this day trimming `fitted-pr-review-4`/`-5` to `enabled: false`
  by line-number `sed`.
- **2026-08-30**: refined into a pipeline-ready spec — added test-named
  Acceptance criteria, the dual-mode design + migration path, and integration
  points derived from `config.py:173-412` / `cli.py:196`. Selected as the next
  pipeline implement target (after issue 025's unified gate model).
