# Spec — Digest omits disabled jobs by default (046) — 20260908T050000Z

## Problem
`herdr-routines digest` (issue 010) enumerates every job's last terminal state from `history.jsonl` + `reports/` without checking `Job.enabled`. A disabled job's frozen last failure remains indistinguishable from an active failure. On the first real Pi run (2026-09-06) 3 of 5 red lines were stale disabled jobs (60% noise), training the reader to skim and defeating the digest's purpose (issue 009: one notification, not noise).

## Approach
Digest already loads `RoutinesConfig`; `Job.enabled` is available at render time. No new state store (issue 010 constraint: read only `history.jsonl`, reports dir, job config).

- `build_digest(config, history_path, reports_dir, *, include_disabled=False)`: filter out jobs where `job.enabled is False` unless `include_disabled` is true. Preserve config order. A disabled job's `DigestRow` when included must carry `enabled=False` so rendering can mark it as historical, not as a failure.
- `render_digest(rows, *, timezone, now, all_disabled=False)` (or equivalent signal): default path omits disabled rows entirely. With `--include-disabled`, disabled rows render as non-alarming, e.g. `- fitted-implementer: disabled (last: failed at 2026-09-02 05:00 -03)` with optional `— <report>` only if the file still exists. Must not render as `- <name>: failed at ...`.
- Empty handling: distinguish "no jobs configured" (config.jobs empty) from "all jobs disabled and filtered" (config non-empty but filtered rows empty). Latter renders explicit line e.g. `(all jobs disabled)` or `(no enabled jobs)` rather than empty body or "no jobs configured".
- `digest_now()` and CLI `_cmd_digest` thread the flag through. CLI adds `digest --include-disabled` (default false, no short flag needed) and forwards to `digest_now`/`build_digest`.
- Enabled jobs' behavior unchanged: `failed`/`missed`/`done` etc. still render as `- <name>: <state> at <local ts> — <report>` exactly as before.
- Out of scope: staleness guard for enabled jobs (issue 046 Design notes it as separate future work).

## Files touched
- `src/herdr_routines/digest.py` — add `enabled` to `DigestRow` (or parallel handling), filter in `build_digest`, non-alarming render branch for disabled rows, explicit all-disabled empty message.
- `src/herdr_routines/cli.py` — add `--include-disabled` to `digest` subparser, pass through `_cmd_digest` → `digest_now`.
- `tests/test_digest.py` — stage 3 adds `test_digest_omits_disabled_job`, `test_digest_include_disabled_marks_not_fails`, `test_digest_still_reports_enabled_failure`, `test_digest_all_disabled_is_explicit` (no prod code in stage 1).
- `docs/process/issues/046-digest-reports-disabled-jobs-as-failing.md` — flipped `status: open` → `done` by the implementing PR (stage 3), not here.
- `deploy/systemd/herdr-routines-digest.service` — no change required (still `digest --notify --timezone America/Montevideo` without flag; default omits disabled, which is desired).

## Risks
- Regressing enabled-job output: filter must key on `job.enabled`, not on state; enabled `failed`/`missed` must remain verbatim or real failures go unnoticed.
- Ambiguous empty digest: `render_digest([], ...)` currently means "no jobs configured"; after filtering it can also mean "all disabled". Caller must disambiguate or the digest reads as empty on a fully-disabled host.
- `--include-disabled` marking must not reuse the failure format (`: failed at`); tests will assert absence of failure phrasing and presence of `disabled` marker. Historical state should be secondary (`last:`) and plain.
- Never-run disabled jobs: `state is None` + `enabled is False` should render as `disabled` (or `disabled — never run`), not as `never run` alone, to preserve the flag's purpose ("why is nothing running?").
- Report link handling unchanged: `_report_path_for` only when file exists; disabled rows follow same rule.
- No new I/O or state: `build_digest` still calls only `last_terminal_run` per job; no schedule staleness check added here to avoid scope creep.
