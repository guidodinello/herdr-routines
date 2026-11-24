"""A controllable clock for tests of tick-based retries (issue 088).

A retry is due a number of ticks after the failure that queued it, and the failure is
stamped with the clock *at dispatch* (`tick._dispatch_pending` reads `datetime.now`), not
with the `now` handed to `run_tick`. Without a frozen clock a test would race the wall
clock. `FrozenClock.tick` sets both to the same instant.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import tick
from herdr_routines.config import RoutinesConfig
from herdr_routines.tick import TickOutcome, run_tick


class FrozenClock:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, start: datetime) -> None:
        self.value = start
        outer = self

        class _Datetime(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
                return outer.value

        monkeypatch.setattr(tick, "datetime", _Datetime)

    def tick(
        self, config: RoutinesConfig, history: Path, client: Any, at: datetime
    ) -> TickOutcome:
        self.value = at
        return run_tick(config, history, client=client, now=at)
