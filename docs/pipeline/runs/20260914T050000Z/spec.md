# Spec — Model selection per job beyond claude/opencode (018) — 20260914T050000Z

Implements `docs/process/issues/018-model-selection-per-job.md`. Per-run spec at `docs/pipeline/runs/20260914T050000Z/spec.md` (per-run path avoids PR shared-path conflict).

## Problem

`model` is wired only for two `agent_kind` values (`src/herdr_routines/config.py:61`):

```python
AGENT_MODEL_FLAGS = {"claude": "--model", "opencode": "-m"}
```

`src/herdr_routines/herdr.py:620` `build_agent_start_args` appends `["--", flag, model]` and `src/herdr_routines/config.py:636` rejects `model` for any other kind (`ConfigError` with supported list). `src/herdr_routines/cli.py:583` `validate` surfaces that error as a blocking `error:` (exit 1). The wiring works, but:

1. Only two of ~22 `VALID_AGENT_KINDS` (`config.py:30`) support `model`. Issue 018 acceptance requires at least one more kind honored, with its native flag documented alongside the existing entries.
2. A *value* typo (`model: "claude-sonent-4"`) passes both `config.py:633` (non-empty string check) and `validate` and only fails at `herdr agent start -- <flag> <model>` runtime, after pane/worktree creation and `agent_start` timeout. The issue asks for a catalog / existence check that fails validation instead.

Current runtime guard in `herdr.py:648` (`ValueError` if `AGENT_MODEL_FLAGS.get(kind)` is None) is unreachable for config-loaded jobs but is the only defense for programmatic `build_agent_start_args` callers.

## Approach

Minimal, flag-first extension. No catalog service; no Herdr API change.

### 1. Extend `AGENT_MODEL_FLAGS` (`src/herdr_routines/config.py:61`)

Add at least one additional kind with a pinned-down native flag, verified empirically before commit (not guessed). Candidates in priority order:

- `codex` → `--model` (OpenAI Codex CLI documents `--model <id>`; most requested after claude/opencode; also covers issue 018's "actually wanting either" trigger if demand materializes).
- `gemini` → `--model` (Gemini CLI `gemini --model` is documented).
- `cursor` → `--model` (cursor-agent `--model`).

Verification step (must be done by implementer, recorded in PR description): for each candidate, run the real agent binary's `--help` (or `herdr --help` integration docs if the binary is absent on CI) and confirm the flag spelling; add a comment above the dict citing the source version (e.g. `# codex 0.52.0 --help: --model <model>`), matching the existing comment at `config.py:57` that cites `herdr 0.8.2`.

No change to `VALID_AGENT_KINDS`; only the flag map grows. `build_agent_start_args` (`herdr.py:620`) needs no code change — it already reads the map.

### 2. Keep (and document) validation as the acceptance-2 mechanism

Acceptance bullet 2 is already satisfied by `config.py:636` + `cli.py:583`, but the spec makes it explicit and regression-tested:

- `load_config` / `load_config_dir` → `ConfigError` if `model is not None and agent_kind not in AGENT_MODEL_FLAGS`, message includes `f"(supported: {sorted(AGENT_MODEL_FLAGS)})"` so the error names the allowlist.
- `validate` prints `error: <job>: 'model' is not supported for agent_kind 'X' (supported: [...])` and exits 1 (no soft `warning:`).
- `fallback_model` keeps its existing asymmetry (`config.py:645`): per-job `fallback_model` for unsupported kind is an error; inherited via `defaults.yaml` is inert (`None`). This is intentional (PR #65) and unchanged.

### 3. Optional catalog check — out of scope for v1, but spec reserves the seam

A full model-name allowlist per kind (e.g. `claude: [sonnet, opus, haiku]`, `codex: [gpt-5, o3, ...]`) drifts with provider releases and would need a refresh mechanism. For this issue the flag-existence check already satisfies acceptance 2. If a catalog is desired later, add it as a soft `warning:` in `validate` (not a blocking error) behind an optional `model_catalog: {kind: [names]}` file or regex, so a typo warns without blocking `tick` on a stale catalog. This spec does not require it; the implementer may add a minimal non-empty-string / no-whitespace guard if they want a value-level check without maintaining a catalog.

### 4. Docs and examples

Update `docs/plan-v1.md:238` comment that lists the two flags, and add the new entry to `deploy/jobs.example.yaml` (example job with `agent_kind: codex` + `model:`).

## Files touched

- `docs/pipeline/runs/20260914T050000Z/spec.md` — this file (per-run spec).
- `src/herdr_routines/config.py:61` — add new entry/entries to `AGENT_MODEL_FLAGS` with source comment; no change to `_JOB_ALLOWED_KEYS` / `Job` dataclass / `VALID_AGENT_KINDS`.
- `src/herdr_routines/herdr.py:620` — no logic change; `build_agent_start_args` already consumes the map. Update its docstring if it enumerates supported kinds.
- `src/herdr_routines/cli.py:583` — no logic change; `validate` already surfaces `ConfigError`. Add regression test that `model` for unsupported kind is an `error:` not a `warning:`.
- `tests/test_config.py:285` (`test_model_is_accepted_for_supported_agent_kinds` parametrize) — add new kind(s) to the matrix; add `test_model_rejected_for_unsupported_kind_mentions_supported_list`.
- `tests/test_herdr.py` — add `test_build_agent_start_args_new_kind_includes_flag` asserting `["--", "<flag>", "<model>"]` suffix.

Not touched: `src/herdr_routines/schedule.py`, `src/herdr_routines/history.py`, `src/herdr_routines/tick.py` (pipeline/gated dispatch), `src/herdr_routines/runner.py` beyond existing `RunOutcome` handling, systemd units.

## Risks

- [blocking] Wrong native flag for the new kind breaks `agent start` at runtime (silent until a real run). Mitigated by empirical `--help` verification before commit and a tier-2 `build_agent_start_args` argv assertion; flag is a single string, so the test is exact. — confidence: high
- [blocking] `AGENT_MODEL_FLAGS` drifts when Herdr adds/renames agent CLIs or flags change upstream. Mitigated by pinning the source version in the comment and re-verifying on Herdr upgrade; `validate` error message always prints the current allowlist so misconfiguration is visible. — confidence: high
- [non-blocking] Catalog staleness if a value-level allowlist is added: provider adds a model, catalog rejects it until updated. Mitigated by keeping any catalog check as `warning:` (exit 0) not `error:`, or deferring catalog entirely and relying on the flag-existence check which already satisfies acceptance 2. — confidence: high
- [non-blocking] `fallback_model` inheriting via `defaults.yaml` being inert for unsupported kinds surprises operators who expect it to error. This is intentional and documented at `config.py:654`; spec preserves it. — confidence: high
- [non-blocking] Spec-path hygiene (G-15): must stay at `docs/pipeline/runs/<run_id>/spec.md`; writing to a shared path reintroduces PR #28/#29 merge conflict. — confidence: high
