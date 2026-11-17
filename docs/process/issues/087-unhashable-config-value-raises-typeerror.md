---
id: "087"
title: "A list or mapping in an enum-like config field raises TypeError, not ConfigError"
status: done
priority: medium
area: config
---

## Description

Several config fields are checked with a bare `value not in VALID_...` against a
frozenset. When the YAML value is a list or a mapping, the membership test itself raises
`TypeError: unhashable type`, before the `ConfigError` line is reached. The CLI catches
only `ConfigError`, so `validate` and `tick` end in a traceback that names no job and no
key.

Reproduce: a job file with `workspace: []` (or `kind: {}`, `on_missed: []`), then
`load_config`:

```text
    if declared_workspace not in VALID_WORKSPACE_MODES:
TypeError: unhashable type: 'list'
```

Unguarded checks in `config.py` (found by grep for `not in VALID_` / `not in ACCEPTED_`):

- `_build_job`: `kind`, `workspace`, `agent_kind`, `on_missed`, `notify_policy`.
- `_build_spawn_alias`: `agent_kind`, `workspace`.
- `_build_github_event_check`: `query`.

The other membership checks (`select`, `target`, `retry_on`, `verify`, `pick`) already
test `isinstance(value, str)` first and are not affected.

Found on 2026-11-16 by the differential fuzz run for issue 060. The old `_build_job`
behaves the same way, so this predates that refactor.

## Acceptance

- Each field listed above raises `ConfigError` with its usual message
  (`"<label>: '<key>' must be one of [...]"`) for a list or mapping value.
- One helper does the check for all of them, so a new enum-like field cannot repeat
  the mistake.
- A test covers a list and a mapping value for every field listed above.
- Messages for string values are unchanged.
- `scripts/ci-check.sh` is green.

## Log

- **2026-11-16**: filed from the issue 060 fuzz run.
- **2026-11-16**: fixed. `config._is_choice` does the membership test for every
  enum-like field: the eight listed above, plus `select`, `target`, `verify` and `pick`,
  which already checked `isinstance(value, str)` by hand. Messages are unchanged.
  The fuzz run turned up two more crashes of the same kind, fixed here too:
  a `select` entry that is a list or mapping (the duplicate check built a set), and a
  malformed `timezone` key (`""`, an absolute path, one with `..`), for which
  `ZoneInfo` raises `ValueError` rather than `ZoneInfoNotFoundError`. On 200,000
  mutated job configs the old code crashed on 21,176; the new code raises `ConfigError`
  on all of those and gives the same result on the rest.
