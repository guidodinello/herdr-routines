# spec.md v1 — 081: Replace the `Callable[..., X]` runner seams with Protocols

Issue: `docs/process/issues/081-runner-seam-protocol.md` · Run: `20261108T050000Z`

## Problem

Five modules inject a subprocess runner as a `Callable[..., X]`, and the `...`
hides each parameter list from mypy. These are exactly the 15 hits from
`mypy src --disallow-any-explicit`:

- **Shape A — a `subprocess.run`-shaped callable** returning
  `CompletedProcess[str]`: `host_drift.py:44` and `self_update.py:136`, both
  declared `Runner = Callable[..., Any]` behind the same (wrong) comment — the
  issue verified mypy *does* accept `subprocess.run` for
  `Callable[..., CompletedProcess[str]]`, and every call site passes
  `text=True`.
- **Shape B — a bounded-command callable**: `(argv, *, timeout_s) ->
  (exit_code, stdout, stderr)`. Declared as inline `Callable[..., tuple[int,
  str, str]]` at `auto_fix.py:1112`, `gates.py:465` and `gates.py:708`, and as
  `Runner = Callable[..., tuple[int, str, str]]` in `pipeline_run.py:912`
  (single inline spell-out at `pipeline_run.py:867`; params at ~1361, ~1807,
  ~1866 already use the alias).

Two open design questions the issue names must be answered:

1. **argv type** — `gates._run_bounded` and `pipeline_run._run_bounded` take
   `Sequence[str]`; the auto_fix default runner and every test fake take
   `list[str]`.
2. **one unioned shape or two** — the `CompletedProcess` seam and the tuple
   seam have disjoint call surfaces and disjoint consumers.

## Approach

**Two Protocols, each defined once, in one new leaf module.** Create
`src/herdr_routines/runner_seam.py` holding both, side by side, with a
docstring that states the split rationale. A leaf module is forced by the
import graph: `gates.py` already imports `auto_fix.py`, so the tuple shape
cannot live in `gates`; `auto_fix` cannot import `gates` without a cycle. A
dedicated module also keeps the two shapes documented side by side and gives
them one home, matching the precedents already in the repo
(`herdr.CommandRunner`, `herdr.Notify`, `self_update.Notify`) — with the noted
exception that we do **not** try to unify with `herdr.CommandRunner` (see
decision 3).

```python
class SubprocessRunner(Protocol):
    """Shape A: `subprocess.run`-shaped. The defaults spell the kwargs every
    src call site passes (cwd, capture_output=True, text=True, timeout,
    check=False) so the parameter list is no longer hidden behind `...`."""

    def __call__(
        self, argv: list[str], **kw: <spelled: cwd / capture_output / text /
        timeout / check, each with a `= ...` default>
    ) -> subprocess.CompletedProcess[str]: ...


class BoundedRunner(Protocol):
    """Shape B: `(argv, *, timeout_s) -> (exit_code, stdout, stderr)`, the
    bounded-git-command seam used by gates/auto_fix/pipeline_run."""

    def __call__(
        self, argv: list[str], *, timeout_s: float
    ) -> tuple[int, str, str]: ...
```

Names (`SubprocessRunner` / `BoundedRunner`) are implementation detail; what
matters is *each shape appears exactly once and everywhere else imports it*.
The seam modules keep the local name `Runner` by importing, e.g.
`from herdr_routines.runner_seam import BoundedRunner as Runner`, so every
existing annotation (and most of `pipeline_run.py`, which already says
`Runner`) is untouched.

**Decision 1 — argv type: `list[str]`.** Structural assignability is
contravariant in parameters, so a Protocol with `Sequence[str]` would *reject*
every test fake annotated `list[str]`, and those fakes are the ~109 `runner=`
sites. `list[str]` matches all fakes, all src call sites (literals), and the
established `herdr.CommandRunner` precedent. The two `_run_bounded` functions
keep their wider `Sequence[str]` *parameter* — an implementation may be
narrower than the protocol requires, so no change there, and no behavior
change.

**Decision 2 — the two shapes stay separate.** They differ in return type
(`CompletedProcess[str]` vs a tuple), in call surface (`cwd`/`capture_output`/
`text`/`check` vs a named `timeout_s`), and have disjoint consumers
(host_drift/self_update vs gates/auto_fix/pipeline_run). Unifying would put
`float | None`-style permissive kwargs on callers that never pass them and
force churn in test fakes. Two Protocols, one home.

**Decision 3 — don't unify with `herdr.CommandRunner`.** `CommandRunner`
declares `timeout_s: float | None`; Shape B needs `float` required. A fake
typed `timeout_s: float` (e.g. `tests/test_auto_fix.py:1832`) is satisfiable
against our `float` protocol but **not** against `CommandRunner`'s `float |
None`, so reusing it would widen test fakes for zero benefit. Note the
cousin-relationship in the module docstring instead.

**Shape A defaults.** `host_drift` / `self_update` keep
`runner: Runner = subprocess.run` as the default iff mypy accepts
`subprocess.run` against the spelled-kwargs Protocol (the issue says it
does). If overload resolution rejects it (the one genuinely unknown
structural bit), the fallback is a small module-level `_real_runner` wrapper
in `runner_seam.py` — exact precedent: `herdr._subprocess_runner` — and the
defaults switch to `= _real_runner`, still hiding `subprocess.run` inside the
seam module. Either way **no behavior change**: the protocol is
annotation-only.

**Test fakes.** Expected impact: zero. Shape B fakes are already
`argv: list[str], *, timeout_s: float | float | None` → satisfied (a wider
impl parameter is fine); Shape A fakes (`FakeValidate`, `FakeSystemctl`) use
`**kwargs: Any → CompletedProcess[str]`, absorbed by the spelled kwargs under
the tests' mypy config (`disallow_any_*` applies only to
`herdr_routines.*`). Any forced fake change beyond zero is reported in the
run log per the issue's own acceptance wording.

## Files touched

| File | Change |
| --- | --- |
| `src/herdr_routines/runner_seam.py` | **new**, ~40 lines: `SubprocessRunner` + `BoundedRunner` Protocols, split rationale, optional `_real_runner` fallback |
| `src/herdr_routines/host_drift.py` | `Runner = Callable[..., Any]` (line 44) → import `Runner` from `runner_seam`; drop dead `Callable`/`Any` imports if now unused |
| `src/herdr_routines/self_update.py` | same at line 136 (`Runner = Callable[..., Any]`); the misleading "overloaded" comment dies with it |
| `src/herdr_routines/auto_fix.py` | line 1112: `Callable[..., tuple[int, str, str]] | None` → `Runner | None`; docstring if needed |
| `src/herdr_routines/gates.py` | lines 465, 708: same swap |
| `src/herdr_routines/pipeline_run.py` | line 912 alias deleted, `Runner` imported; line 867 inline spell-out uses `Runner` |
| `tests/` | only if mypy forces it (expected: none) |
| `vulture_whitelist.py` | only if vulture flags the new Protocols' methods |

`runner_seam.py` must be importable with no `herdr_routines.*` imports
(leaf) — it imports only `subprocess`, `typing`, `collections.abc`.

## Verification

1. Discovery first: `uv run mypy src --disallow-any-explicit` — confirm the
   issue's claim that all hits are these 15 runner seams.
2. After the change, same command: **0 hits**.
3. `uv run pytest` green; `scripts/ci-check.sh` green; `uv run mypy --strict
   src` green; `uv run ruff check .` + `ruff format --check .` green.
4. Confirm the two defaults still work with the real `subprocess.run`
   (existing tests already exercise both fakes and defaults).

## Risks

- **R1 — `subprocess.run` vs the spelled-kwargs Protocol.** Overload
  resolution against kwargs spelled with `= ...` defaults is the one
  structural step the issue asserts but we will re-verify. If it fails: use
  the `_real_runner` adapter fallback (still no `Any`, no behavior change) —
  do **not** loosen the protocol back to `Callable[..., Any]`.
- **R2 — kwarg literal width.** If the protocol kwargs are spelled wider than
  the call sites need (e.g. `capture_output: bool` instead of restricting to
  what's passed), overload resolution may pick the wrong `subprocess.run`
  overload and return `CompletedProcess[bytes]`. Mitigation: mirror the
  literal call sites (`capture_output: Literal[True]`, `text=True`,
  `check=False`), widen only if a real call site needs it.
- **R3 — hidden constraint on fake ergonomics.** `argv: list[str]` slightly
  narrows what a future fake may declare; documented in the protocol
  docstring so the next fake is written conformant on day one.
- **R4 — import placement churn.** If review prefers the shapes in existing
  modules (tuple shape forced out of `gates` by its `auto_fix` import), the
  only permitted alternative home is a module both consumers already import —
  the "defined once and imported" acceptance survives a move, not a copy.
- **R5 — vulture/ruff false positives** on the new Protocol methods; covered
  by the existing whitelist-file precedent if they appear.

## Changelog

- **v1 (this file, 2026-11-08)**: first draft. Decisions: two Protocols in a
  new leaf `runner_seam.py`; argv `list[str]`; shapes kept separate; not
  unified with `herdr.CommandRunner`.
