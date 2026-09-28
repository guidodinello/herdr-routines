# Spec — Log rotation: `history.jsonl` + reports retention (021) — 20260928T051515Z — v2

Implements `docs/process/issues/021-log-rotation.md`. Per-run spec at `docs/pipeline/runs/20260928T051515Z/spec.md` (per-run path avoids the PR #28/#29 shared-path conflict — G-15).

## Problem

Two state artifacts grow without bound, and neither has a retention story:

1. **`history.jsonl`** — one line per state transition, appended forever
   (`history.append` at `src/herdr_routines/history.py:73`). `docs/plan-v1.md:415` explicitly
   deferred rotation: *"No log rotation in v1; a handful of jobs writing a few lines a day will
   not matter for years."* The issue log (2026-08-27) waived that gate anyway — small, mechanical,
   worth having before it is needed.

2. **`reports/`** — `default_reports_dir()` (`src/herdr_routines/runner.py:360-369`) accumulates
   one `{run_id}.md` per run **plus** one `{run_id}.tail.txt` per failure
   (`runner._capture_visible_tail`) **plus** one `state.json` per pipeline run. Nothing ever
   removes them. This is now the dominant growth: 108 MB of `.venv` per worktree is already the
   bigger on-disk cost (issue 044), so unbounded report text is the next thing to notice.

The hard part is **not** moving or deleting files — it is that rotation must not change what the
readers see. Seven call sites read history through `read_all`/`read_job`, and four more read the
reports directory:

| Reader | Entry point | Must keep working |
| --- | --- | --- |
| `herdr-routines history` | `cli.py:507-508` → `history.read_job` | across rolled files |
| `herdr-routines status` | `cli.py:433` → `history.last_terminal_run` | across rolled files |
| `herdr-routines scheduled` | `scheduled.py:build_scheduled_rows` → `last_terminal_run` | across rolled files |
| `digest` | `digest.py:build_digest` → `last_terminal_run` | across rolled files |
| `auto_fix` | `auto_fix.py:388,652` → `read_job` | across rolled files |
| `ps` | `ps.py:55,73` → scans `state.json` under reports | must not lose in-flight runs |
| `pick_feature` / `pipeline_watchdog` | `_has_terminal_report` (`pick_feature.py:328`, `pipeline_watchdog.py:221`) | must not prune a run's terminal report |

Acceptance names `history` / `ps` / `scheduled`; the table above is the same problem stated
honestly — `digest` and `auto_fix` are unlisted readers that would break silently if only the
named three were checked. — confidence: high

## Approach

Rotation is **non-destructive** (it renames, never deletes) so it can run automatically inside the
tick. Pruning **is** destructive, so it is an explicit command gated on `--yes`, exactly like
`gc --delete` (`cli.py:180-199`, refusal at `cli.py:797-799`). The asymmetry in the issue's
acceptance ("rotates on a threshold" vs "pruned by an explicit command") follows directly from
that. — confidence: high

### 1. Roll naming: `history-<YYYYMMDDTHHMMSSZ>.jsonl`, not `history-YYYYMM.jsonl`

The issue suggests `history-YYYYMM.jsonl` "or similar". **Reject the `YYYYMM` form**: it collides
on two size-triggered rotations in the same month, and the obvious disambiguator breaks
chronological ordering. Comparing `history-202609-2.jsonl` against `history-202609.jsonl`, the
first differing byte is `-` (0x2D) vs `.` (0x2E) at index 15, so the *second* roll sorts *first*
— a lexicographic reader returns that month's runs out of order, which corrupts
`first_seen_at` (`history.py:127`, which deliberately takes the earliest record) and feeds a wrong
`job_registered_at` into `schedule.decide()`. — confidence: high

Use `history-<YYYYMMDDTHHMMSSZ>.jsonl`: strictly increasing under lexicographic sort, and the
same suffix format as `runner.make_run_id` (`runner.py:372`), so it matches repo convention. A
same-second collision is impossible in practice because rotation runs under the exclusive
`tick.lock` (`tick.py:85-100`); defensively, if the target exists, **skip the rotation and
`log.warning`** rather than inventing a suffix that would reintroduce the ordering bug. — confidence: high

Rotation itself: `os.replace(live, rolled)` (atomic rename, so a reader never sees a truncated
file) followed by creating a fresh empty `history.jsonl`. `append` already does
`path.parent.mkdir(parents=True, exist_ok=True)` and opens `"a"` (`history.py:74-77`), so the
missing-live-file window self-heals on the next append. — confidence: high

### 2. Transparency: fix it once, in `read_all` — [blocking]

`read_all(path)` (`history.py:81-99`) becomes the single place that knows about rolled files: it
globs siblings matching `history-*.jsonl`, and reads `sorted(rolled) + [live]`.

**Every existing reader becomes transparent with zero caller changes.** `read_job`,
`last_terminal_run`, `has_ever_been_seen`, `first_seen_at`, `find_stale_running` and
`is_currently_running` all funnel through `read_all`/`read_job`, and so do all five
call sites in the table above. This is the whole reason the feature is small: acceptance is
satisfied by changing one function, not by threading a file-set through seven call sites. — confidence: high

Ordering is **explicit** (`sorted(rolled) + [live]`), deliberately not relying on the incidental
fact that `history-*.jsonl` happens to sort before `history.jsonl`. That coincidence is an ASCII
artifact of `-` (0x2D) vs `.` (0x2E); depending on it would be a latent bug the first time
someone changes the naming scheme. — confidence: high

The existing unparseable-line skip (`history.py:97-98`) applies per rolled file too, so a file
truncated by a power cut mid-append still degrades one line, not one tick. — confidence: high

### 3. When rotation runs: the tick preamble, under the lock, best-effort — [blocking]

In `run_tick` (`tick.py:126-145`), next to the existing `reap_tmp()` preamble (issue 027,
`tick.py:131-137`), before job dispatch. Same shape: `try/except Exception` → `log.warning`,
never fails the tick. The exclusive `flock` is already held by the caller, so rotation cannot
interleave with another tick's appends. — confidence: high

Trigger is **size OR age** (either suffices):

- `history_max_bytes` — `live.stat().st_size` exceeds it.
- `history_max_age_days` — the live file's oldest record is older than the window. Prefer the
  oldest record's `ts` over mtime: a file copied in or restored from backup carries a misleading
  mtime, and the records' own timestamps are the thing the age policy is about. Fall back to
  mtime only if the file has no parseable records. — confidence: medium

### 4. Config: an optional top-level `retention:` block — [blocking]

Rotation is a property of **one file**, not of a job, so it must not be a per-job key — a
`defaults:` entry would be merged under every job, which is semantically wrong.

- Single-file layout (`jobs.yaml`): top-level `retention:` mapping, sibling of `defaults:`/`jobs:`.
- Directory layout (`jobs.d/`): a recognized sibling file `retention.yaml`, mirroring how
  `defaults.yaml` is already special-cased in `load_config_dir` (`config.py:409-496` — the glob
  at `config.py:440` already excludes it; add the exclusion, the read, and a
  `_validate_retention_keys` beside `_validate_defaults_keys` at `config.py:524-528`).
- `RoutinesConfig` (`config.py:291-300`) gains `retention: Retention = field(default_factory=Retention)`.

```yaml
retention:
  history_max_bytes: null      # null = off; e.g. 10485760
  history_max_age_days: null   # null = off; e.g. 30
  reports_max_age_days: 90     # default window for `prune reports`
```

**Defaults: rotation off, retention generous, deletion always opt-in** — the issue's "both off or
generous by default". Off is the honest default for rotation because §5's estimate still holds;
the point of the issue is that the mechanism now *exists* when the day comes, not that it should
be running on day one. — confidence: high

`validate` gets one soft `warning:` (exit code unchanged, per the `cli.py:631-646` precedent) when
`history_max_bytes` is set below 1 MiB, which would rotate on essentially every tick. — confidence: medium

### 5. Prune: `herdr-routines prune reports` — [blocking]

Mirrors `gc`'s CLI shape exactly (`cli.py:180-199`): a required mutually-exclusive
`--dry-run` / `--yes` group, `--yes` required to actually delete, the same
`error: refusing to delete without --yes` message, and a `render_table` listing
(`table.py`). Reusing the idiom means no new safety vocabulary to learn and no new
"delete path" to audit. — confidence: high

```
herdr-routines prune reports [--older-than DAYS] [--reports-dir PATH] (--dry-run | --yes)
herdr-routines prune history [--older-than DAYS] (--dry-run | --yes)   # opt-in; off by default
```

`--older-than` defaults to `retention.reports_max_age_days` (fallback 90). Selection is
mtime-based at the reports-dir top level only, no recursion, following `tmp_hygiene.reap_tmp`'s
proven shape (`tmp_hygiene.py:41-92`): per-entry `try/except OSError` so one failure never aborts
the sweep, symlinks skipped, and a `PruneResult(removed, kept_fresh, protected, errors)` counter
returned. Prunes `{run_id}.md`, `{run_id}.tail.txt` and `state.json`. — confidence: high

**Protected files — the one genuinely non-mechanical part.** A report belonging to an in-flight
pipeline run must not be pruned, or `ps` loses the row and `pick_feature`/`pipeline_watchdog`
re-derive the wrong in-flight set. Reuse the predicate that already encodes this rather than
reinventing it: `ps.scan_pipeline_runs` (`ps.py:55`) returns `PipelineRun` rows whose
`.in_progress` (`ps.py:47`) is exactly `not report_path.exists()`. Protect every report path of
every in-progress run (`pipeline-<run_id>.md` and `<run_id>.md`, per
`pipeline_watchdog._candidate_report_paths` at `pipeline_watchdog.py:202-217`) plus its
`state.json`, and count them as `protected` in the output. — confidence: high

This is also where issue 011 hands off: its re-refinement states *"Issue 021 (log rotation) covers
pruning the reports directory; this issue should settle the policy and let 021 implement the
mechanism."* Pruning `.tail.txt` here implements that mechanism; the **window** stays 011's to
set, which is why the default is a generous 90 days rather than a number this spec invents. — confidence: high

`prune history` deletes rolled `history-*.jsonl` files older than the window. Off by default and
`--yes`-gated. Named caveat below (risk 4) — deleting the earliest roll can make
`first_seen_at` return `None` for a long-lived job, re-registering it and silently changing its
due window. — confidence: high

### 6. Docs — [non-blocking]

- `docs/plan-v1.md:415` ("No log rotation in v1…") and the out-of-scope list at
  `docs/plan-v1.md:650` both become stale the moment this ships. This repo's own convention is to
  correct such claims in place rather than leave them (see `plan-v1.md:357-365`, `:619`).
- `deploy/jobs.example.yaml` + `deploy/jobs.d/` gain a commented `retention:` block; README
  documents `prune`.
- Flip `status: open` → `done` in `docs/process/issues/021-log-rotation.md` and add a Log line.

## Files touched

- `src/herdr_routines/history.py` — `read_all` reads rolled siblings (§2); add
  `rolled_history_paths`, `maybe_rotate`, `Retention`-independent helpers. Docstring at line 3
  gains a rotation note. — confidence: high
- `src/herdr_routines/config.py` — `Retention` dataclass; `RoutinesConfig.retention`
  (`config.py:291-300`); parse/validate `retention:` in `load_config` (`config.py:383-406`);
  `retention.yaml` support + glob exclusion in `load_config_dir` (`config.py:409-496`), mirroring
  `defaults.yaml`. No change to `_DEFAULTS_ALLOWED_KEYS`/`_JOB_ALLOWED_KEYS` (`config.py:126-166`).
  — confidence: high
- `src/herdr_routines/tick.py` — `run_tick` preamble gains `maybe_rotate` beside `reap_tmp`
  (`tick.py:126-145`). — confidence: high
- `src/herdr_routines/cli.py` — `prune` subparser (`_build_parser`, near the `gc` parser at
  `cli.py:180`) and `_cmd_prune`. `validate` gains the soft `history_max_bytes` warning.
  — confidence: high
- `src/herdr_routines/reports_prune.py` — **new**. `PruneResult`, `prune_reports`,
  `prune_rolled_history`, protected-set computation. New module rather than growing `gc.py`
  (pure git, 800+ lines) or `tmp_hygiene.py` (`/tmp` only) — this is state-dir retention with a
  different safety predicate. — confidence: medium
- `src/herdr_routines/ps.py` — **read-only reuse**, no edit expected; the prune path imports
  `scan_pipeline_runs`/`PipelineRun.in_progress` rather than duplicating them. Verify no import
  cycle (`ps` imports `runner`; `reports_prune` imports `ps` and `history`). — confidence: medium
- `docs/plan-v1.md:415,650`; `deploy/jobs.example.yaml`; `deploy/jobs.d/`; `README.md`;
  `docs/process/issues/021-log-rotation.md` (status + Log). — confidence: high
- Tests: `tests/test_history.py` (rotation + cross-file reads), `tests/test_config.py` (retention
  parsing, both layouts, unknown keys), `tests/test_tick.py` (preamble rotation, best-effort
  survival), `tests/test_cli.py` (prune parser + `--yes` refusal), new
  `tests/test_reports_prune.py`. — confidence: high

Not touched: `schedule.py` (rotation is not a scheduling concern), `herdr.py`, `runner.py`,
`pick_feature.py`, `pipeline_watchdog.py`, `digest.py`, systemd units, `herdr-plugin.toml` (a
`prune` action is nice-to-have; a destructive action in a UI-invoked manifest is a separate
decision). — confidence: high

## Risks

- [blocking] **A reader that silently misses records across a rotation boundary.** The window
  between `os.replace` and re-creating the live file is sub-millisecond, but a read-only command
  (`history`/`status`/`scheduled`/`digest`) running in it can see a set of files that no longer
  contains the records it is about to report. Nothing is lost on disk and the *scheduler* is
  unaffected — `tick` holds `tick.lock` for the entire rotate-and-append sequence, so
  `schedule.decide` can never observe a half-rotated history. Blast radius is one inspection
  command's output for one instant. Do **not** fix this with a shared `flock` on the read path:
  `run_tick` holds the exclusive lock for the whole multi-hour run, so `LOCK_SH` would make
  `status` block for hours, which is a worse regression than the race. Mitigated by a regression
  test asserting `last_terminal_run` is identical before and after a rotation. — confidence: high
- [blocking] **Ordering bug re-entering via the roll filename** — the `history-YYYYMM` →
  `history-YYYYMM-2` trap in §1 corrupts `first_seen_at` and therefore
  `job_registered_at`/`schedule.decide`. Mitigated by the timestamped name plus a
  skip-and-warn on collision, and by asserting in a test that three rolls in the same month read
  back in write order. — confidence: high
- [blocking] **Pruning a report an in-flight pipeline run still needs**, which would make `ps`
  drop the row and let `pick_feature` re-pick the same issue. Mitigated by the `protected` set
  (§5) reusing `ps.scan_pipeline_runs`; a test plants an in-flight `state.json` with an old
  report and asserts it survives. — confidence: high
- [non-blocking] **`prune history` deleting the earliest roll** makes `first_seen_at` return
  `None`, re-registering a long-lived job and shifting its due window. Mitigated by keeping
  `prune history` opt-in and off by default; if it is ever wired up, exclude the most recent
  roll unconditionally. — confidence: high
- [non-blocking] **Config schema growth** — a new top-level `retention:` key plus a new
  `retention.yaml` filename in `jobs.d/`. Any unknown-key strictness in `validate` must be
  updated in the same commit or a configured file will start failing validation. Since both
  `jobs.yaml` and `jobs.d/` are host-specific and not committed as live config
  (`plan-v1.md:315-320`), an operator's existing file keeps working unchanged — rotation is off
  by default, so no migration is required. — confidence: high
- [non-blocking] **Rotation is a no-op in practice** until an operator sets a threshold, so this
  PR delivers a mechanism with no live user. That is intentional and is what the issue log
  (2026-08-27) asked for; the risk is that untested-in-anger code sits dormant. Mitigated by
  tests that force both thresholds, plus one real `tick` with a tiny `history_max_bytes` to prove
  the end-to-end path. — confidence: high
- [non-blocking] Spec-path hygiene (G-15): this file must stay at
  `docs/pipeline/runs/20260928T051515Z/spec.md`. Writing to a shared root path reintroduces the
  PR #28/#29 conflict. — confidence: high

## Acceptance criteria

1. `read_all` returns records from `history.jsonl` and every sibling `history-*.jsonl` in
   chronological order, with the live file's records last — [blocking] confidence: high — Test: `test_read_all_spans_rolled_files_in_chronological_order`
2. Three rolls within one calendar month read back in write order (guards the §1 naming trap) —
   [blocking] confidence: high — Test: `test_three_rolls_in_one_month_read_in_write_order`
3. `last_terminal_run`, `first_seen_at`, `has_ever_been_seen`, `find_stale_running` and
   `is_currently_running` each return the same value whether the records live in the live file
   or in a roll — [blocking] confidence: high — Test: `test_history_queries_span_rolled_files`
4. A roll leaves the live `history.jsonl` present, empty, and immediately appendable, and no
   record is lost or duplicated by the rotation — [blocking] confidence: high — Test: `test_maybe_rotate_preserves_all_records`
5. `maybe_rotate` rotates when `history_max_bytes` is exceeded and does not when under it;
   same for `history_max_age_days` — [blocking] confidence: high — Test: `test_maybe_rotate_triggers_on_size_and_age`
6. With both thresholds unset (the default), `maybe_rotate` never rotates regardless of file
   size or age — [blocking] confidence: high — Test: `test_maybe_rotate_is_off_by_default`
7. `run_tick` rotates when the threshold is met, and a rotation that raises is logged and does
   not fail the tick — [blocking] confidence: high — Test: `test_run_tick_rotates_and_survives_rotation_error`
8. A collision on the target roll filename skips the rotation and logs a warning rather than
   inventing an out-of-order suffix — [blocking] confidence: high — Test: `test_maybe_rotate_skips_on_collision`
9. `retention:` parses in single-file `jobs.yaml` and in `jobs.d/retention.yaml`; unknown keys
   under `retention` are a `ConfigError`; absent config yields the documented defaults
   (rotation off, `reports_max_age_days: 90`) — [blocking] confidence: high — Test: `test_retention_parses_in_both_layouts`
10. `retention.yaml` in a `jobs.d/` directory is not treated as a job file (mirrors the existing
    `defaults.yaml` exclusion) — [blocking] confidence: high — Test: `test_retention_yaml_is_not_a_job_file`
11. `prune reports --dry-run` lists candidates and deletes nothing; `prune reports` without
    `--yes` exits non-zero with the `gc`-style refusal and deletes nothing — [blocking]
    confidence: high — Test: `test_prune_reports_requires_yes`
12. `prune reports --yes` deletes reports older than the window and keeps newer ones — [blocking]
    confidence: high — Test: `test_prune_reports_deletes_only_expired`
13. `prune reports` protects the reports and `state.json` of any in-flight pipeline run regardless
    of age — [blocking] confidence: high — Test: `test_prune_reports_protects_inflight_pipeline_run`
14. Nothing is ever deleted without an explicit command: no tick, no scheduled job, and no code
    path outside `prune` unlinks a report or a roll — [blocking] confidence: high — Test: `test_no_automatic_deletion_outside_prune`
15. A per-entry `OSError` during the sweep is counted and does not abort the remaining entries —
    [non-blocking] confidence: high — Test: `test_prune_reports_survives_per_entry_error`
16. `validate` warns (exit 0) when `history_max_bytes` is below 1 MiB — [non-blocking]
    confidence: medium — Test: `test_validate_warns_on_tiny_history_max_bytes`
17. `docs/plan-v1.md:415` and `:650` no longer claim log rotation is out of scope, and
    `docs/process/issues/021-log-rotation.md` is `status: done` with a Log entry — [non-blocking]
    confidence: medium — Test: `test_plan_no_longer_defers_log_rotation`

## Verification beyond the suite

`uv run pytest` + `uv run ruff check .` + `uv run ruff format --check .` green; then one real
end-to-end: set `history_max_bytes` to something tiny in a scratch config, run one `tick`, and
confirm a `history-<ts>.jsonl` roll appears, the live file is non-empty afterwards, and
`herdr-routines history <job>` still lists the run — i.e. transparency verified against a real
rotation, not only a fixture one.

## Changelog v1→v2

Editorial pass. No design change, no new requirement, no renamed or removed test, and no
change to any `blocking`/`non-blocking` call or confidence tier.

- **The `## Acceptance criteria` section was audited, not added.** v2 was requested to *add*
  it, but v1 already shipped one (commit `7b49d09`, 17 numbered items). It was checked against
  the required shape instead of being duplicated or rewritten — a second section would have left
  two competing acceptance lists for the implementer to reconcile. Post-audit state: 17 items
  numbered 1–17 with no gaps, all 17 terminating in `Test: <exact test name>`, 14 `[blocking]`
  and 3 `[non-blocking]`, and 15 `confidence: high` / 2 `confidence: medium`. — confidence: high
- **Item 16's confidence tier no longer wraps.** v1 broke the line between `confidence:` and
  `medium`, so the tier was present for a human but invisible to anything matching
  `confidence: <tier>` — the same 16-of-17 count this pass is built to prevent. The value is now
  inline with the key, as in every other item. — confidence: high
- **Item 10's continuation line re-indented** from 5 spaces to 4, so it aligns with the wrapped
  list body rather than hanging one space inside it. Cosmetic only. — confidence: high
- **Coverage of the issue's named acceptance surfaces re-checked and left unchanged.** Issue 021
  names `history` / `ps` / `scheduled`; the Problem table adds `digest`, `auto_fix`,
  `pick_feature` and `pipeline_watchdog` as unlisted readers. All are already reached — the
  three history readers by items 1–4 via the `read_all` transparency argument in §2,
  `ps`/`pick_feature`/`pipeline_watchdog` by item 13's protected set. No item was added,
  because the existing items cover the surfaces; the mapping is recorded here so the next
  reviewer does not re-derive it. — confidence: high
- **Not yet verified, and deliberately so:** no test in §Files touched exists on disk
  (`tests/test_reports_prune.py` is new, and no `retention`/`maybe_rotate` symbol exists under
  `src/`). Every test name above is a specification to be written by the implementation, not a
  reference to a passing test. Tiering in this spec is a judgement about what must block the
  PR, not an observation of current behaviour. — confidence: high

