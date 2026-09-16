# Spec — Model selection per job beyond claude/opencode (018) — 20260914T050000Z — v2

Implements `docs/process/issues/018-model-selection-per-job.md`. Per-run spec at `docs/pipeline/runs/20260914T050000Z/spec.md` (per-run path avoids PR shared-path conflict).

## Problem

`model` is wired only for two `agent_kind` values (`src/herdr_routines/config.py:61`):

```python
AGENT_MODEL_FLAGS = {"claude": "--model", "opencode": "-m"}
```

`VALID_AGENT_KINDS` (`config.py:30`) has exactly 22 entries (`pi`, `claude`, `codex`, `gemini`, `cursor`, `devin`, `agy`, `cline`, `omp`, `mastracode`, `opencode`, `copilot`, `kimi`, `kiro`, `droid`, `amp`, `grok`, `hermes`, `kilo`, `qodercli`, `qwen`, `maki`) so 20 kinds reject `model`.

`src/herdr_routines/herdr.py:620` `build_agent_start_args` appends `["--", flag, model]` where `flag = AGENT_MODEL_FLAGS.get(kind)`. `src/herdr_routines/config.py:633-640` rejects `model` for any other kind (`ConfigError` with `sorted(AGENT_MODEL_FLAGS)` in message). `src/herdr_routines/cli.py:583` `_cmd_validate` surfaces that error — for single-file layout as a caught `ConfigError` → `error:` + exit 1, for `jobs.d/` layout as `config.errors` collected per-file in `load_config_dir:438-482` → `problems` → `error:` + exit 1. The wiring works, but:

1. Only two of 22 `VALID_AGENT_KINDS` support `model`. Issue 018 acceptance requires at least one more kind honored, with its native flag documented alongside the existing entries.
2. A *value* typo (`model: "claude-sonent-4"`) passes both `config.py:634` (non-empty string check — actually type check at 634 + kind check at 636) and `validate` and only fails at `herdr agent start -- <flag> <model>` runtime, after pane/worktree creation and `agent_start` timeout. The issue asks for a catalog / existence check that fails validation instead (log notes this may be split out if no demand).

Current runtime guard in `herdr.py:642-647` (`ValueError` if `AGENT_MODEL_FLAGS.get(kind)` is None) is unreachable for config-loaded jobs (they were already rejected at load) but is the only defense for programmatic `build_agent_start_args` callers. — confidence: high

## Approach

Minimal, flag-first extension. No catalog service; no Herdr API change. — confidence: high

### 1. Extend `AGENT_MODEL_FLAGS` (`src/herdr_routines/config.py:61`) — [blocking]

Add at least one additional kind with a pinned-down native flag, verified empirically before commit (not guessed). Candidates in priority order:

- `codex` → `--model` (OpenAI Codex CLI documents `--model <id>`; most requested after claude/opencode; also covers issue 018's "actually wanting either" trigger if demand materializes). — confidence: high
- `gemini` → `--model` (Gemini CLI `gemini --model` is documented). — confidence: medium (flag spelling needs empirical confirmation)
- `cursor` → `--model` (cursor-agent `--model`). — confidence: medium

Verification step (must be done by implementer, recorded in PR description): for each candidate, run the real agent binary's `--help` (or `herdr --help` integration docs if the binary is absent on CI) and confirm the flag spelling; add a comment above the dict citing the source version (e.g. `# codex 0.52.0 --help: --model <model>`), matching the existing comment at `config.py:57` that cites `herdr 0.8.2`.

No change to `VALID_AGENT_KINDS`; only the flag map grows. `build_agent_start_args` (`herdr.py:620-649`) needs no code change — it already reads the map. — confidence: high

### 2. Keep (and document) validation as the acceptance-2 mechanism — [blocking]

Acceptance bullet 2 is already satisfied by `config.py:636-640` + `cli.py:583-661`, but the spec makes it explicit and regression-tested:

- `load_config` / `load_config_dir` → `ConfigError` (single-file) or per-file `config.errors` entry (directory) if `model is not None and agent_kind not in AGENT_MODEL_FLAGS`, message includes `f"(supported: {sorted(AGENT_MODEL_FLAGS)})"` so the error names the allowlist. — confidence: high
- `validate` (`cli.py:583-661`) prints `error: <job>: 'model' is not supported for agent_kind 'X' (supported: [...])` (single-file) or `error: <file>: ...` via `config.errors` (directory) and exits 1 (no soft `warning:`). Directory layout also logs each error via `log.warning` at `config.py:495` but `validate` still treats `problems` as blocking. — confidence: high
- `fallback_model` keeps its existing asymmetry (`config.py:642-657`): per-job `fallback_model` for unsupported kind is an error; inherited via `defaults.yaml` is inert (`None`). This is intentional (PR #65) and unchanged. — confidence: high

### 3. Optional catalog check — out of scope for v1, but spec reserves the seam — [non-blocking]

A full model-name allowlist per kind (e.g. `claude: [sonnet, opus, haiku]`, `codex: [gpt-5, o3, ...]`) drifts with provider releases and would need a refresh mechanism. For this issue the flag-existence check already satisfies acceptance 2. If a catalog is desired later, add it as a soft `warning:` in `validate` (not a blocking error) behind an optional `model_catalog: {kind: [names]}` file or regex, so a typo warns without blocking `tick` on a stale catalog. This spec does not require it; the implementer may add a minimal non-empty-string / no-whitespace guard if they want a value-level check without maintaining a catalog. — confidence: high

### 4. Docs and examples — [non-blocking]

Update `docs/plan-v1.md:238-239` comment that lists the two flags (`--model` for claude, `-m` for opencode), and add the new entry to `deploy/jobs.d/` (e.g. new example yaml or comment in `deploy/jobs.example.yaml`) showing `agent_kind: codex` + `model:`. — confidence: high

## Files touched

- `docs/pipeline/runs/20260914T050000Z/spec.md` — this file (per-run spec).
- `src/herdr_routines/config.py:61` — add new entry/entries to `AGENT_MODEL_FLAGS` with source comment; no change to `_JOB_ALLOWED_KEYS` / `Job` dataclass / `VALID_AGENT_KINDS`. — confidence: high
- `src/herdr_routines/herdr.py:620` — no logic change; `build_agent_start_args` already consumes the map. Update its docstring if it enumerates supported kinds. — confidence: high
- `src/herdr_routines/cli.py:583` — no logic change; `validate` already surfaces `ConfigError`/`config.errors`. Add regression test that `model` for unsupported kind is an `error:` not a `warning:`. — confidence: high
- `tests/test_config.py:281` (`test_model_is_accepted_for_supported_agent_kinds` parametrize) — add new kind(s) to the matrix; add `test_model_rejected_for_unsupported_kind_mentions_supported_list`. — confidence: high
- `tests/test_herdr.py` — add `test_build_agent_start_args_new_kind_includes_flag` asserting `["--", "<flag>", "<model>"]` suffix. — confidence: high

Not touched: `src/herdr_routines/schedule.py`, `src/herdr_routines/history.py`, `src/herdr_routines/tick.py` (pipeline/gated dispatch), `src/herdr_routines/runner.py` beyond existing `RunOutcome` handling, systemd units. — confidence: high

## Risks

- [blocking] Wrong native flag for the new kind breaks `agent start` at runtime (silent until a real run). Mitigated by empirical `--help` verification before commit and a tier-2 `build_agent_start_args` argv assertion; flag is a single string, so the test is exact. — confidence: high
- [blocking] `AGENT_MODEL_FLAGS` drifts when Herdr adds/renames agent CLIs or flags change upstream. Mitigated by pinning the source version in the comment and re-verifying on Herdr upgrade; `validate` error message always prints the current allowlist so misconfiguration is visible. — confidence: high
- [non-blocking] Catalog staleness if a value-level allowlist is added: provider adds a model, catalog rejects it until updated. Mitigated by keeping any catalog check as `warning:` (exit 0) not `error:`, or deferring catalog entirely and relying on the flag-existence check which already satisfies acceptance 2. — confidence: high
- [non-blocking] `fallback_model` inheriting via `defaults.yaml` being inert for unsupported kinds surprises operators who expect it to error. This is intentional and documented at `config.py:654`; spec preserves it. — confidence: high
- [non-blocking] Spec-path hygiene (G-15): must stay at `docs/pipeline/runs/<run_id>/spec.md`; writing to a shared path reintroduces PR #28/#29 merge conflict. — confidence: high

## Acceptance criteria

1. `AGENT_MODEL_FLAGS` in `src/herdr_routines/config.py:61` contains at least one additional agent_kind beyond `claude`/`opencode` (e.g. `codex`) with its native model flag documented alongside the existing entries, including a source-version comment citing the binary's `--help` output — [blocking] confidence: high — Test: test_model_is_accepted_for_supported_agent_kinds
2. Loading config with `model` set for a supported new kind succeeds and the resulting `Job.model` equals the supplied value — [blocking] confidence: high — Test: test_model_accepted_for_codex_kind
3. Loading config with `model` set for any `VALID_AGENT_KINDS` not in `AGENT_MODEL_FLAGS` raises `ConfigError` whose message includes the sorted supported list — [blocking] confidence: high — Test: test_model_rejected_for_unsupported_kind_mentions_supported_list
4. `build_agent_start_args` for the new kind with `model` appends `["--", "<flag>", "<model>"]` as the last three argv elements — [blocking] confidence: high — Test: test_build_agent_start_args_codex_includes_model_flag
5. `build_agent_start_args` for an unsupported kind with `model` raises `ValueError` naming the supported list and does not shell out — [blocking] confidence: high — Test: test_agent_start_rejects_model_for_unsupported_kind
6. `herdr-routines validate` reports `model` for unsupported kind as a blocking `error:` (exit 1), not a `warning:` — [blocking] confidence: high — Test: test_validate_rejects_model_for_unsupported_kind_as_error
7. `fallback_model` per-job for unsupported kind remains a `ConfigError`, while `fallback_model` inherited only via `defaults.yaml` remains inert (`None`) for unsupported kind — [non-blocking] confidence: high — Test: test_defaults_fallback_model_is_inert_for_unsupported_agent_kind
8. Existing `claude` (`--model`) and `opencode` (`-m`) flag wiring is unchanged and regression-tested via `build_agent_start_args` argv assertions — [blocking] confidence: high — Test: test_agent_start_passes_claude_model_via_native_flag
9. Existing `claude`/`opencode` `opencode` flag wiring second leg unchanged — [blocking] confidence: high — Test: test_agent_start_passes_opencode_model_via_native_flag
10. `docs/plan-v1.md:238` comment enumerating model flags is updated to include the new kind/flag, and an example job (in `deploy/jobs.d/` or `deploy/jobs.example.yaml`) demonstrates `agent_kind: <new>` with `model:` — [non-blocking] confidence: medium — Test: test_plan_docs_list_model_flags_for_new_kind

## Changelog v1→v2

- Fixed `VALID_AGENT_KINDS` count: v1 said "~22", verified as exactly 22 (enumerated). confidence: high
- Fixed line references and clarified validation path: v1 cited `cli.py:583` as single error path; v2 distinguishes single-file `ConfigError` → `error:` vs `jobs.d/` `config.errors` → `problems` → `error:` (both blocking, both exit 1), and notes the `log.warning` at `config.py:495` for directory errors. confidence: high
- Fixed `config.py:636` reference to `633-640` (type check at 634, kind check at 636-640) and `fallback_model` reference to `642-657` with correct asymmetry description. confidence: high
- Added explicit `## Acceptance criteria` section (10 numbered items) with `Test: <name>` runnable test names covering: new-kind acceptance, new-kind rejection message, `build_agent_start_args` argv, `ValueError` guard, `validate` blocking error, `fallback_model` asymmetry preservation, and regression + doc updates. confidence: high
- Added `blocking`/`non-blocking` tiers and `confidence:` markers to each Approach subsection and each Acceptance criterion (Risks already had them in v1; now Approach and Acceptance also do). confidence: high
- Clarified Approach §4 docs scope: `deploy/jobs.d/` preferred for example (per-directory layout) and `deploy/jobs.example.yaml` comment update; noted `herdr.py:620-649` full range vs prior `620` shorthand. confidence: medium
- No change to minimal flag-first design; catalog remains out-of-scope deferred to soft `warning:` seam. confidence: high
