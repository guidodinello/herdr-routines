from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def tmp_config_path(tmp_path: Path) -> Path:
    return tmp_path / "jobs.yaml"


@pytest.fixture
def tmp_history_path(tmp_path: Path) -> Path:
    return tmp_path / "history.jsonl"


# ---------------------------------------------------------------------------
# Hermetic /tmp diagnosis (issue 038)
# ---------------------------------------------------------------------------

# `execute_run` consults the real filesystem (`df -h /tmp`, issue 027) to decide
# whether to report `tmp_full` instead of the underlying failure reason.  Left
# unstubbed that made six tests pass on CI — where runners have free disk — and
# fail on any host at >=95%.  Shape matches `runner.diagnose_tmp`'s return.
NOT_FULL_DIAGNOSIS: dict[str, Any] = {
    "tmp_full": False,
    "df_tmp": (
        "Filesystem      Size  Used Avail Use% Mounted on\n"
        "tmpfs           2.0G  200M  1.8G   10% /tmp"
    ),
    "du_tmp": "",
}


@pytest.fixture(autouse=True)
def _hermetic_tmp_diagnosis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin `execute_run`'s /tmp diagnosis to a not-full host.

    Autouse on purpose: the failure mode is a *new* test silently inheriting the
    host's disk state, so the default has to be hermetic rather than something each
    test remembers to opt into.  Patches `_best_effort_tmp_diagnosis` — the single
    seam `execute_run` goes through — leaving `diagnose_tmp()`'s own unit tests,
    which call it directly, untouched.
    """
    monkeypatch.setattr(
        "herdr_routines.runner._best_effort_tmp_diagnosis",
        lambda *_args, **_kwargs: dict(NOT_FULL_DIAGNOSIS),
    )
