# Spec — Digest omits disabled jobs by default (046) — 20260908T050000Z

## Problem
`herdr-routines digest` (issue 010) enumerates every job's last terminal state from `history.jsonl` + `reports/` without checking `Job.enabled`. A disabled job's frozen last failure remains indistinguishable from an active failure. On the first real Pi run (2026-09-06) 3 of 5 red lines were stale disabled jobs (60% noise), training the reader to skim and defeating the digest's purpose (issue 009: one notification, not noise).

## Approach
Digest already loads `RoutinesConfig`; `Job.enabled` is available at render time. No new state store (issue 010 constraint: read only `history.jsonl`, reports dir, job config).

- `build_digest(config, history_path, reports_dir, *, include_disabled=False)`: filter out jobs where `job.enabled is False` unless `include_disabled` is true. Filter must key on `job.enabled` (boolean false), not on state value; enabled jobs' rows are unchanged. Preserve config definition order in the returned list. A disabled job's `DigestRow` when included must carry `enabled=False` so rendering can mark it as historical, not as a failure. `DigestRow` is extended with an `enabled: bool` field (default `True` for backward compatibility/plaine helpers), or equivalent per-row signal.
- `render_digest(rows, *, timezone, now, all_disabled=False)` (or equivalent signal that lets the renderer distinguish the two empty cases): default path omits disabled rows entirely. With `--include-disabled`, disabled rows render as non-alarming, e.g. `- fitted-implementer: disabled (last: failed at 2026-09-02 05:00 -03)` with optional `— <report>` only if the file still exists. Must not render as `- <name>: failed at ...` or `- <name>: missed at ...`. Historical state when present is secondary (`last:`) and plain. When `state is None` (never had a terminal run) and `enabled is False`, render as `disabled` (e.g. `disabled — never run` or `disabled (never run)`) rather than `never run` alone, to preserve the flag's diagnostic purpose ("why is nothing running?").
- Empty handling: distinguish "no jobs configured" (`config.jobs` empty) from "all jobs disabled and filtered" (config non-empty but filtered rows empty after omitting disabled). Latter must render an explicit line, e.g. `(all jobs disabled)` or `(no enabled jobs)` rather than empty body or `no jobs configured`. `digest_now()` is responsible for passing the disambiguating signal to `render_digest` (either `all_disabled` flag or equivalent inference from config + filtered rows); `render_digest([], ..., all_disabled=True)` vs `render_digest([], ..., all_disabled=False)` must produce different bodies.
- `digest_now()` and CLI `_cmd_digest` thread the flag through. CLI adds `digest --include-disabled` (store_true, default false, no short flag needed) and forwards to `digest_now`/`build_digest`. The systemd unit `deploy/systemd/herdr-routines-digest.service` stays `digest --notify --timezone America/Montevideo` without the flag — default omits disabled, which is desired.
- Enabled jobs' behavior unchanged: `failed`/`missed`/`done` etc. still render as `- <name>: <state> at <local ts> — <report>` exactly as before, with local-time formatting via `timezone` and report link only when `_report_path_for` finds an existing file. Disabled rows follow the same report-link rule when included.
- Out of scope: staleness guard for enabled jobs (issue 046 Design notes it as separate future work). No schedule staleness check, no new I/O.

## Files touched
- `src/herdr_routines/digest.py` — add `enabled` to `DigestRow` (or parallel handling), filter in `build_digest`, non-alarming render branch for disabled rows, explicit all-disabled empty message.
- `src/herdr_routines/cli.py` — add `--include-disabled` to `digest` subparser, pass through `_cmd_digest` → `digest_now`.
- `tests/test_digest.py` — stage 3 adds `test_digest_omits_disabled_job`, `test_digest_include_disabled_marks_not_fails`, `test_digest_still_reports_enabled_failure`, `test_digest_all_disabled_is_explicit` (no prod code in stage 1).
- `docs/process/issues/046-digest-reports-disabled-jobs-as-failing.md` — flipped `status: open` → `done` by the implementing PR (stage 3), not here.
- `deploy/systemd/herdr-routines-digest.service` — no change required (still `digest --notify --timezone America/Montevideo` without flag; default omits disabled, which is desired).

## Risks
- [blocking] Regressing enabled-job output: filter must key on `job.enabled`, not on state; enabled `failed`/`missed` must remain verbatim or real failures go unnoticed. — confidence: high
- [blocking] Ambiguous empty digest: `render_digest([], ...)` currently means "no jobs configured"; after filtering it can also mean "all disabled". Caller must disambiguate or the digest reads as empty on a fully-disabled host. — confidence: high
- [blocking] `--include-disabled` marking must not reuse the failure format (`: failed at`); tests will assert absence of failure phrasing and presence of `disabled` marker. Historical state should be secondary (`last:`) and plain. — confidence: high
- [non-blocking] Never-run disabled jobs: `state is None` + `enabled is False` should render as `disabled` (or `disabled — never run`), not as `never run` alone, to preserve the flag's purpose ("why is nothing running?"). — confidence: medium
- [non-blocking] Report link handling unchanged: `_report_path_for` only when file exists; disabled rows follow same rule. — confidence: high
- [non-blocking] No new I/O or state: `build_digest` still calls only `last_terminal_run` per job; no schedule staleness check added here to avoid scope creep. — confidence: high

## Acceptance criteria
1. [blocking] Default digest omits disabled jobs entirely: a job with `enabled: false` and a last terminal state of `failed` (or `missed`) does not appear in the default `build_digest`/`render_digest` output and order of remaining enabled jobs is preserved — confidence: high — Test: test_digest_omits_disabled_job
2. [blocking] `--include-disabled` shows disabled jobs but never as failures: with `include_disabled=True` (or `digest --include-disabled`) the disabled job appears exactly once, the line contains `disabled` (case-insensitive), does not contain `: failed at` / `: missed at` for that job, and historical state if shown uses `last:` as secondary plain text with optional `— <report>` only when the report file exists — confidence: high — Test: test_digest_include_disabled_marks_not_fails
3. [blocking] Enabled jobs still report failures verbatim: an enabled job whose last terminal state is `failed` (or `missed`) still renders as `- <name>: <state> at <local ts> — <report>` exactly as before, regardless of other jobs being disabled — confidence: high — Test: test_digest_still_reports_enabled_failure
4. [blocking] All-disabled filtered digest is explicit, not empty or "no jobs configured": when config is non-empty but every job is disabled and `include_disabled` is false, the rendered digest contains an explicit marker such as `(all jobs disabled)` or `(no enabled jobs)` and does not contain `no jobs configured`; the empty-config case (`config.jobs == ()`) still renders `no jobs configured` — confidence: high — Test: test_digest_all_disabled_is_explicit
5. [non-blocking] Never-run disabled job is marked disabled, not "never run" alone: a disabled job with `state is None` rendered via `--include-disabled` contains `disabled` (e.g. `disabled — never run`) and is not rendered as bare `never run` — confidence: medium — Test: test_digest_disabled_never_run_marked_disabled
6. [non-blocking] CLI flag threads through correctly: `digest --include-disabled` is accepted by the parser (default false, no short flag) and forwards `include_disabled=True` to `digest_now`/`build_digest`; without the flag the default filtering from (1) applies — confidence: medium — Test: test_digest_cli_include_disabled_flag

## Changelog v1→v2
- Added `## Acceptance criteria` with 6 numbered items; the four required tests appear verbatim as `Test: test_digest_omits_disabled_job`, `Test: test_digest_include_disabled_marks_not_fails`, `Test: test_digest_still_reports_enabled_failure`, `Test: test_digest_all_disabled_is_explicit`, plus two non-blocking extras for never-run disabled and CLI flag threading.
- Added `blocking`/`non-blocking` tier vocabulary and `confidence: high|medium|low` annotations to both Risks and Acceptance criteria (previously absent).
- Strengthened Approach: clarified `DigestRow.enabled` per-row signal and backward-compat default, precise empty-state disambiguation contract (`all_disabled` flag or equivalent inference by `digest_now`), never-run disabled rendering (`disabled — never run`), strict boolean `job.enabled is False` filter with order preservation, and report-link parity for disabled rows.
- Tightened Risks with explicit blocking/non-blocking labels and confidence ratings; no change to Files touched list or Problem statement beyond adding diagnostic precision.

## Review
- blocking: items 1–4 in Acceptance criteria cover the PI-measured 60% noise bug and must not regress enabled failures or empty semantics.
- non-blocking: items 5–6 cover diagnostic edge cases (never-run disabled, CLI threading) that improve debuggability but do not gate the core noise fix.
- confidence: high for blocking items (directly tied to issue 046's four acceptance criteria and existing `Job.enabled` + history contract); medium for non-blocking extras (edge-case rendering and CLI plumbing).
