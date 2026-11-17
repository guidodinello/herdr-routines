"""The modules that make up a tick's dispatch, and a patch that reaches all of them.

`tick.py` was split into one module per dispatch flow (issue 060). Each module binds its
own copy of a shared collaborator (`RealGhClient`, `ensure_repo`, `subprocess`, ...), so
replacing one for a whole tick means replacing it in every module that binds it.
"""

from __future__ import annotations

from types import ModuleType

import pytest

from herdr_routines import (
    audit_flow,
    dispatch_common,
    gated_base_flow,
    gated_event_flow,
    gated_pr_flow,
    pipeline_flow,
    routine_flow,
    routine_targets_flow,
    tick,
)

DISPATCH_MODULES: tuple[ModuleType, ...] = (
    tick,
    dispatch_common,
    audit_flow,
    gated_pr_flow,
    gated_event_flow,
    gated_base_flow,
    routine_targets_flow,
    routine_flow,
    pipeline_flow,
)


def patch_dispatch(monkeypatch: pytest.MonkeyPatch, name: str, value: object) -> None:
    """Replace `name` in every dispatch module that binds it.

    Raises if no module binds it, so a misspelt or since-removed name fails the test
    instead of patching nothing.
    """
    bound = [module for module in DISPATCH_MODULES if hasattr(module, name)]
    if not bound:
        raise AttributeError(f"no dispatch module binds {name!r}")
    for module in bound:
        monkeypatch.setattr(module, name, value)
