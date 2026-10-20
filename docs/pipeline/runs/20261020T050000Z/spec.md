# Spec v1 — Type the remaining `Any` boundaries (issue 065)

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
