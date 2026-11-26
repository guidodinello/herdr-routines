"""Pipeline stage workflow for issue 056 (phases B and C). Stdlib-only.

`STAGES` is a single source of truth imported by both `pipeline_run` and
`pipeline_watchdog`, so the two cannot disagree about the layout.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StageSpec:
    """Declarative per-stage specification.

    - `model` is None => no agent at all (stage 5: bookkeeping and the PR).
    - `prompt_file` is None when no agent is started.
    - `reuses_stage` is the stage number whose session id is reused, if any (stage 7
      reuses stage 4); every other stage with a model runs in its own session (G-17).
    - `fallback_model` is what `pipeline_run` resumes the stage's session on, once, when
      `model`'s provider rejects a call mid-stage (a `provider.*` error in the opencode
      session record, or Zen's quota text on screen). A different
      provider from `model`, so one provider's bad night is not the run's.
    """

    stage: int
    model: str | None
    prompt_file: str | None
    timeout_ms: int
    start_timeout_ms: int
    reuses_stage: int | None = None
    fallback_model: str | None = None


# Not on OpenCode Zen, where every primary model lives: Zen's free models route to
# whichever upstream it picks, and one of those 400'd the implement stage mid-run on
# 2026-10-10. Not deepseek (big-pickle's upstream that night), glm nor nemotron, so a
# fallback implementer is still a different family from stage 6's reviewer. Was
# kimi-k3 until NVIDIA withdrew it (gone by 2026-11-10; opencode ran its default
# big-pickle in its place without an error).
AUTHOR_FALLBACK_MODEL = "nvidia/minimaxai/minimax-m3.1-flash"
# Off Zen (the fallback exists for Zen's quota) and outside every author family:
# independence from the author is about the model family, which a provider switch does
# not change. Replaced Nemotron on OpenRouter on 2026-11-26 (OpenRouter free was the
# flakiest provider; Zen withdrew its nemotron-free by 2026-10-29). Chosen after probing
# the Pi: it answers in 1-2 s and used tools correctly, while nvidia/z-ai/glm-5.3-flash
# hung 3/3 and the gemma-4-31b-it options were rate-limited or errored. Quality is
# unverified: no fitted review data for laguna-xs (its sibling laguna-s-2.1 had 2 reviews,
# 0 findings), so the babysit-prs leaderboard is the check. Known risk: it shares the
# NVIDIA provider with AUTHOR_FALLBACK_MODEL, so one NVIDIA outage hits both fallbacks.
REVIEWER_FALLBACK_MODEL = "nvidia/poolside/laguna-xs-2.1"

# Which family each configured model belongs to. Spelled out rather than matched on a
# substring of the id, so a new model id has to be given a family on purpose, and the
# independence rules (test author vs implementer, implementer vs reviewer) compare
# families instead of strings. `big-pickle` is Zen's own router: its upstream changes
# night to night, so it is treated as a family of its own, which is all the repo can say.
MODEL_FAMILY: dict[str, str] = {
    "opencode/deepseek-v4.1-flash-free": "deepseek",
    "opencode/big-pickle": "zen-router",
    AUTHOR_FALLBACK_MODEL: "minimax",
    REVIEWER_FALLBACK_MODEL: "laguna",
}


def model_family(model: str) -> str:
    """The family of a configured model; raises on one with no declared family."""
    try:
        return MODEL_FAMILY[model]
    except KeyError:
        raise ValueError(f"no model family declared for {model!r}") from None


def stage_families(spec: StageSpec) -> set[str]:
    """Every family a stage may run on: its primary model and its fallback."""
    return {model_family(m) for m in (spec.model, spec.fallback_model) if m}


# The stages `pipeline_run` has to treat differently, by role. Named here, next to the
# table, so a renumbering is one edit and a test can check each still points at the stage
# it means (test_stages_table_matches_the_prompt_files_on_disk).
# Stage 2 commits the spec the later stages are diffed against, so code sweeps it too.
SPEC_REVIEW_STAGE = 2
TEST_AUTHOR_STAGE = 3
IMPLEMENT_STAGE = 4
# The stage with no agent: bookkeeping, then the PR.
PR_STAGE = 5
# The stage that reviews the PR. Its gate counts tier labels only from trusted authors,
# the same set stage 7 is scoped to.
PR_REVIEW_STAGE = 6
# The stage that acts on review feedback, which code fetches and filters to trusted
# authors before the prompt is built (`pipeline_run._review_feedback`).
ADDRESS_COMMENTS_STAGE = 7

STAGES: tuple[StageSpec, ...] = (
    StageSpec(
        stage=1,
        model="opencode/deepseek-v4.1-flash-free",
        prompt_file="stage-1.md",
        timeout_ms=60 * 60 * 1000,
        start_timeout_ms=120_000,
        fallback_model=AUTHOR_FALLBACK_MODEL,
    ),
    StageSpec(
        stage=2,
        model="opencode/deepseek-v4.1-flash-free",
        prompt_file="stage-2.md",
        timeout_ms=60 * 60 * 1000,
        start_timeout_ms=120_000,
        fallback_model=AUTHOR_FALLBACK_MODEL,
    ),
    StageSpec(
        stage=3,
        # The test author: writes the acceptance tests and nothing else. A different
        # family from stage 4's implementer, primary and fallback, so the tests are not
        # written by the model they are meant to check.
        model="opencode/deepseek-v4.1-flash-free",
        prompt_file="stage-3.md",
        timeout_ms=60 * 60 * 1000,
        start_timeout_ms=120_000,
        fallback_model=REVIEWER_FALLBACK_MODEL,
    ),
    StageSpec(
        stage=4,
        model="opencode/big-pickle",
        prompt_file="stage-4.md",
        timeout_ms=90 * 60 * 1000,
        start_timeout_ms=120_000,
        fallback_model=AUTHOR_FALLBACK_MODEL,
    ),
    StageSpec(
        stage=5,
        model=None,
        prompt_file=None,
        timeout_ms=60 * 60 * 1000,
        start_timeout_ms=120_000,
    ),
    StageSpec(
        stage=6,
        # A different model family from stage 4's implementer, so the review is
        # independent. Zen withdrew nemotron-3-ultra-free by 2026-10-29 and
        # glm-5.3-flash-free by 2026-11-19. Its fallback is laguna-xs-2.1 on NVIDIA
        # (family "laguna": not stage 4's, not deepseek; see REVIEWER_FALLBACK_MODEL). deepseek-v4.1-flash had the best
        # accepted-finding rate of the free reviewers left on fitted's babysit-prs
        # leaderboard (96%, only 3 reviews, 2026-11-22). Known risk, accepted: big-pickle
        # (stage 4) has routed to deepseek, so on such a night the review is not
        # independent of the implementer.
        model="opencode/deepseek-v4.1-flash-free",
        prompt_file="stage-6.md",
        timeout_ms=60 * 60 * 1000,
        start_timeout_ms=120_000,
        fallback_model=REVIEWER_FALLBACK_MODEL,
    ),
    StageSpec(
        stage=7,
        model="opencode/big-pickle",
        prompt_file="stage-7.md",
        timeout_ms=60 * 60 * 1000,
        start_timeout_ms=120_000,
        reuses_stage=4,
        fallback_model=AUTHOR_FALLBACK_MODEL,
    ),
)
