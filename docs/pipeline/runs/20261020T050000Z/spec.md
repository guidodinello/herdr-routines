# Spec v2 — Type the remaining `Any` boundaries (issue 065)

Type-only change. No behavior change is permitted; the only non-source edits allowed
are test fakes adapted where a narrower parameter type forces it (issue acceptance).

## Problem

The 2026-10-19 type-health audit left three `Any` boundaries untyped (follow-up runs
PRs #171–#195 did not cover them). They sit on real seams consumed by `cli.py`, the
tick's gate check and the event-prompt builder:

1. `src/herdr_routines/telegram_spawn.py:166` — `alias_table(spawn: Any)`.
2. `src/herdr_routines/telegram_spawn.py:187` —
   `handle_spawn_message(..., aliases: Mapping[str, Mapping[str, Any]])`.
3. `src/herdr_routines/auto_fix.py:1033` — `run_checks(..., runner: Any | None = None)`.
4. `src/herdr_routines/auto_fix.py:1349` — `GithubEvent.payload: dict[str, Any]`.

(The issue counts three boundaries; `alias_table`/`aliases` are one pair in
`telegram_spawn.py`, with `runner` and `payload` in `auto_fix.py`.)

## Approach

### 1. `telegram_spawn.py` — spawn typed, aliases narrowed

- Add `TelegramSpawn` to the existing runtime import from `herdr_routines.config`
  (that import already exists at line ~33; config does not import this module, so no
  cycle and no `TYPE_CHECKING` guard needed). Annotate:
  `def alias_table(spawn: TelegramSpawn) -> dict[str, dict[str, str | None]]:`
- Narrow `handle_spawn_message`'s keyword param to
  `aliases: Mapping[str, Mapping[str, str | None]]` (the audit's probe confirmed this
  type-checks; the audit probe named a `TYPE_CHECKING` import — either placement
  passes, occupy whichever keeps the import list honest).
- Body unchanged: every reader uses `str(spec.get(...) or default)` or receives
  `str | None` (`spec.get("model")` → `model: str | None`), which is what the code
  already assumes.
- `typing.Any` becomes unused here → drop it from the `typing` import (`Protocol`
  stays for `SpawnClient`).
- `cli.py:1072` calls `alias_table(spawn)` with a `config.TelegramSpawn` — already
  compatible; no change to `cli.py`.
- Tests: `tests/test_telegram_spawn.py::_aliases` returns
  `dict[str, dict[str, Any]]` whose values are all `str` (`None`-free); that is
  assignable to the narrower param, so no forced adaptation is expected. Adapt only
  if mypy proves otherwise.

### 2. `auto_fix.py` — runner seam

- `run_checks(runner: Callable[..., tuple[int, str, str]] | None = None)` with
  `Callable` imported from `collections.abc` (module already imports `Iterable`,
  `Mapping`, `Sequence` there). This is the pattern from the issue ("the same as
  herdr's `Runner` Protocol") reduced to its minimal in-repo precedent:
  `gates.py:83` `gate3_result` already declares exactly
  `runner: Callable[..., tuple[int, str, str]] | None = None` and forwards to
  `run_checks`, so that call site type-checks untouched.
- The internal `_default_runner` already has this exact signature; the
  `runner = _default_runner` assignment keeps working. The docstring already
  documents the contract; keep it.
- Tests calling `run_checks` inject nothing today; `gate3_result`'s injected fakes
  already satisfy the narrower type.

### 3. `auto_fix.py` — `GithubEvent.payload`

- Narrow the dataclass field to `payload: Mapping[str, object]`, readers narrowing
  what they read (`Mapping` from `collections.abc`, already imported).
- Verified safe: the field is never mutated (~3 read sites in src, none reading by
   key today — `build_event_fix_prompt` builds from `event.*` fields, and line 1622
  merely forwards `event.payload` unchanged). Construction sites pass already
  `isinstance`-narrowed `dict` objects (`payload=pr` / `payload=node`), which assign
  to `Mapping[str, object]` without a cast. Tests do not construct `GithubEvent`
  directly, so no fixture churn is expected.

## Files touched

- `src/herdr_routines/telegram_spawn.py` — signatures + import + drop `Any`.
- `src/herdr_routines/auto_fix.py` — two signature/field narrows (+1 import).
- `tests/test_telegram_spawn.py`, `tests/test_auto_fix.py`, `tests/test_gates.py` —
  only if the narrower types force an adaptation (none expected).
- No other file. No `cast()`, no new `# type: ignore`, no `scripts/`, no CI files.

## Verification

1. `uv run mypy --strict src` green (acceptance target 1).
2. `scripts/ci-check.sh` green (acceptance target 2).
3. `uv run pytest` green.
4. `git diff` review: signatures and imports only; zero logic edits in src.

## Risks

- **`telegram_spawn.py` is also changing in the security PR (#177/#178).** Whoever
  implements must merge main first (issue precondition) and may need to re-apply the
  typing on the merged form if the security edit touches these same signatures.
  This run's branch is a fresh worktree off main; re-check `git log -- src/herdr_routines/telegram_spawn.py`
  before starting.
- **Narrowing `aliases` changes a public contract** (module `__all__` exports both
  helpers; `cli.py` is the only in-repo consumer and passes `alias_table`'s own
  output). Out-of-repo callers passing looser dicts would break at type-check time —
  acceptable; the allowlist-shaped contract is the point.
- **mypy strict may cascade** from `spawn: TelegramSpawn` into `_build_spawn_alias`/
  config typing. Expected clean (the audit probe already showed it type-checks);
  if not, the fix lives in this change, not a suppression.
- **Behavior-change risk is near zero** (pure annotations), but the acceptance
  forbids behavior drift, so the verification step 4 diff review is mandatory, not
  advisory.
- Low priority / late in the audit's INVESTIGATE list: keep the change exactly the
  three boundaries; resist reformatting or refactoring import style beyond what the
  narrows require.

## Acceptance criteria

Each item states its tier (`blocking` or `non-blocking`) and a `confidence:` tier
(`high` / `medium` / `low`), and ends with its exact verification test name. Items
1–5 are new source-contract tests (they fail before the narrows land and pass
after); items 6–9 anchor existing behavior suites that must stay green unmodified.

1. [blocking, confidence: high] `alias_table` declares
   `def alias_table(spawn: TelegramSpawn)` with `TelegramSpawn` imported from
   `herdr_routines.config` (runtime import, no cycle, no `TYPE_CHECKING` guard) and
   keeps its `dict[str, dict[str, str | None]]` return; body and the `cli.py`
   call site (`aliases=alias_table(spawn)`, ~line 1071) unchanged. Test: test_alias_table_spawn_param_is_telegram_spawn
2. [blocking, confidence: high] `handle_spawn_message` declares
   `aliases: Mapping[str, Mapping[str, str | None]]` and reads aliases exactly as
   before (`str(spec.get(...) or default)` coercion, `spec.get("model")` as
   `str | None`); alias resolution behavior — unknown alias listing, per-alias
   harness, model validation — is unchanged. Test: test_handle_spawn_message_aliases_are_str_or_none_mapping
3. [blocking, confidence: high] `run_checks` declares
   `runner: Callable[..., tuple[int, str, str]] | None = None` with `Callable` from
   `collections.abc`; `_default_runner` keeps that exact signature, the
   `runner = _default_runner` fallback wiring is untouched, and an explicitly
   injected runner (as `gates.py:83` `gate3_result` already injects) still drives
   the checks. Test: test_run_checks_runner_param_is_callable_contract
4. [blocking, confidence: high] `GithubEvent` declares
   `payload: Mapping[str, object]`; the field is never mutated in src, forwarded
   unchanged at `auto_fix.py:1622`, and `build_event_fix_prompt` continues to build
   from `event.*` fields so event-fix prompt output is byte-identical. Test: test_github_event_payload_is_mapping_str_object
5. [blocking, confidence: high] No `Any` remains in the four named signatures;
   `telegram_spawn.py` drops `Any` from its `typing` import (Protocol retained for
   `SpawnClient`); the change introduces no `cast(` and no new `# type: ignore`
   anywhere in src. Test: test_typed_boundaries_have_no_any
6. [non-blocking, confidence: high] Duplicated-alias validation still rejects bad
   alias maps with `ConfigError` on the narrowed type. Test: test_spawn_alias_map_validation
7. [non-blocking, confidence: high] Alias model resolution still validates and
   reaches the handler through `handle_spawn_message` only for the alias's own
   harness on the narrowed type. Test: test_spawn_alias_model_is_validated_and_reaches_the_handler
8. [non-blocking, confidence: medium] Unknown spawn targets still print the alias
   list built by `alias_table` on the narrowed type. Test: test_spawn_unknown_target_lists_aliases
9. [non-blocking, confidence: medium] `run_checks` partial-output-on-timeout and
   specific-error reporting behavior is unchanged through the narrowed seam.
   Test: test_run_checks_keeps_partial_output_on_timeout

## Changelog v1→v2

- Title `Spec v1` → `Spec v2`; v1 content kept verbatim (no design change — same
  three boundaries, same narrows, same no-behavior-change rule).
- Added `## Acceptance criteria`: 9 numbered items, each carrying a
  `blocking`/`non-blocking` tier plus a `confidence:` tier (`high`/`medium`/`low`)
  and ending `Test: <exact test name>` for the `rg -c "Test:"` extraction and
  `rg -F` existence check (gate 2/3). Items 1–5 name new source-contract tests to
  be written in stage 3 (`tests/test_telegram_spawn.py`,
  `tests/test_auto_fix.py`) that grep the exact narrowed signatures — they fail
  before the change exists and pass after; items 6–9 anchor existing promotion
  tests already in `tests/` (`test_telegram_spawn.py:305/:329/:752`,
  `test_auto_fix.py:1382`) as regression guards. `mypy --strict src` and
  `scripts/ci-check.sh` greenness stays in Verification as commands for the
  implementer to run, not as `Test:` items — a pytest criterion cannot nest the
  CI script (it runs the full pytest suite recursively).
- Review corrections found while verifying v1's claims against source (all four
  boundary signatures, `gates.py:83`, `cli.py` call site, `_aliases` fixture
  None-free dict, `collections.abc` imports confirmed accurate): v1's risk item
  "telegram_spawn.py is also changing in the security PR (#177/#178)" is stale —
  the security edit landed as PR #202 (`ec74180 fix: scope claim scans to own PRs
  and Telegram spawn to a private chat`) and is already in this worktree's base;
  line numbers 166/187 still match, so the merge-main precondition is satisfied.
  Risk text kept verbatim for the audit trail; whoever implements should read
  "#177/#178" as "already merged main (PR #202)".
