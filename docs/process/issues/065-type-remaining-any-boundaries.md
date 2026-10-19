---
id: "065"
title: "Type the remaining Any boundaries: telegram_spawn aliases, the auto_fix runner seam, GithubEvent.payload"
status: open
priority: low
area: infra
---

## Description

The 2026-10-19 type-health audit left three `Any` boundaries untyped. Its follow-up runs
(PRs #171-#195) did not cover them:

- `telegram_spawn.py`: `alias_table(spawn: Any)` (~:159) and
  `aliases: Mapping[str, Mapping[str, Any]]` (~:180). The audit's probe showed
  `spawn: TelegramSpawn` (imported under `TYPE_CHECKING`) and
  `aliases: Mapping[str, Mapping[str, str | None]]` type-check. `cli.py` consumes these
  helpers.
- `auto_fix.py`: `runner: Any | None = None` (~:1033), a public test seam. It could be
  `Callable[..., tuple[int, str, str]]`, the same pattern as herdr's `Runner` Protocol.
- `auto_fix.py`: `GithubEvent.payload: dict[str, Any]` (~:1349), the raw gh node, read
  by key and forwarded into the fix prompt. It could be `Mapping[str, object]`, with
  readers narrowing what they read.

`telegram_spawn.py` is also changing in the security PR (#177/#178). Whoever picks this
up must merge main first.

## Acceptance

- No `Any` left in these three signatures. No `cast` and no new `# type: ignore`.
- No behavior change. Test fakes are adapted only where the narrower type forces it.
- `scripts/ci-check.sh` and `uv run mypy --strict src` are green.

## Log

- **2026-10-19**: filed from the type-health audit's INVESTIGATE list. It was found still
  open on main after the type-gaps run.
