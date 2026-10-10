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
    - `fallback_model` is what `pipeline_run` resumes the stage's session on, once, when
      `model`'s provider rejects a call mid-stage (an opencode `APIError`). A different
      provider from `model`, so one provider's bad night is not the run's.
    """

    stage: int
    model: str | None
    prompt_file: str | None
    timeout_ms: int
    start_timeout_ms: int
    isolation: Literal["independent", "none", "reused"]
    reuses_stage: int | None = None
    fallback_model: str | None = None


# Not on OpenCode Zen, where every primary model lives: Zen's free models route to
# whichever upstream it picks, and one of those 400'd stage 3 mid-implementation on
# 2026-10-10. kimi-k3 is not deepseek (big-pickle's upstream that night) nor nemotron,
# so a fallback author is still a different family from stage 5's reviewer.
AUTHOR_FALLBACK_MODEL = "nvidia/moonshotai/kimi-k3"
# The reviewer's own family on another provider: independence from the author is about
# the model family, which a provider switch does not change.
REVIEWER_FALLBACK_MODEL = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"

STAGES: tuple[StageSpec, ...] = (
    StageSpec(
        stage=1,
        model="opencode/muse-spark-1.3-contributor-free",
        prompt_file="stage-1.md",
        timeout_ms=60 * 60 * 1000,
        start_timeout_ms=120_000,
        isolation="independent",
        fallback_model=AUTHOR_FALLBACK_MODEL,
    ),
    StageSpec(
        stage=2,
        model="opencode/muse-spark-1.3-contributor-free",
        prompt_file="stage-2.md",
        timeout_ms=60 * 60 * 1000,
        start_timeout_ms=120_000,
        isolation="independent",
        fallback_model=AUTHOR_FALLBACK_MODEL,
    ),
    StageSpec(
        stage=3,
        model="opencode/big-pickle",
        prompt_file="stage-3.md",
        timeout_ms=90 * 60 * 1000,
        start_timeout_ms=120_000,
        isolation="independent",
        fallback_model=AUTHOR_FALLBACK_MODEL,
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
        # A different model family from stage 3's author, so the review is independent.
        model="opencode/nemotron-3-ultra-free",
        prompt_file="stage-5.md",
        timeout_ms=60 * 60 * 1000,
        start_timeout_ms=120_000,
        isolation="independent",
        fallback_model=REVIEWER_FALLBACK_MODEL,
    ),
    StageSpec(
        stage=6,
        model="opencode/big-pickle",
        prompt_file="stage-6.md",
        timeout_ms=60 * 60 * 1000,
        start_timeout_ms=120_000,
        isolation="reused",
        reuses_stage=3,
        fallback_model=AUTHOR_FALLBACK_MODEL,
    ),
)
