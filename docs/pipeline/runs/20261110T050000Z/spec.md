# Spec v1 — issue 083: type telegram spawn aliases as a record

Run: 20261110T050000Z · issue: `docs/process/issues/083-telegram-alias-typed-record.md` (priority: low, infra)

## Problem

`telegram_spawn.alias_table` (telegram_spawn.py:173) returns
`dict[str, dict[str, str | None]]`, de-typing validated values. The one consumer,
`handle_spawn_message` (~:240), rebuilds mode with
`mode = str(spec.get("workspace") or "worktree")` — re-stringifying a value config
already validated as a closed `WorkspaceMode` and duplicating the `"worktree"` default
in a second file. The same stale coercion applies to `repo`, `agent_kind` and `base`
(`str(spec.get(...) or default)`), so three more defaults ("", "claude", "main") are
mirrored outside config.

Key discovery: the "named record" the issue asks for already exists —
`config.SpawnAlias` (config.py:386) is a frozen dataclass with
`repo: Path`, `agent_kind: str`, `workspace: WorkspaceMode`, `base: str`,
`model: str | None` and every default set at construction. The de-typing is purely
the round-trip through `alias_table`'s dict shape.

## Approach

Propagate `SpawnAlias` itself, instead of inventing a parallel record type that would
duplicate its shape field-for-field:

1. **`alias_table(spawn: TelegramSpawn) -> dict[str, SpawnAlias]`** —
   `{a.alias: a for a in spawn.aliases}`; values passed through unmodified, so anywhere
   `SpawnAlias` lives the `WorkspaceMode` Literal travels with it (ROADMAP.md:227 aim
   satisfied).
2. **`handle_spawn_message`** — parameter becomes
   `aliases: Mapping[str, SpawnAlias]`; reads become direct attribute access
   (`spec.agent_kind`, `spec.workspace`, `spec.base`, `spec.model`, `str(spec.repo)`).
   The `str(spec.get(...) or "worktree" / "claude" / "main")` coercions and their
   inline defaults are deleted; the `"worktree"` default now lives in exactly one
   place — `config.SpawnAlias`'s field default (config.py:399).
3. **`cli.py` call site (cli.py:1284) stays `aliases=alias_table(spawn)`** — unchanged
   text, now passing typed values. No change needed in `config.py` (and none is made:
   `SpawnAlias` already validates against `VALID_WORKSPACE_MODES` via
   `_build_spawn_alias` — the present issue's "spans config" resolves to zero edits).

Alternatives rejected: (a) a new frozen dataclass in `telegram_spawn.py` — duplicates
`SpawnAlias` 1:1 only to keep the handler "free of any config import"; but the handler
already imports five runtime names from `herdr_routines.config` (telegram_spawn.py:33),
so that layering intent is already historical, not real. (b) A `TypedDict` — cannot
enforce the mode Literal on assignment, so it re-admits the problem.

Behavior is unchanged: `SpawnAlias.workspace` is non-Optional and already defaulted, so
for every config-validated spawn the `mode` string is identical; the dispatch structure
(`worktree` → `worktree_create_full`, else → `tab_create`) is kept verbatim.

## Files touched

- `src/herdr_routines/telegram_spawn.py` — `alias_table` return type and body;
  `handle_spawn_message` parameter type + the four `spec.get` reads + inner-docstring
  updates (including the stale "handler stays free of any config import" line in the
  `alias_table` docstring, now false by design).
- `tests/test_telegram_spawn.py` — update `_aliases` helper (:52) and any literal-dict
  alias fixtures to construct `SpawnAlias(...)` records; **rewrite the two issue-065
  source-contract tests** `test_alias_table_spawn_param_is_telegram_spawn` (:866) and
  `test_handle_spawn_message_aliases_are_str_or_none_mapping` (:908), which pin the
  exact old signatures and the `str(spec.get(...))` source strings via
  `get_type_hints`/`inspect.getsource`. Updated to assert the new types
  (`dict[str, SpawnAlias]`, `Mapping[str, SpawnAlias]`) the same way. Also update
  `test_alias_table_*` assertions like `alias_table(cfg.telegram_spawn)["nightly"]["base"]`
  to attribute access.
- `src/herdr_routines/config.py` — **no change** (record and defaults already correct).
- `ROADMAP.md` — no change (entry :227 stays; link target gains no new file).

## Issues-065 acceptance interaction

The strongest constraint: issue 065's tests are *blocking source contracts* locking
`dict[str, dict[str, str | None]]`. 083 supersedes those two specific assertions — the
narrowing direction (no `Any`, no `cast(`, no `# type: ignore`) is preserved and still
enforced by `test_typed_boundaries_have_no_any` (:931), which keeps passing untouched.
Everything else from 065 (auto_fix/run_checks/GithubEvent) is untouched.

## Risks

1. **Missed contract tests**: other tests may embed the old dict shape anywhere
   (`test_none_sentinels.py:172` lists a "workspace" dict in an unrelated context —
   checked, not affected; full-suite run will confirm). Mitigation: grep + `uv run pytest`.
2. **mypy --strict**: `str(spec.repo)` conversion of a `Path` at the protocol boundary —
   `SpawnClient.worktree_create_full`/`tab_create` take `cwd: str`; explicit `str()`
   keeps it clean. `get_type_hints` on `Mapping[str, SpawnAlias]` resolves
   at runtime since the import is already runtime-visible.
3. **Behavior drift via default re-flattening**: the old `or`-coercions also flattened
   *empty strings* to defaults. Config rejects `repo: ''`, empty `agent_kind`, empty
   `base` and empty `allowed_chat_id` at load (fail-closed, per SpawnAlias/TelSpawn
   docstrings), so no reachable config produces the flattened inputs — hence no
   behavior change. `model: null` / omitted is the one None-carrying field and both
   old and new paths yield None.
4. **Silent widening of the else-branch**: today *any* non-`"worktree"` mode string
   takes the tab path; after typing only `"root"` can reach it. For validated config
   these sets coincide; the `else` stays as the structural fallback (no
   `/workspace == "root"` exhaustive-match rewiring), documented in place.

## Acceptance / verification

- Alias table typed by a named record (`SpawnAlias`); `workspace` is `WorkspaceMode` —
  asserted by the rewritten contract tests.
- `"worktree"` default in exactly one place (`config.SpawnAlias`) — asserted by
  absence-check on `str(spec.get("workspace") or "worktree")` in the rewritten test.
- No behavior change: every existing spawn-flow test passes on the same fake
  (`FakeSpawnClient.workspace_calls` / `agent_calls` assertions unchanged).
- `scripts/ci-check.sh` green; `uv run mypy --strict src` green.
