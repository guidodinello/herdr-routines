"""The two subprocess runner seams, defined once for the whole package.

Shape A (`SubprocessRunner`) is the `subprocess.run`-shaped seam used by
`host_drift` and `self_update`; Shape B (`BoundedRunner`) is the
`(argv, *, timeout_s) -> (exit_code, stdout, stderr)` bounded-git seam used by
`gates`, `auto_fix` and `pipeline_run`.

The two shapes stay separate because their call surfaces and consumers are
disjoint: Shape A takes `cwd`/`capture_output`/`text`/`check` and returns a
`CompletedProcess[str]`, while Shape B takes a required `timeout_s` and returns
a plain tuple. A single unioned shape would saddle callers that never pass them
with permissive kwargs and force churn in every test fake.

This is a leaf module (standard library only) for an import-graph reason:
`gates` imports `auto_fix`, so the tuple shape cannot live in `gates`, and
`auto_fix` cannot import `gates` without a cycle. It is the one home for both
shapes, and every consumer reaches them through
`from herdr_routines.runner_seam import <Shape> as Runner`.

Not a unification of `herdr.CommandRunner`: that seam declares
`timeout_s: float | None`, whereas Shape B needs `timeout_s: float` required, so
a fake typed `timeout_s: float` satisfies this seam but not `CommandRunner`.
The cousin relationship is deliberate — each stays minimal for its callers.

`argv` is spelled `list[str]` rather than `Sequence[str]`: protocols are
contravariant in their parameters, so a `Sequence[str]` protocol would reject
the many test fakes annotated `list[str]`. The `_run_bounded` implementations
keep their wider `Sequence[str]` parameter — an implementation may accept more
than the protocol requires — so this narrows only what a new fake may declare.

Shape A's default is `_real_runner`, a thin adapter over `subprocess.run`, not
`subprocess.run` itself: the spelled `Literal[True]`/`Literal[False]` kwargs are
exactly what callers pass, but `subprocess.run`'s overloads do not narrow to
them under structural checking. The adapter keeps `subprocess.run` inside this
seam module and the annotation free of `Any`, same precedent as
`herdr._subprocess_runner`.
"""

from __future__ import annotations

import subprocess
from typing import Literal, Protocol


class _Fspath(Protocol):
    """The `os.PathLike` surface `subprocess.run(cwd=...)` accepts, without
    importing `os`/`pathlib` and thus keeping this module a stdlib-leaf."""

    def __fspath__(self) -> str: ...


class SubprocessRunner(Protocol):
    """Shape A: `subprocess.run`-shaped, as every `host_drift`/`self_update` call
    site uses it. The five kwargs are spelled with defaults (rather than hidden
    behind `...` and `Any`) so the parameter list is visible to mypy; call sites
    mirror the literals below (`capture_output=True`, `text=True`,
    `check=False`)."""

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: str | bytes | _Fspath | None = ...,
        capture_output: Literal[True] = ...,
        text: Literal[True] = ...,
        timeout: float | None = ...,
        check: Literal[False] = ...,
    ) -> subprocess.CompletedProcess[str]: ...


def _real_runner(
    argv: list[str],
    *,
    cwd: str | bytes | _Fspath | None = None,
    capture_output: Literal[True] = True,
    text: Literal[True] = True,
    timeout: float | None = None,
    check: Literal[False] = False,
) -> subprocess.CompletedProcess[str]:
    """The real Shape A default: `subprocess.run` with the spelled kwargs, kept
    here so the protocol's parameter list stays visible and `Any`-free."""
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        check=check,
    )


class BoundedRunner(Protocol):
    """Shape B: `(argv, *, timeout_s) -> (exit_code, stdout, stderr)`, the
    bounded-command seam used by `gates`, `auto_fix` and `pipeline_run`. The
    `timeout_s` is a required keyword-only `float` so an implementation never
    has to invent an unbounded default."""

    def __call__(
        self, argv: list[str], *, timeout_s: float
    ) -> tuple[int, str, str]: ...
