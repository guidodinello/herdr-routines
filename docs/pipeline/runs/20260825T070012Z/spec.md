# spec: unified CLI visibility — `ps` + `scheduled` (20260825T070012Z)

## Problem

`herdr-routines` already has `status`/`history` for its own scheduled jobs, but there is no single place to see everything running or scheduled across the Herdr+pipeline stack. Today an operator must manually compose:

- live workspaces/panes/agents via `herdr pane list` / `herdr agent list` (and interpret bare agent names),
- pipeline runs in progress via `~/.local/state/herdr-routines/reports/pipeline-*.md` plus each run's `state.json` (`current_stage` / completion),
- scheduled jobs via `jobs.yaml` + `history.jsonl` (next-fire, enabled/disabled).

The recent `herdr-pr-review` disable is the concrete example: disabled state is invisible unless you open `jobs.yaml` directly — `status` shows last-run/due but not next-fire or enabled. Pipelines add a second hidden layer: a `herdr agent list` row says `pl-3-…` but not "pipeline run 20260825T… stage 4/6".

Goal: two purely additive read-only CLI commands that unify these sources into tables, with no web server, no daemon, no new state.

## Approach

Add two new subcommands to `herdr-routines` CLI (names checked against existing `cli.py:79` subparsers `tick|status|history|validate|run|gc` — no collision):

- **`herdr-routines ps`** — "what's currently running": calls `herdr pane list` / `herdr agent list` (via `herdr.py:HerdrClient`, same seam as `tick.py:266`), cross-references any `state.json` files under the reports directory (`~/.local/state/herdr-routines/reports/` or `$HERDR_PLUGIN_STATE_DIR/reports/` — same fallback as `runner.py:187` / `history.py:31`), where `current_stage` hasn't reached completion or whose `pipeline-<run_id>.md` report doesn't exist yet. Row enriches bare agent/pane name to "pipeline run 20260825T… stage 4/6" when a match exists, otherwise shows pane/agent as-is.
- **`herdr-routines scheduled`** — "what's scheduled": iterates `jobs.yaml` via `config.py:138` `default_config_path()` / `load_config()` (reuse, don't re-parse), computes next-fire per job by reusing `schedule.py:72`'s due-check / `croniter` + `ZoneInfo` logic (same `timezone` and DST dedup as `tick.py:145` `decide()`), shows enabled/disabled, cron, timezone, catch-up, last terminal state from `history.py:112` `last_terminal_run`, and next-fire timestamp. Disabled jobs are shown (dimmed/marked), not hidden.

Both commands are read-only (no JSONL/marker writes, no `history.append`), handle missing Herdr server / missing files gracefully (`HerdrCliError` / absent `history.jsonl` → empty table with warning, exit 0), and follow existing module layout: add handlers in `cli.py:67` (`_build_parser` + `_cmd_ps`/`_cmd_scheduled`) and extract table-building helpers to new small modules under `src/herdr_routines/` mirroring `history.py`/`schedule.py` purity (e.g. `src/herdr_routines/ps.py` + `src/herdr_routines/scheduled.py` or a shared `visibility.py` — check existing `src/herdr_routines/*.py` before inventing pattern; `status` logic currently lives in `cli.py:167` `_cmd_status` so either inline or split is acceptable if it reuses `config.py`/`history.py`/`schedule.py`/`herdr.py`).

Reuse `schedule.py:ScheduleResult`/`decide()` or factor `_occurrences_since`'s `get_next` enumeration for next-fire rather than reimplementing cron math. Reuse `HerdrClient.agent_statuses()` (`herdr.py:231`) and add `pane_list`/`workspace_list` wrappers only if needed, behind the same `CommandRunner` fake seam for tier-2 tests.

Output: plain text table by default (fixed-width columns, fits `herdr notification` width), `--json` flag optional for scripting (mirrors `history --json`).

## Files touched

- `docs/pipeline/runs/20260825T070012Z/spec.md` — this file (per-run spec, `docs/pipeline/design.md:79` G-15).
- `src/herdr_routines/cli.py` — register `ps` and `scheduled` subparsers in `_build_parser`, wire `_cmd_ps` / `_cmd_scheduled` handlers (follows `tick|status|history|validate|run|gc` pattern).
- `src/herdr_routines/herdr.py` — add `pane_list`/`workspace_list` (or `pane_statuses`) wrappers if `agent_statuses()` alone insufficient; keep behind `CommandRunner` seam for fake tests.
- `src/herdr_routines/ps.py` (or `visibility.py`) — pure helper: merge `herdr agent/pane list` output with `state.json` scan under `default_reports_dir()`; enrich rows with `current_stage`/`run_id`.
- `src/herdr_routines/scheduled.py` (or `visibility.py`) — pure helper: for each `Job` in `RoutinesConfig` compute next-fire via `schedule.py` cron logic, read `last_terminal_run`/`first_seen_at`, emit row including `enabled` (visible disabled marker).
- `src/herdr_routines/config.py` / `src/herdr_routines/schedule.py` / `src/herdr_routines/history.py` — reused, not rewritten (only imported).
- `tests/test_ps.py` + `tests/test_scheduled.py` (new) — tier-1/2 tests: config-load reuse, next-fire via `schedule.py` (including DST/catch-up), disabled-visible, pane+state.json cross-reference, Herdr-unreachable graceful empty, table formatting; mirrors `test_schedule.py`/`test_herdr.py` fake-CommandRunner pattern.
- `docs/pipeline/design.md` / `README.md` — optional doc note for new commands (not required for gate).

## Risks

- **Herdr CLI schema drift.** `herdr pane list` / `herdr agent list` JSON shape is pinned only in `tests/fixtures/api-schema.json` and `herdr.py:_extract_*` — a Herdr 0.8.x bump could rename fields. Mitigation: use same `_try_parse_json` + shape guards as `herdr.py:235`, add fixture update step, fail open with warning row rather than crash.
- **Reports/state.json path divergence.** Pipeline runs write `state.json` alongside `pipeline-<run_id>.md` under `~/.local/state/herdr-routines/reports/` (or `$HERDR_PLUGIN_STATE_DIR`); `herdr-routines` reports use same base via `runner.py:187` `default_reports_dir()`. If a pipeline run uses a different base, cross-reference misses. Mitigation: resolve both `default_reports_dir()` and `default_history_path()` parents, scan with `Path.glob`, document the one directory contract.
- **Cron next-fire recomputation.** Reimplementing cron math would diverge from `tick.py:145` `decide()` (DST dedup, `catch_up_minutes`, `job_registered_at` vs `last_terminal`). Mitigation: reuse `schedule.py:_occurrences_since` / `decide` enumeration; test disabled job shows `enabled=false` and still computes next-fire but marks it inactive — covers the `herdr-pr-review` case.
- **Table vs JSON trade-off.** Fixed-width tables can truncate long branch/pane IDs; adding deps (`rich`/`tabulate`) adds weight for a single-user CLI. Mitigation: stdlib formatting, truncate with `…`, offer `--json` for scripting; keep no new runtime deps.
- **Stale/incomplete `state.json`.** A crashed pipeline may leave `state.json` with stale `current_stage` and no `pipeline-*.md`. Mitigation: treat absent report as "in progress" only when `state.json` `current_stage` not terminal and `history.jsonl` has no terminal record for that `run_id`; otherwise show as orphaned with warning.
- **Spec-path hygiene (G-15).** Must stay at `docs/pipeline/runs/<run_id>/spec.md`; writing to root `spec.md` or `docs/pipeline/spec.md` reintroduces PR #28/#29 full-file merge conflict. Verified `mkdir -p` per-run path on purpose.

## Acceptance criteria

1. A new subcommand (`herdr-routines ps`) prints a table of currently-running panes/agents from `herdr pane list`/`herdr agent list` via `HerdrClient` (same seam as `tick.py:266`) with no crash when herdr reports zero live agents — empty table with warning, exit 0 — Test: test_status_running_table_handles_empty
2. The running-table command cross-references in-progress pipeline runs (`state.json` files under `~/.local/state/herdr-routines/reports/` or `$HERDR_PLUGIN_STATE_DIR/reports/` whose stage isn't complete) and shows a stage indicator (e.g. "pipeline run 20260825T… stage 4/6"), not just a bare agent name — Test: test_status_running_table_shows_pipeline_stage
3. A new subcommand (`herdr-routines scheduled`) prints a table of scheduled jobs from `jobs.yaml` via `config.py:138` `load_config()`/`default_config_path()` with each job's next-fire time (reusing `schedule.py:72` croniter + ZoneInfo logic) and enabled/disabled state — Test: test_status_scheduled_table_shows_next_fire
4. The scheduled-jobs table correctly shows a disabled job (e.g. `enabled: false` in `jobs.yaml`) as disabled (dimmed/marked), not silently omitted — Test: test_status_scheduled_table_shows_disabled_jobs
5. Both commands are read-only: no test asserts any write to `jobs.yaml`, `history.jsonl`, or any `state.json` — Test: test_status_commands_are_read_only

## Changelog

`## Changelog v1→v2` — changes from v1 to v2:

- Added `## Acceptance criteria` with 5 numbered items, each ending `Test: <name>` to make verification deterministic and CI-enforceable (covers empty running table, pipeline stage cross-reference, scheduled next-fire, disabled-job visibility, and read-only guarantee).
- Added `## Review notes` with explicit `blocking`/`non-blocking` labels and `confidence:` tiers for review gating.
- Clarified that `ps` reuses `HerdrClient`/`CommandRunner` fake seam and handles missing Herdr server / absent files gracefully (`HerdrCliError` → empty table with warning, exit 0); `scheduled` reuses `schedule.py` cron logic (DST dedup, `catch_up_minutes`, `ZoneInfo`) rather than reimplementing.
- No change to `## Problem`, `## Approach`, `## Files touched`, `## Risks` — still two additive read-only subcommands (`ps` + `scheduled`) with no web server/daemon/new state.

## Review notes

blocking: ps must table herdr pane/agent list via HerdrClient and handle zero live agents without crash (criterion 1); ps must cross-reference state.json in-progress runs and show stage indicator not bare name (criterion 2); scheduled must table jobs.yaml with next-fire and enabled/disabled per job (criterion 3); disabled jobs must be shown as disabled not omitted (criterion 4); both commands must be read-only with no writes to jobs.yaml/history.jsonl/state.json (criterion 5)
non-blocking: exact table column widths/truncation and optional --json flag shape are formatting choices; module split (ps.py+scheduled.py vs visibility.py) is at implementer's discretion if reuse of config.py/schedule.py/history.py/herdr.py is preserved
confidence: high — empty-table, stage cross-reference, next-fire, disabled-visible, and read-only criteria are deterministic via fake CommandRunner/filesystem tests (criteria 1-5)
confidence: medium — next-fire recomputation reuses schedule.py/croniter + ZoneInfo DST dedup; edge on catch-up/history interaction may need live history.jsonl shape check
confidence: low — live Herdr CLI JSON shape drift (herdr pane list / herdr agent list) and reports/state.json path divergence require fixture update and fail-open warning, covered outside isolated unit tests
