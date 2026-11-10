# Spec v2 — issue 083: type telegram spawn aliases as a record

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

## Acceptance criteria

Classification: each item is **blocking** (run must not ship without it) or
**non-blocking** (valuable, ship with a follow-up note if it slips). Confidence:
`high` (deterministic, fully testable in-repo), `medium` (testable but depends
on live external state shape), `low` (can only be exercised against the real
job). The two issue-065 source-contract tests appear under their v1 names —
v1's Files touched rewrites them in place; only their asserted types change.

1. **blocking / confidence: high** — `alias_table` propagates config's record
   typed: return type is `dict[str, SpawnAlias]`, body is the pass-through
   `{a.alias: a for a in spawn.aliases}` (values unmodified, so the
   `WorkspaceMode` Literal travels with every entry); `SpawnAlias` is imported
   at runtime from `herdr_routines.config` (no `TYPE_CHECKING` guard, no
   cycle). Test: `test_alias_table_spawn_param_is_telegram_spawn`
2. **blocking / confidence: high** — `handle_spawn_message` declares
   `aliases: Mapping[str, SpawnAlias]` and reads via direct attribute access on
   the record (`spec.agent_kind`, `spec.workspace`, `spec.base`, `spec.model`,
   `str(spec.repo)`); the rewritten source-contract pin asserts the new hints
   and the new read strings the same way v1's contract test did for the old
   ones. Test: `test_handle_spawn_message_aliases_are_str_or_none_mapping`
3. **blocking / confidence: high** — the four stale coercions are gone: no
   `str(spec.get("workspace") or "worktree")`, `str(spec.get("agent_kind") or
   "claude")`, `str(spec.get("base") or "main")` or `str(spec.get("repo") or
   "")` remains in `telegram_spawn.py`; the `"worktree"` default lives in
   exactly one place — `config.SpawnAlias`'s field default (config.py:399) —
   asserted by the rewritten test's absence-checks on those source strings.
   Test: `test_handle_spawn_message_aliases_are_str_or_none_mapping`
4. **blocking / confidence: high** — issue 065's narrowing direction is
   preserved with zero new erasure: no `Any`, no `cast(` and no
   `# type: ignore` in the changed path; 065's guard test passes untouched
   (exactly the test 083 must not weaken). Test:
   `test_typed_boundaries_have_no_any`
5. **blocking / confidence: high** — dispatch behavior is unchanged and the
   else-branch stays as the structural fallback without exhaustive-match
   rewiring: a `worktree`-mode alias reaches `worktree_create_full` and a
   `root`-mode alias reaches `tab_create` on the same `FakeSpawnClient`
   assertions as before. Test:
   `test_spawn_root_mode_opens_a_tab_in_the_checkout`
6. **non-blocking / confidence: high** — the one None-carrying field round-trips:
   `SpawnAlias.model` (`str | None`) reaches the handler as `None` when omitted
   and as the configured string per alias otherwise, through the new attribute
   reads. Test: `test_spawn_alias_model_is_validated_and_reaches_the_handler`
7. **non-blocking / confidence: high** — alias-map end-user behavior is
   unchanged: an unknown target still lists the configured aliases in the
   reply, exercising `alias_table`'s new record values through the user-facing
   path. Test: `test_spawn_unknown_target_lists_aliases`
8. **non-blocking / confidence: medium** — the fixtures follow the record: the
   `_aliases` helper (tests/test_telegram_spawn.py:52) and all literal-dict
   alias fixtures construct `SpawnAlias(...)` records, and the
   repo-as-`Path` → `str(spec.repo)` conversion keeps
   `SpawnClient.worktree_create_full`/`tab_create`'s `cwd: str` clean under
   strict typing. Test: `test_alias_table_spawn_param_is_telegram_spawn`
9. **non-blocking / confidence: medium** — meta-check: `scripts/ci-check.sh`
   green, `uv run mypy --strict src` green, and the blocking criteria verified
   red before implementation (TDD: contract tests rewritten first, confirmed
   failing, then implementation). Test:
   `test_ci_green_with_blocking_tests_run_first`

## Changelog v1→v2

- Title bumped to Spec v2.
- Added `## Acceptance criteria`: 9 numbered items, each tagged
  **blocking**/non-blocking with a `confidence:` tier and ending
  `Test: <name>` (exact test names). The two blocking 065 source-contract
  tests (`:1`, `:2`) and the no-erasure guard (`:4`) were already implicitly
  named in v1's Files touched / Issues-065 sections and are now first-class
  criteria; v1's "Acceptance / verification" prose claims are converted into
  items 5–9 with concrete test names.
- Renamed v1's `## Acceptance / verification` heading to `## Verification`
  (content unchanged) to disambiguate from the new `## Acceptance criteria`
  section.
- No change to Problem, Approach, Files touched, Issues-065 interaction, or
  Risks content — v2 only adds observability of acceptance, per stage-2 scope.

## Verification

- Alias table typed by a named record (`SpawnAlias`); `workspace` is `WorkspaceMode` —
  asserted by the rewritten contract tests.
- `"worktree"` default in exactly one place (`config.SpawnAlias`) — asserted by
  absence-check on `str(spec.get("workspace") or "worktree")` in the rewritten test.
- No behavior change: every existing spawn-flow test passes on the same fake
  (`FakeSpawnClient.workspace_calls` / `agent_calls` assertions unchanged).
- `scripts/ci-check.sh` green; `uv run mypy --strict src` green.
