"""`herdr-routines ps`: what's currently running across Herdr and the pipeline.

Read-only merge of two sources (spec 20260825T070012Z):
- live agents via `HerdrClient.agent_statuses()` (same CommandRunner seam as tick),
- in-progress pipeline runs, discovered by scanning `state.json` files under the
  reports directory (`default_reports_dir()` / `$HERDR_PLUGIN_STATE_DIR/reports/`,
  the same base as history.py/runner.py).

A pipeline run counts as in progress while its final report
(`pipeline-<run_id>.md`) has not been written yet; its row then enriches a bare
agent name like `pl-4-20260825T070012Z` to "pipeline run 20260825T… stage 4/6".
Every failure here is fail-open: an unreachable Herdr server or an unreadable
state.json degrades to a warning, never a crash.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from logger import get_logger

from herdr_routines.herdr import LIVE_AGENT_STATUSES, HerdrClient, HerdrCliError
from herdr_routines.runner import default_reports_dir
from herdr_routines.table import render_table

log = get_logger(__name__)

# The pipeline workflow (docs/pipeline/design.md) always runs exactly six stages,
# numbered 1..6; worker agents are named pl-<stage>-<run_id>.
TOTAL_PIPELINE_STAGES = 6

STATE_JSON_NAME = "state.json"


@dataclass(frozen=True, slots=True)
class PipelineRun:
    run_id: str
    current_stage: int
    state_path: Path
    report_path: Path

    @property
    def in_progress(self) -> bool:
        """In progress until the final report exists; a stale state.json whose report
        was already written is complete and must not claim a live agent's identity."""
        return not self.report_path.exists()

    def detail(self) -> str:
        stage = min(self.current_stage, TOTAL_PIPELINE_STAGES)
        return f"pipeline run {self.run_id[:10]}… stage {stage}/{TOTAL_PIPELINE_STAGES}"


def scan_pipeline_runs(reports_dirs: list[Path]) -> dict[str, PipelineRun]:
    """All parseable pipeline state.json files under the reports directories, keyed by
    run_id. Unparseable/incomplete files are skipped with a warning (fail open); when
    two directories describe the same run_id the first entry wins."""
    runs: dict[str, PipelineRun] = {}
    for base in reports_dirs:
        for state_path in sorted(base.rglob(STATE_JSON_NAME)):
            run = _parse_state_json(state_path)
            if run is None:
                continue
            runs.setdefault(run.run_id, run)
    return runs


def default_scan_dirs() -> list[Path]:
    """The one directory contract (spec 20260825T070012Z §Risks): the reports dir plus
    its parent base — the same `$HERDR_PLUGIN_STATE_DIR` fallback as history/runner — so
    a state.json dropped either beside history.jsonl or under reports/ is found."""
    reports = default_reports_dir()
    return [reports, reports.parent]


def _parse_state_json(state_path: Path) -> PipelineRun | None:
    try:
        raw = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("skipping unreadable %s: %s", state_path, e)
        return None
    if not isinstance(raw, dict):
        log.warning("skipping %s: top level is not an object", state_path)
        return None
    run_id = raw.get("run_id")
    current_stage = raw.get("current_stage")
    if not isinstance(run_id, str) or not run_id:
        log.warning("skipping %s: missing run_id", state_path)
        return None
    if not isinstance(current_stage, int) or isinstance(current_stage, bool):
        log.warning("skipping %s: missing current_stage", state_path)
        return None
    return PipelineRun(
        run_id=run_id,
        current_stage=current_stage,
        state_path=state_path,
        report_path=_resolve_report_path(raw, state_path, run_id),
    )


def _resolve_report_path(raw: dict, state_path: Path, run_id: str) -> Path:
    """Where this run's final `pipeline-<run_id>.md` report lives. The orchestrator
    records the exact path under artifact_paths.report (reports root); fall back to
    the convention (beside state.json, then its parent — i.e. the reports root) when
    the field is missing."""
    artifacts = raw.get("artifact_paths")
    if isinstance(artifacts, dict):
        report = artifacts.get("report")
        if isinstance(report, str) and report:
            return Path(report)
    name = f"pipeline-{run_id}.md"
    for candidate in (state_path.parent / name, state_path.parent.parent / name):
        if candidate.exists():
            return candidate
    return state_path.parent.parent / name


@dataclass(frozen=True, slots=True)
class PsRow:
    agent: str
    status: str
    detail: str


def build_ps_rows(
    agent_statuses: dict[str, str], pipeline_runs: dict[str, PipelineRun]
) -> list[PsRow]:
    """One row per live (working) agent, bare names enriched to their pipeline run's
    stage indicator when an in-progress run matches the name (`pl-<N>-<run_id>`).
    Settled idle/done agents stay registered until their pane closes but are not
    "currently running" (LIVE_AGENT_STATUSES), so they are not listed."""
    in_progress = {rid: r for rid, r in pipeline_runs.items() if r.in_progress}
    rows = []
    for name, status in sorted(agent_statuses.items()):
        if status not in LIVE_AGENT_STATUSES:
            continue
        # Herdr lowercases agent names, so match run ids case-insensitively
        # (state.json keeps `20260825T070012Z`, the agent row says `…t070012z`).
        lowered = name.lower()
        matched = next(
            (r for rid, r in in_progress.items() if rid.lower() in lowered), None
        )
        rows.append(
            PsRow(agent=name, status=status, detail=matched.detail() if matched else "")
        )
    return rows


def collect_ps_rows(client: HerdrClient) -> tuple[list[PsRow], list[str]]:
    """Query Herdr and the reports dir; returns (rows, warnings). Never raises on
    HerdrCliError/OSError — an unreachable server yields an empty table plus warning so
    `ps` stays usable on hosts where Herdr is down."""
    try:
        agent_statuses = client.agent_statuses()
    except (HerdrCliError, OSError) as e:
        return [], [f"herdr unavailable, showing nothing running: {e}"]
    runs = scan_pipeline_runs(default_scan_dirs())
    return build_ps_rows(agent_statuses, runs), []


def render_ps(rows: list[PsRow]) -> str:
    return render_table(
        ["AGENT", "STATUS", "DETAIL"],
        [[r.agent, r.status, r.detail] for r in rows],
    )
