---
id: "081"
title: "Replace the Callable[..., X] runner seams with a Runner Protocol"
status: open
priority: low
area: infra
---

## Description

Five modules inject a subprocess runner as `Callable[..., X]`:

- `host_drift.py` (~:44) and `self_update.py` (~:136): `Runner = Callable[..., Any]`.
  The comment justifying `Any` is wrong. mypy accepts `subprocess.run` for
  `Callable[..., CompletedProcess[str]]`, and every call passes `text=True`.
- `auto_fix.py` (~:1109), `gates.py` (~:461, ~:698), and `pipeline_run.py` (~:867,
  ~:912, ~:1354, ~:1799, ~:1856): `Callable[..., tuple[int, str, str]]`.
  `pipeline_run.py:867` spells the type out instead of using the module's own
  `Runner` alias.

The `...` hides each parameter list from the checker. These are all 15 hits from
`mypy --disallow-any-explicit` on `src`.

A `Protocol` with an explicit `__call__` would close this. Two design choices are
open. First, the argv type: `_run_bounded` takes `Sequence[str]`, while the auto_fix
default and the test fakes take `list[str]`. Second, whether the two `Runner` shapes
(the `CompletedProcess` one and the `tuple` one) stay separate. About 109
`runner=` sites in tests have to satisfy the result.

## Acceptance

- No `Callable[..., ...]` runner types left in `src`. `mypy src --disallow-any-explicit`
  reports no runner-seam hits.
- Each runner shape is defined once and imported where it is used.
- No behavior change. Test fakes change only where the narrower type forces it.
- `scripts/ci-check.sh` and `uv run mypy --strict src` are green.

## Log

- **2026-10-26**: filed from the type-health audit's INVESTIGATE list.
