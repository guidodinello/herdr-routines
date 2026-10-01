"""Pipeline stage workflow for issue 056 (phases B and C). Stdlib-only.

`STAGES` is a single source of truth imported by both `pipeline_run` and
`pipeline_watchdog`, so the two cannot disagree about the layout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class StageSpec:
    """Declarative per-stage specification.

    - `model` is None => no agent at all (stage 4).
    - `prompt_file` is None when no agent is started.
    - `isolation` is the G-17 layout category: `independent`, `none` (model=None),
      or `reused` (stage 6 reuses stage 3).
    - `reuses_stage` is the stage number whose session id is reused, if any.
    """

    stage: int
    model: str | None
    prompt_file: str | None
    timeout_ms: int
    start_timeout_ms: int
    isolation: Literal["independent", "none", "reused"]
    reuses_stage: int | None = None


STAGES: tuple[StageSpec, ...] = (
    StageSpec(
        stage=1,
        model="opencode/muse-spark-1.2-contributor-free",
        prompt_file="stage-1.md",
        timeout_ms=60 * 60 * 1000,
        start_timeout_ms=120_000,
        isolation="independent",
    ),
    StageSpec(
        stage=2,
        model="opencode/muse-spark-1.2-contributor-free",
        prompt_file="stage-2.md",
        timeout_ms=60 * 60 * 1000,
        start_timeout_ms=120_000,
        isolation="independent",
    ),
    StageSpec(
        stage=3,
        model="opencode/x-preview-f-free",
        prompt_file="stage-3.md",
        timeout_ms=90 * 60 * 1000,
        start_timeout_ms=120_000,
        isolation="independent",
    ),
    StageSpec(
        stage=4,
        model=None,
        prompt_file=None,
        timeout_ms=60 * 60 * 1000,
        start_timeout_ms=120_000,
        isolation="none",
    ),
    StageSpec(
        stage=5,
        model="opencode/big-pickle",
        prompt_file="stage-5.md",
        timeout_ms=60 * 60 * 1000,
        start_timeout_ms=120_000,
        isolation="independent",
    ),
    StageSpec(
        stage=6,
        model="opencode/x-preview-f-free",
        prompt_file="stage-6.md",
        timeout_ms=60 * 60 * 1000,
        start_timeout_ms=120_000,
        isolation="reused",
        reuses_stage=3,
    ),
)


def expected_session_layout(*, current_stage: int) -> tuple[StageSpec, ...]:
    """The subset of stage specs relevant to validating session layout."""
    if current_stage < 1:
        return ()
    limit = min(current_stage, len(STAGES))
    return STAGES[:limit]
