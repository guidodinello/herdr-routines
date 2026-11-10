from __future__ import annotations

import functools
import os
from datetime import datetime
from pathlib import Path
from typing import Any, cast

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


@pytest.fixture(autouse=True)
def _hermetic_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the default state dir (history, reports) at the test's tmp dir.

    Since issue 074 `herdr-routines run` writes history like a tick does, so a test that
    drives `cli.main(["run", ...])` without pinning the state dir would append to the
    developer's real `~/.local/state/herdr-routines/history.jsonl`. Tests that need a
    specific location still set the variable themselves."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def _hermetic_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the host's global and system git config from every test.

    Same reasoning as `_hermetic_tmp_diagnosis`: a developer `~/.gitconfig` with
    `commit.gpgsign=true` made every fixture commit sign with the real key, and a
    test that narrows `PATH` to just `git` then failed with "cannot run gpg" — on
    the laptop only, since CI runners carry no global config.  Tests that need a
    signing setup configure it on their own repo (see `test_signing.py`).
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def _hermetic_host_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the host drift check the CLI runs for `digest` and `self-update`.

    Same reasoning as `_hermetic_tmp_diagnosis`: unstubbed, every CLI test would run
    real `systemctl --user` and git against `~/projects/herdr-routines` — failing on
    CI, reading live state on a laptop.  Tests that exercise the check override this
    with their own `cli.check_host` (see `tests/test_host_drift.py`).
    """
    monkeypatch.setattr("herdr_routines.cli.check_host", lambda *_a, **_kw: [])


@pytest.fixture(autouse=True)
def _dispatch_clock_follows_tick_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make tick's dispatch-time clock deterministic, without pinning it to tick start.

    Since issue 079 `_dispatch_pending` re-reads the clock at dispatch time. Tests drive
    `run_tick(now=...)` with a simulated clock, so this fixture makes the re-read return
    that same simulated value (or the real clock when a test supplies none). It does not
    change production: the module only reads the clock through `datetime.now`.
    """
    from herdr_routines import tick

    current: dict[str, datetime | None] = {"now": None}
    real_datetime = tick.datetime

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            value = current["now"]
            if value is None:
                return cast(datetime, real_datetime.now(tz))
            return value if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(tick, "datetime", _Clock)

    def _follow(fn: Any) -> Any:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            current["now"] = kwargs.get("now")
            return fn(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(tick, "_process_job", _follow(tick._process_job))
    monkeypatch.setattr(tick, "dispatch_occurrence", _follow(tick.dispatch_occurrence))
    monkeypatch.setattr(
        tick, "dispatch_manual_occurrence", _follow(tick.dispatch_manual_occurrence)
    )
