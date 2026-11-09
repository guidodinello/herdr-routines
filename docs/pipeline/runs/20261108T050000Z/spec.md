# spec.md v2 — 081: Replace the `Callable[..., X]` runner seams with Protocols

Issue: `docs/process/issues/081-runner-seam-protocol.md` · Run: `20261108T050000Z` · v1→v2 changes are listed in [`## Changelog v1→v2`](#changelog-v1v2).

## Problem

Five modules inject a subprocess runner as a `Callable[..., X]`, and the `...`
hides each parameter list from mypy. These are all 10 hits from
`mypy src --disallow-any-explicit` (re-run on this tree 2026-11-08: `Found 10
errors in 5 files`, at exactly the sites below — the issue's "15" overcounts):

- **Shape A — a `subprocess.run`-shaped callable** returning
  `subprocess.CompletedProcess[str]`, declared at `host_drift.py:44` and
  `self_update.py:136` as
  `Runner = Callable[..., Any]` behind the same (wrong) comment — the
  issue verified mypy *does* accept `subprocess.run` for
  `Callable[..., CompletedProcess[str]]`, and every call site passes
  `text=True`.
- **Shape B — a bounded-command callable**: `(argv, *, timeout_s) ->
  (exit_code, stdout, stderr)`. Declared as inline `Callable[..., tuple[int,
  str, str]]` at `auto_fix.py:1112`, `gates.py:465` and `gates.py:708`, and in
  `pipeline_run.py` as the `Runner = Callable[..., tuple[int, str, str]]`
  alias at :912 plus four inline spell-outs (:868, :1361, :1807, :1866 — v1
  wrongly claimed the last three "already use the alias"; they spell the type
  out).

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
The five modules keep the local name `Runner` by importing, e.g.
`from herdr_routines.runner_seam import BoundedRunner as Runner`
(`host_drift`/`self_update`: `SubprocessRunner as Runner`), so call sites
never change; in `pipeline_run.py` the import also replaces the deleted :912
alias and rewrites all four inline spell-outs to `Runner`.

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

**Test fakes.** Expected impact: fakes untouched; one contract assertion
updated. Shape B fakes are already
`argv: list[str], *, timeout_s: float | None` → satisfied (a wider impl
parameter is fine); Shape A fakes (`FakeValidate`, `FakeSystemctl`) use
`**kwargs: Any → CompletedProcess[str]`, absorbed by the spelled kwargs under
the tests' mypy config (`disallow_any_*` applies only to
`herdr_routines.*`). The one forced change is the issue-065 contract test
`test_run_checks_runner_param_is_callable_contract`
(`tests/test_auto_fix.py:1787`): it pins the exact
`Callable[..., tuple[int, str, str]] | None` spelling of `auto_fix.run_checks`
plus `Callable` imported from `collections.abc`, so it is updated in step to
anchor `Runner | None` — its behavioral body (an injected runner drives the
checks; `runner=None` falls back to `_default_runner`) passes unmodified. Any
fake change beyond that one is reported in the run log per the issue's own
acceptance wording.

## Files touched

| File | Change |
| --- | --- |
| `src/herdr_routines/runner_seam.py` | **new**, ~40 lines: `SubprocessRunner` + `BoundedRunner` Protocols, split rationale, optional `_real_runner` fallback |
| `src/herdr_routines/host_drift.py` | `Runner = Callable[..., Any]` (line 44) → import `Runner` from `runner_seam`; drop dead `Callable`/`Any` imports if now unused |
| `src/herdr_routines/self_update.py` | same at line 136 (`Runner = Callable[..., Any]`); the misleading "overloaded" comment dies with it |
| `src/herdr_routines/auto_fix.py` | line 1112: `Callable[..., tuple[int, str, str]] | None` → `Runner | None`; docstring if needed |
| `src/herdr_routines/gates.py` | lines 465, 708: same swap |
| `src/herdr_routines/pipeline_run.py` | :912 alias deleted, `Runner` imported; the four inline spell-outs (:868, :1361, :1807, :1866) all read `Runner` |
| `tests/` | `test_run_checks_runner_param_is_callable_contract`'s annotation anchor updated to `Runner | None`; fake bodies untouched |
| `vulture_whitelist.py` | only if vulture flags the new Protocols' methods |

`runner_seam.py` must be importable with no `herdr_routines.*` imports
(leaf) — it imports only `subprocess`, `typing`, `collections.abc`.

## Verification

1. Discovery first: `uv run mypy src --disallow-any-explicit` — re-confirm the
   seam inventory (re-verified 2026-11-08: 10 errors in 5 files, at exactly
   the sites listed in the Problem).
2. After the change, same command: **0 hits**.
3. `uv run pytest` green; `scripts/ci-check.sh` green; `uv run mypy --strict
   src` green; `uv run ruff check .` + `ruff format --check .` green.
4. Confirm the two defaults still work with the real `subprocess.run`
   (existing tests already exercise both fakes and defaults).

## Risks

- [blocking] **R1 — `subprocess.run` vs the spelled-kwargs Protocol.** Overload
  resolution against kwargs spelled with `= ...` defaults is the one
  structural step the issue asserts but we will re-verify. If it fails: use
  the `_real_runner` adapter fallback (still no `Any`, no behavior change) —
  do **not** loosen the protocol back to `Callable[..., Any]`. — confidence:
  high (the issue verified `subprocess.run` for
  `Callable[..., CompletedProcess[str]]`; the fallback covers the one unknown,
  and either outcome keeps the annotation free of `Any`)
- [blocking] **R2 — kwarg literal width.** If the protocol kwargs are spelled wider than
  the call sites need (e.g. `capture_output: bool` instead of restricting to
  what's passed), overload resolution may pick the wrong `subprocess.run`
  overload and return `CompletedProcess[bytes]`. Mitigation: mirror the
  literal call sites (`capture_output: Literal[True]`, `text=True`,
  `check=False`), widen only if a real call site needs it. — confidence:
  medium (call sites are enumerable today, but mypy's overload pick is only
  learned by running it)
- [non-blocking] **R3 — hidden constraint on fake ergonomics.** `argv: list[str]` slightly
  narrows what a future fake may declare; documented in the protocol
  docstring so the next fake is written conformant on day one. — confidence:
  high
- [blocking] **R4 — import placement churn.** If review prefers the shapes in existing
  modules (tuple shape forced out of `gates` by its `auto_fix` import), the
  only permitted alternative home is a module both consumers already import —
  the "defined once and imported" acceptance survives a move, not a copy. —
  confidence: high (the acceptance is about one home, not this specific name)
- [non-blocking] **R5 — vulture/ruff false positives** on the new Protocol methods; covered
  by the existing whitelist-file precedent if they appear. — confidence: high

## Acceptance criteria

1. [blocking] One home, leaf module: new `src/herdr_routines/runner_seam.py`
   defines both `SubprocessRunner` and `BoundedRunner` side by side with the
   split-rationale docstring and the `herdr.CommandRunner` cousin note; it
   imports only `subprocess`, `typing` and `collections.abc` (no
   `herdr_routines.*` imports), each shape is defined exactly once in src
   (no consumer spells it out again), and the five consumers reach them
   through `from herdr_routines.runner_seam import ... as Runner`. —
   confidence: high — Test: test_runner_seam_is_a_leaf_module_defining_both_protocols
2. [blocking] Shape A (host_drift): the `Runner = Callable[..., Any]` alias at
   `host_drift.py:44` is gone, `Runner` is imported from `runner_seam`, and
   the default stays real-subprocess (`subprocess.run`, or `_real_runner` per
   R1) with no `Any`; the `subprocess.run`-shaped injected fake still drives
   the check end to end (findings, count/age message, no fetch). — confidence:
   high — Test: test_runner_behind_origin_reports_count_and_age
3. [blocking] Shape A (self_update): the same swap at `self_update.py:136`,
   with the misleading "overloaded" comment gone; the seam's spelled defaults
   still bite — a `validate` that times out is a failure with the same
   verdict and records as before, driven by the injected fake. — confidence:
   high — Test: test_self_update_treats_validate_timeout_as_failure
4. [blocking] Shape B (auto_fix): `run_checks` declares
   `runner: Runner | None = None` with `_default_runner` and the
   `runner = _default_runner` fallback wiring untouched; the issue-065
   contract test is updated in step to the new spelling (annotation pinned as
   `Runner | None`, no local `Callable` annotation) while its behavioral body
   — an injected runner drives the checks, `runner=None` runs the real
   default — passes unmodified. — confidence: high — Test: test_run_checks_runner_param_is_callable_contract
5. [blocking] Shape B (gates): `run_gate1` and `run_stage_gate` read
   `runner: Runner | None` (`gates.py:465`, `:708`), `_run_bounded` stays the
   default unchanged, and gate verdicts with their git command shapes are
   identical — the pipeline fixture's fake `_git_runner` drives the same
   bounded-git seam through the suite (gate 1's spec-commit check among
   them). — confidence: high — Test: test_pipeline_run_does_not_advance_current_stage_before_its_gate
6. [blocking] Shape B (pipeline_run): the :912 alias is deleted, `Runner` is
   imported from `runner_seam`, and all four inline spell-outs (:868, :1361,
   :1807, :1866) read `Runner`; `_run_bounded` itself is untouched, and the
   fake `_git_runner` route (run_pipeline → bookkeeping git calls) behaves
   identically. — confidence: high — Test: test_pipeline_run_records_real_stage_sessions
7. [blocking] No runner seam hides its parameters behind `...` any more:
   `uv run mypy src --disallow-any-explicit` reports 0 hits (the seam
   inventory is 10 sites in 5 files — the issue's "15" overcounts),
   `uv run mypy --strict src` stays green, and a structural pin holds that no
   ellipsis-form `Callable` runner annotation or explicit `Any` survives on a
   `herdr_routines` runner seam. — confidence: high — Test: test_src_runner_seams_hold_no_explicit_any
8. [non-blocking] No lint churn beyond what the new Protocols force: vulture
   stays green with at most a `vulture_whitelist.py` entry for the Protocol
   methods (R5), and the dead-code check stays wired into lint. — confidence:
   medium — Test: test_lint_runs_the_dead_code_check

## Changelog v1→v2

- **v1 (2026-11-08)**: first draft. Decisions: two Protocols in a new leaf
  `runner_seam.py`; argv `list[str]`; shapes kept separate; not unified with
  `herdr.CommandRunner`.
- **v2 (this update, after review)**:
  - Bumped the header to v2 with a pointer here.
  - Added `## Acceptance criteria`: eight numbered items, each tagged
    `[blocking]`/`[non-blocking]` with a `confidence:` tier and ending in an
    exact `Test: <name>`. Test names are contractual: two new tests
    (`test_runner_seam_is_a_leaf_module_defining_both_protocols`,
    `test_src_runner_seams_hold_no_explicit_any`) pin the seam home and the
    no-`Callable[..., ...]`/no-explicit-`Any` end state; the rest anchor the
    per-module ports to existing pins from `tests/test_host_drift.py`,
    `tests/test_self_update.py`, `tests/test_auto_fix.py` and
    `tests/test_pipeline_run.py`.
  - Corrected the hit count: v1 said "exactly the 15 hits"; re-running
    `mypy src --disallow-any-explicit` on this tree (2026-11-08) reports 10
    errors in 5 files at exactly the listed sites. v2 states 10 everywhere.
  - Corrected the `pipeline_run.py` claim: the params at ~1361/~1807/~1866 do
    **not** "already use the alias" — they spell out
    `Callable[..., tuple[int, str, str]]`. The correct inventory is the :912
    alias plus four inline spell-outs (:868, :1361, :1807, :1866), all
    converted to the imported `Runner`.
  - Corrected the test-impact claim: v1's "expected impact: zero" missed the
    issue-065 contract test `test_run_checks_runner_param_is_callable_contract`,
    which pins the exact `Callable[..., tuple[int, str, str]] | None`
    annotation of `auto_fix.run_checks` and would fail on the swap. v2
    commits to updating that test's annotation anchor in step (behavioral
    body unchanged) and fixed the `float | float | None` typo to
    `float | None`.
  - Tagged every Risks bullet `[blocking]`/`[non-blocking]` with a
    `confidence:` tier (v1's Risks had neither) — no risk content changed,
    only tiers added.

## Review

- blocking: 1–7 — the seam home, the two Shape A ports, the three Shape B
  ports, and the no-`Callable[..., ...]`/no-explicit-`Any` end state gate the
  merge.
- non-blocking: 8 — the vulture/whitelist follow-through doesn't gate a type-only
  change.
- Basis: every criterion is a structural or behavioral pin over the change
  v1's Approach already described. Criteria 2, 3, 5 and 6 rest on existing
  tests that must stay green unmodified (fake bodies untouched); criterion 4's
  contract test carries the swap's one forced test change (its annotation
  anchor updated in step, behavioral body identical); criteria 1 and 7 are the
  two new structural pins. Confidence is high throughout because the
  inventory, seams and fakes were re-verified on this tree (2026-11-08); AC8
  is medium because vulture's treatment of Protocol methods is only known
  through the whitelist precedent, not a dedicated pin.
