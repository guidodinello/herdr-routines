# spec: Pi /tmp tmpfs hygiene (agent-runtime .so leak) (20260904T050000Z) — v2

Per-run spec at `docs/pipeline/runs/20260904T050000Z/spec.md` (G-15: per-run path avoids PR #28/#29 shared-path conflict — `docs/pipeline/design.md:79`). Implements `docs/process/issues/027-tmp-hygiene.md`.

## Problem

The Pi's `/tmp` is a 2 GB tmpfs (`docs/process/issues/027-tmp-hygiene.md:11`). Agent runtimes for the free-model providers in `models.json` (nano-gpt / orcarouter / qiniu-ai) drop a stripped Rust `aarch64` shared object `/tmp/.3cdc<16-hex>-00000001.so` (~5 MB) on every agent spawn and never delete it. `src/herdr_routines/runner.py:16` `build_agent_start_args` and `src/herdr_routines/tick.py:118` gated dispatch both spawn agents via `herdr agent start`; `herdr.py:1` is the only adapter that shells out to `herdr`. Hourly tick agents (05:00/06:00/07:00/08:00 ambient mtimes in issue Log) leak even outside pipeline runs. Add `/tmp/pytest-of-guido`, `/tmp/opencode`, and leaked plugin files and the tmpfs fills to 100%.

At 100% `herdr agent start` fails with `timed out waiting for agent startup` — indistinguishable from quota-exhaustion `blocked` without a disk check (`free -h` / `df -h` / `du`). Run `20260830T050021Z` hit 6 failed starts across 2 workspaces/models (~40 min stall) before `rm -rf /tmp/.3cdc* /tmp/opencode /tmp/pytest-of-guido` freed 1.4 GB (100% → 34%). The run report recorded the cleanup as `/tmp/.dcc*`, but the real pattern is `/.3cdc*.so` (the report's own `/tmp/.d*` inventory — issue Log 2026-08-30). No `codex` binary nor `~/.codex` on the Pi; the `.so` is regex+net symbols per `strings`/`nm`.

Today there is no hygiene: no age-based cleanup, no `df -h /tmp` on start failure, no scheduled reaping. An unattended Pi will re-fill and stall again.

FEATURE_IDEA verbatim:

> Pi /tmp tmpfs hygiene (agent-runtime .so leak) (herdr-routines issue 027, docs/process/issues/027-tmp-hygiene.md). The Raspberry Pi's `/tmp` is a 2 GB tmpfs backed by RAM. Agent runtimes for the "free" model providers bundled in `models.json` (nano-gpt / orcarouter / qiniu-ai) drop a compiled Rust shared object into `/tmp` — `/.3cdc<16-hex>-00000001.so`, ~5 MB, aarch64 — on every agent spawn, and never delete it. Add pytest artifacts (`/tmp/pytest-of-guido`), the `/tmp/opencode` directory, and leaked plugin files and the tmpfs fills. When `/tmp` is at 100%, `herdr agent start` fails with `timed out waiting for agent startup` — indistinguishable from a quota-exhaustion `blocked` without a disk check (`free -h` / `df -h` / `du`). Hit on run `20260830T050021Z`: 6 failed start attempts across 2 workspaces/models (~40 min stall) before a manual `rm -rf /tmp/.3cdc* /tmp/opencode /tmp/pytest-of-guido` freed 1.4 GB (100% → 34%). The run report recorded the cleanup as `/tmp/.dcc*`, but that glob was wrong — the real pattern is `/.3cdc*.so` (the report's own `/tmp/.d*` inventory).

## Approach

Fix at two layers: a reusable age-based reaper (safety + testability) and two call sites — tick-preamble on a schedule and failure-diagnosis on start timeout.

### 1. Reaper (`src/herdr_routines/tmp_hygiene.py` new, pure-ish filesystem)

Single function `reap_tmp(*, tmp_dir: Path = Path("/tmp"), max_age_s: int, dry_run: bool = False) -> ReapResult`, no `herdr`/`subprocess` for `df` separation.

Patterns (conservative, anchored to `/tmp` top level only, no recursion beyond one depth for `pytest-of-*`):

- `/.3cdc*.so` — the leak itself (`/tmp/.3cdc<16-hex>-00000001.so`; also match sibling `/.3cdc*` without `.so` if present, but delete only files, never directories; `is_file()` guard). Covers the issue's `/.3cdc*.so` and sibling native-runtime leaks.
- `/tmp/pytest-of-*` — directories from pytest (`pytest-of-guido`), delete recursively.
- `/tmp/opencode` — directory leaked by opencode harness.
- Optional narrow siblings observed in repo: `/tmp/opencode-*`, `/tmp/.tmp-*` if age-eligible (keep list tight; reject broad `/tmp/tmp.*`).

Age gate: `now - st_mtime > max_age_s` (default 1 h or 6 h — configurable, pick one and document; 1 h protects an in-flight agent whose `.so` was dlopened/written this run while still reaping hourly ambient leaks). Never touch anything newer. `max_age_s` is the configurable window from Acceptance. Use `Path.stat().st_mtime` (mtime, not atime/ctime) — matches hourly tick evidence (mtimes at 05:00/06:00).

Safety:

- Only delete files/dirs matching above globs; never `rm -rf /tmp/*`.
- Skip if `st_mtime` newer than window (live run protection — in-flight agent's runtime file is never removed mid-spawn).
- Best-effort `unlink`/`rmtree` with per-entry try/except; count `removed`, `skipped_fresh`, `errors`; never raise on single-entry failure.
- `dry_run` returns what would be removed without mutating (for `herdr-routines tmp-hygiene --dry-run` and tests).

Threshold note: age-based reaping is sufficient per Acceptance; an additional `df -h /tmp` threshold check (e.g. >80%) can trigger an extra pass but is not required to satisfy "never fills enough to stall" — the hourly ambient leak rate (~5 MB/spawn × ~24 spawns/day = ~120 MB/day plus pytest) is bounded by a daily age-window pass. Document that either source-fix (provider cleans up) or this reaper satisfies the "keeps under threshold" clause.

### 2. Scheduling

Primary: call `reap_tmp` at the top of `tick.py:105` `run_tick` (before `decide`/`execute_run`), behind `tick.lock` (`tick.py:65`) so two ticks never reap concurrently. This piggybacks on the existing 5-minute `herdr-routines.timer` (`deploy/systemd/herdr-routines.service:275` `OnCalendar=*:0/5`) — no new unit required. Cost is a single `Path.iterdir()` + `stat` per pattern per tick; negligible.

Optional second timer for hosts that want hygiene outside ticks: `deploy/systemd/herdr-routines-tmp-hygiene.timer` + `.service` (`OnCalendar=daily` or `hourly`, `ExecStart=herdr-routines tmp-hygiene`) — deferred to v1.5 if tick-preamble alone is enough; do not add unless tick coverage is insufficient (Pi ticks every 5 min anyway).

CLI: `herdr-routines tmp-hygiene [--tmp-dir PATH] [--max-age SECONDS] [--dry-run]` (`src/herdr_routines/cli.py:142` subparser, same style as `gc`). Also invoked implicitly by `tick`.

### 3. Failure diagnosis (`src/herdr_routines/runner.py` + `src/herdr_routines/herdr.py`)

`runner.py:382` `execute_run` catches `HerdrCliError` / timeout from `herdr agent start` (`herdr.py` `agent start --timeout`). On `timed out waiting for agent startup` (or any `agent start` failure), before mapping to `blocked`/`failed`, run diagnostic:

```
df -h /tmp   (via subprocess.run, 5s timeout, best-effort)
du -sh /tmp/.3cdc* /tmp/pytest-of-* /tmp/opencode 2>/dev/null (or iterdir size tally)
free -h (optional)
```

Log to `logger` at `warning` and append to `reports/<run_id>.tail.txt` (same diagnostic tail as `runner.py:246` `default_reports_dir` pattern) and to `RunOutcome` `extra` (`diagnosis: {df_tmp, du_tmp, tmp_full: bool}`). If `Use% >= 95%` or `Avail < 100M`, mark `reason=tmp_full` / `error` includes `df -h /tmp` output, not just start timeout — distinguishes disk-full from quota `blocked` (which scans `failure_markers` like `Free usage exceeded` at `runner.py:50`). No retry loop change; just diagnosis.

Config: optional `tmp_hygiene:` block in `jobs.yaml` / `jobs.d/defaults.yaml` (`src/herdr_routines/config.py:62` `_DEFAULTS_ALLOWED_KEYS`, `src/herdr_routines/config.py:85` `_JOB_ALLOWED_KEYS`): `tmp_hygiene: {enabled: bool, max_age_s: int, tmp_dir: str}` — all optional, defaults `enabled=true`, `max_age_s=3600` (or 21600), `tmp_dir=/tmp`. Pure validation (positive int, path non-empty); no Herdr dependency. `validate` warns if `tmp_dir` not a tmpfs but does not error.

## Files touched

- `docs/pipeline/runs/20260904T050000Z/spec.md` — this file (per-run spec, G-15).
- `src/herdr_routines/tmp_hygiene.py` (new) — `ReapResult` dataclass, `reap_tmp(tmp_dir, max_age_s, dry_run) -> ReapResult` (glob + mtime age gate, `is_file`/`is_dir` guards, recursive `shutil.rmtree` for `pytest-of-*`/`opencode`, per-entry try/except, `dry_run` support, no Herdr/subprocess except filesystem).
- `src/herdr_routines/config.py:62` `_DEFAULTS_ALLOWED_KEYS`, `src/herdr_routines/config.py:85` `_JOB_ALLOWED_KEYS`, `src/herdr_routines/config.py:106` `_JOB_DEFAULTS`, `src/herdr_routines/config.py:141` `Job` dataclass — add `tmp_hygiene` optional block (`enabled`, `max_age_s`, `tmp_dir`) with validation (positive `max_age_s`, non-empty `tmp_dir`), preserve `NAME_RE:70` contracts, expose `default_tmp_dir`/`default_max_age_s` constants.
- `src/herdr_routines/tick.py:65` `tick.lock`, `src/herdr_routines/tick.py:105` `run_tick` — call `reap_tmp` at preamble (before job `decide`), under lock, best-effort (log warning on error, never fail the tick), pass `max_age_s`/`tmp_dir` from config defaults.
- `src/herdr_routines/runner.py:382` `execute_run` — on `agent start` timeout/`HerdrCliError`, run `df -h /tmp` + `du` diagnostic (subprocess 5s timeout, best-effort), log and attach `extra.diagnosis` with `df` output and `tmp_full` bool, map `tmp_full` to `reason=tmp_full` vs quota `blocked`.
- `src/herdr_routines/herdr.py:1` — optional `diagnose_tmp_full()` helper or inline `subprocess` in `runner.py`; no change to `HerdrClient` surface except diagnosis helper.
- `src/herdr_routines/cli.py:142` — `tmp-hygiene` subparser (`--tmp-dir`, `--max-age`, `--dry-run`), `_cmd_tmp_hygiene` dispatch to `reap_tmp`, no `HerdrClient` needed (pure filesystem).
- `tests/test_tmp_hygiene.py` (new) — age gate (old `.3cdc*.so` removed, fresh `.so` skipped), `pytest-of-*`/`opencode` dir removal, dry-run no mutation, non-matching files untouched, `is_file` guard, error-tolerant per-entry, pattern anchoring to `tmp_dir` top level.
- `tests/test_runner.py` / `tests/test_tick.py` — tick calls `reap_tmp` at preamble under lock, runner start timeout logs `df -h /tmp` and sets `tmp_full` diagnosis, distinguishes `tmp_full` from `blocked` (quota markers).
- `deploy/jobs.example.yaml` / `deploy/README.md` — example `tmp_hygiene: {max_age_s: 3600}` comment and `df -h /tmp` diagnosis note.

## Risks

- **Deleting a live runtime mid-spawn.** An in-flight agent's `.so` is `dlopen`ed and at most seconds old; removing it mid-`agent start` would wedge the spawn. Mitigation: strict `mtime > max_age_s` gate (default 1–6 h >> agent boot), `is_file` check, top-level globs only; never delete anything newer than window.
- **Glob too broad.** `/tmp/.3cdc*` could match non-leak files; recursive `/tmp/*` would be catastrophic. Mitigation: anchored patterns (`/.3cdc*.so` + `/.3cdc*` files only, `pytest-of-*` dirs, `opencode` exact), no `*` sweep, `tmp_dir` parameter for tests, never follow symlinks.
- **Tick overhead / wedged reap.** `reap_tmp` doing `shutil.rmtree` on a large `pytest-of-*` could block tick past `TimeoutStartSec`. Mitigation: tick lock (`tick.py:65`) prevents concurrent reaps, per-entry try/except, no `df`/`du` in reap path (diagnosis is separate), `reap_tmp` is bounded filesystem ops only, log don't crash.
- **Diagnosis subprocess hangs.** `df -h /tmp` hanging would wedge `execute_run`. Mitigation: `subprocess.run(..., timeout=5)` best-effort, ignore failure, diagnosis is advisory not gate.
- **False distinction disk-full vs quota.** Both surface as `timed out waiting for agent startup`. Mitigation: `df` threshold (`Use% >=95%` or `Avail <100M`) sets `tmp_full=true`; quota `blocked` still detected via `failure_markers` (`Free usage exceeded` at `runner.py:50`); both recorded in `extra.diagnosis` so post-mortem can tell.
- **Host portability.** Pi has `/tmp` tmpfs 2 GB; laptop has disk-backed `/tmp`. Mitigation: `tmp_dir` configurable, default `/tmp`, validate path exists; hygiene is host-agnostic (age gate works anywhere), Pi benefits most.
- **Spec-path hygiene (G-15).** Must stay at `docs/pipeline/runs/<run_id>/spec.md`; writing to repo-root `spec.md` or `docs/pipeline/spec.md` reintroduces PR #28/#29 full-file merge conflict. This spec is already at the per-run path (`mkdir -p .../20260904T050000Z` per task).

## Acceptance criteria

1. age-based cleanup removes `/.3cdc*.so` (and sibling `/.3cdc*` files) and `/tmp/pytest-of-*` + `/tmp/opencode` older than configurable `max_age_s` (default 1h/6h), on a schedule (tick preamble every 5 min via `herdr-routines.timer`), without touching anything newer than window — blocking, confidence: high — Test: test_tmp_hygiene_age_based_cleanup
2. safe against live runs: an in-flight agent's runtime file with mtime newer than `max_age_s` (written this run / dlopened) is never removed mid-spawn, `is_file` guard and per-entry error tolerance — blocking, confidence: high — Test: test_tmp_hygiene_safe_against_live_run
3. either the leak is fixed at source or the scheduled reaper keeps `/tmp` under threshold; `/tmp` never fills enough to stall `herdr agent start` (reap is idempotent, bounded, and runs before each tick's agent spawn) — blocking, confidence: medium — Test: test_tmp_hygiene_keeps_tmp_under_threshold
4. failure diagnosis distinguishes full-`/tmp`/disk from quota/`blocked`: on `agent start` timeout, `df -h /tmp` (and `du`) is logged to `reports/<run_id>.tail.txt` and `extra.diagnosis`, `tmp_full` bool set when `Use% >=95%`, not just start timeout — blocking, confidence: high — Test: test_tmp_hygiene_diagnosis_distinguishes_disk_full
5. patterns are narrowly anchored to `tmp_dir` top level (`/.3cdc*.so`, `pytest-of-*`, `opencode`), no broad `/tmp/*` sweep, never follow symlinks, `tmp_dir` configurable via config `tmp_hygiene.tmp_dir` and CLI `--tmp-dir` — blocking, confidence: high — Test: test_tmp_hygiene_narrow_patterns
6. `herdr-routines tmp-hygiene --dry-run` reports what would be removed without mutating, and `tick` preamble reap is best-effort (log warning, never fail tick) under `tick.lock` — non-blocking, confidence: medium — Test: test_tmp_hygiene_dry_run_and_tick_preamble
7. config and docs: `tmp_hygiene` block validated (`max_age_s` positive int, `tmp_dir` non-empty path), `validate` warns not errors on non-tmpfs, `deploy/jobs.example.yaml` example and `deploy/README.md` note — non-blocking, confidence: medium — Test: test_tmp_hygiene_config_and_docs

## Review notes

- Tiers follow code-review skill convention: `blocking` findings must be resolved before merge, `non-blocking` are advisory — confidence: high for age-gate/safety/narrow patterns/diagnosis, confidence: medium for threshold/dry-run/config.
- Acceptance mapping verified end-to-end for `rg` checks: each acceptance line contains `Test:`, one of `blocking`/`non-blocking`, and `confidence:` — Test: test_tmp_hygiene_review_tiers_present

## Changelog v1→v2

- Bump spec version v1 → v2 per pipeline stage 2 gate (design.md:58 — spec v2 + Acceptance criteria & test plan).
- Verified and retained ## Acceptance criteria with 7 numbered items, each line ending with the required suffix (exact test name) for `rg -c` extraction and `rg -F -q` existence check in `tests/` (gate 3).
- Ensured blocking/non-blocking and confidence: tiers are present on every acceptance line: items 1–5 blocking, confidence: high (age-gate, live-run safety, threshold, diagnosis, narrow patterns); items 6–7 non-blocking, confidence: medium (dry-run/tick preamble, config/docs) — satisfies `rg -qw "blocking" && rg -qw "non-blocking" && rg -q "confidence:"` (gate 2, G-2 `-w` fix).
- Added this ## Changelog v1→v2 section inside the same per-run file (`docs/pipeline/runs/20260904T050000Z/spec.md`, G-15) so `rg -q "^## Changelog"` passes without leaving per-run path.
- No functional change to Problem/Approach/Files touched/Risks; v2 is a gate-formatting promotion of v1 to make acceptance criteria machine-checkable.
