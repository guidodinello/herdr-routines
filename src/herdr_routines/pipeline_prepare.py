"""Pre-flight for the overnight feature pipeline, in code (issue 054 phase A).

Everything the pipeline used to ask the orchestrator *model* to do before stage 1 —
sync the parent clone, pick and claim the feature, create the shared worktree and
workspace, write `state.json` — happens here instead, in a subcommand the launcher
runs **before any agent exists**, and the resolved values are handed to the prompt as
`KEY=VALUE` lines instead of being recomputed by a model. See
`docs/pipeline/runs/20260930T050000Z/spec.md` §"Approach" and
`docs/pipeline/orchestrator-prompt.md`'s (now trimmed) Prerequisite section.

Order matches the prompt's old Prerequisite order exactly — sync → pick → worktree +
workspace → `state.json` — with one deliberate change: the worktree is created *after*
the pick, so a night with an empty backlog costs no worktree, no workspace and no
`state.json`, only the terminal report (issue 052). Sync and pick need no Herdr server;
the worktree/workspace steps are the first that do, which is why they sit last.

The non-`ok` paths write the terminal report themselves, in the same shape
`scripts/pipeline-launch.sh` uses for its own stubs, so `tick` reconciles them on the
usual later tick instead of waiting out the deadline.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from logger import get_logger

from herdr_routines.herdr import HerdrClient, HerdrCliError
from herdr_routines.pick_feature import (
    IssueParseError,
    ReclaimedPick,
    select_and_claim_feature,
)
from herdr_routines.repos import _fetch_and_fast_forward

log = get_logger(__name__)

DEFAULT_ISSUES_DIR = "docs/process/issues"

# The environment the shared workspace must be forked with, or every `herdr` call made
# from inside an agent in it settles `blocked` (docs/pipeline/design.md).
SHARED_WORKSPACE_ENV = {"HERDR_ENV": "1"}

# Exit codes. The launcher branches on these and nothing else; `2` is argparse's own
# usage code and is deliberately not redefined here.
EXIT_PREPARED = 0
EXIT_FAILURE = 1
EXIT_NO_FEATURE = 3


@dataclass(frozen=True, slots=True)
class PrepareResult:
    """A run's terminal disposition, the same shape as `runner.RunOutcome` (the existing
    convention for "how did this run end"). Every field beyond `outcome` is None on a
    non-`ok` outcome, because a skipped or failed prepare got no further than the step
    that stopped it."""

    outcome: Literal["ok", "no_feature", "sync_failed", "prepare_failed"]
    feature_idea: str | None
    feature_source: str | None
    issue_id: str | None
    worktree: str | None
    branch: str | None
    workspace_id: str | None
    state_path: Path | None
    reclaimed: tuple[ReclaimedPick, ...]


def prepare_run(
    *,
    run_id: str,
    repo_parent: Path,
    report: Path,
    deadline_epoch: int,
    base: str = "main",
    issues_dir: str | Path = DEFAULT_ISSUES_DIR,
    client: HerdrClient | None = None,
    claims_path: Path | None = None,
    worktrees_root: Path | None = None,
    reports_dir: Path | None = None,
    now: datetime | None = None,
) -> PrepareResult:
    """Sync, pick, create the worktree+workspace, write `state.json`. Never raises: a
    failure is a `PrepareResult` plus a terminal report at `report`, which is the file
    `tick` reconciles this run from.

    `client` is the seam that keeps this testable with no `herdr` binary on `PATH`
    (only the worktree/workspace steps use it). `deadline_epoch` has no default and is
    never computed here — the launcher forwards tick's value verbatim, so a prepare
    invocation can never invent a deadline (the failure that reaped a live run on
    2026-09-28)."""
    resolved_client = client if client is not None else HerdrClient()
    issues_path = Path(issues_dir)
    issues_root = (
        issues_path if issues_path.is_absolute() else repo_parent / issues_path
    )
    branch = f"auto/pipeline-{run_id}"
    reclaim_lines: list[str] = []
    claimed_issue_id: str | None = None

    def _reclaim_line(reclaimed: ReclaimedPick) -> None:
        reclaim_lines.append(
            f"reclaimed stale claim: issue {reclaimed.issue_id} "
            f"(claimed {reclaimed.claimed_at.isoformat()}, "
            "no open PR, no in-flight run)"
        )

    def _failed(
        outcome: Literal["no_feature", "sync_failed", "prepare_failed"],
        outcome_marker: str,
        reason: str,
    ) -> PrepareResult:
        # "Never raises" is the contract `_cmd_pipeline_prepare`'s exit-code table is
        # built on, so an unwritable report path must not escape as NotADirectoryError
        # and leave the launcher with a non-{0,3,1,2} exit and no report to reconcile.
        # Degrade to log-only: the run is lost either way, but the failure is still loud.
        try:
            _write_terminal_report(
                report,
                run_id=run_id,
                outcome=outcome_marker,
                lines=[
                    f"reason: {reason}",
                    # Once step 2 has claimed, the morning report has to say *which*
                    # issue went into the claimed set — the claim self-heals via
                    # `reclaim_stale_claims`, but a silent claim is exactly what issue
                    # 040 forbids. Absent before the pick, so the two are never confused.
                    *(
                        [f"claimed issue: {claimed_issue_id}"]
                        if claimed_issue_id is not None
                        else []
                    ),
                    *reclaim_lines,
                ],
            )
        except OSError as e:
            log.error(
                "pipeline-prepare: could not write the terminal report at %s: %s",
                report,
                e,
            )
        return PrepareResult(
            outcome=outcome,
            feature_idea=None,
            feature_source=None,
            issue_id=None,
            worktree=None,
            branch=None,
            workspace_id=None,
            state_path=None,
            reclaimed=(),
        )

    # 1. sync — the same primitive `sync-repo` and `ensure_repo` use. Deliberate even
    # though tick already synced at dispatch: that ran in tick's process on tick's
    # checkout, while the branch-off happens here, later, from a different checkout.
    try:
        _fetch_and_fast_forward(repo_parent, base=base)
    except RuntimeError as e:
        log.error("pipeline-prepare: repo sync failed for %s: %s", repo_parent, e)
        return _failed("sync_failed", "failed (repo_sync_failed)", str(e))

    # 2. pick + claim (out of tree; the issue file is never edited).
    try:
        picked = select_and_claim_feature(
            issues_root,
            mark=True,
            claims_path=claims_path,
            repo=repo_parent,
            worktrees_root=worktrees_root,
            reports_dir=reports_dir,
            now=now,
            on_reclaim=_reclaim_line,
        )
    except IssueParseError as e:
        log.error("pipeline-prepare: could not read %s: %s", issues_root, e)
        return _failed("prepare_failed", "failed (issues_unreadable)", str(e))
    if picked is None:
        log.info("pipeline-prepare: no feature to build in %s", issues_root)
        return _failed(
            "no_feature",
            "skipped (no_feature)",
            f"no open unclaimed issue in {Path(issues_dir).as_posix()}",
        )
    # The claim is on disk from here on, so every later `_failed` records it.
    claimed_issue_id = picked.issue.id

    # 3. shared worktree + branch, then the shared workspace (first step needing Herdr).
    try:
        worktree = resolved_client.worktree_create_full(
            cwd=str(repo_parent), branch=branch, base=base
        )
        workspace_id = _ensure_shared_workspace(
            resolved_client, cwd=worktree.path, label=f"pipeline-{run_id}"
        )
    except (HerdrCliError, OSError) as e:
        log.error("pipeline-prepare: worktree/workspace setup failed: %s", e)
        return _failed("prepare_failed", "failed (worktree_setup_failed)", str(e))

    # 4. state.json — the exact key set the prompt's example used, plus `feature_source`
    # (which the prompt required in prose but omitted from its own example).
    state_path = Path(worktree.path) / "state.json"
    try:
        _write_state_json(
            state_path,
            {
                "run_id": run_id,
                "current_stage": 0,
                "pr_number": None,
                "shared_worktree": worktree.path,
                "branch": worktree.branch,
                "shared_workspace": workspace_id,
                "deadline_epoch": deadline_epoch,
                "feature_source": _feature_source(picked.issue.path, repo_parent),
                "artifact_paths": {
                    "spec": str(
                        Path(worktree.path)
                        / "docs"
                        / "pipeline"
                        / "runs"
                        / run_id
                        / "spec.md"
                    ),
                    "report": str(report),
                },
                # Phase A does not move this writer (issue 054 phase B does) — the
                # orchestrator still records each stage's real `agent_session.value`.
                "stage_sessions": {},
            },
        )
    except OSError as e:
        log.error("pipeline-prepare: could not write %s: %s", state_path, e)
        return _failed("prepare_failed", "failed (state_write_failed)", str(e))

    log.info(
        "pipeline-prepare: run %s prepared on %s (issue %s, worktree %s)",
        run_id,
        branch,
        picked.issue.id,
        worktree.path,
    )
    return PrepareResult(
        outcome="ok",
        feature_idea=picked.feature_idea,
        feature_source=_feature_source(picked.issue.path, repo_parent),
        issue_id=picked.issue.id,
        worktree=worktree.path,
        branch=worktree.branch,
        workspace_id=workspace_id,
        state_path=state_path,
        reclaimed=picked.reclaimed,
    )


def render_prepared_values(result: PrepareResult) -> str:
    """The resolved values as `KEY=VALUE` lines for the launcher to append to the
    orchestrator's prompt header. This is a machine-parsed stdout contract, unlike every
    other `herdr-routines` command's human output — the values have to cross a shell
    boundary into a prompt, and a `KEY: value` table would be ambiguous there.

    Values are single-line: `\\` and newlines are escaped, so `FEATURE_IDEA`'s
    multi-paragraph body survives the round trip without breaking the line format. Logs
    go to stderr and never land here (the launcher redirects them to the run log)."""
    pairs = (
        ("FEATURE_IDEA", result.feature_idea),
        ("FEATURE_SOURCE", result.feature_source),
        ("ISSUE_ID", result.issue_id),
        ("WT", result.worktree),
        ("BRANCH", result.branch),
        ("SHARED_WS", result.workspace_id),
        ("STATE_JSON", str(result.state_path) if result.state_path else None),
    )
    return "\n".join(f"{key}={_escape_value(value)}" for key, value in pairs)


def _escape_value(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n")


def _feature_source(issue_path: Path, repo_parent: Path) -> str:
    """The picked issue's path relative to `$REPO_PARENT` — the `docs/process/issues/NNN-….md`
    form `state.json:feature_source` has always held, and what
    `pick_feature._issue_id_from_feature_source` parses back into an issue id."""
    try:
        return issue_path.relative_to(repo_parent).as_posix()
    except ValueError:
        return issue_path.as_posix()


def _ensure_shared_workspace(client: HerdrClient, *, cwd: str, label: str) -> str:
    """Locate-or-create the run's shared workspace: the one carrying this run's label
    *and* rooted at this run's worktree.

    Both conditions, never the label alone. `scripts/pipeline-launch.sh` creates a
    second workspace with the identical `pipeline-{run_id}` label for the orchestrator,
    rooted at `$REPO_PARENT`, so a label-only match can hand back that one — and every
    stage worker spawned into it would operate on the parent clone instead of the run's
    branch. Equally, `worktree create` already opens a workspace *on the worktree* and
    reports it as `worktree.open_workspace_id`, so a path-only match can hand back that
    one instead — a workspace not forked with `HERDR_ENV=1`, in which every `herdr` call
    from inside an agent settles `blocked` (docs/pipeline/design.md).

    `herdr workspace list` carries no `cwd` key (live `herdr 0.8.2`); `worktree.
    checkout_path` is the nearest real one, and it is absent entirely for a workspace
    whose cwd is not a checkout."""
    for entry in client.workspace_list():
        if entry.get("label") != label:
            continue
        entry_worktree = entry.get("worktree")
        if not isinstance(entry_worktree, dict):
            continue
        if entry_worktree.get("checkout_path") != cwd:
            continue
        workspace_id = entry.get("workspace_id")
        if isinstance(workspace_id, str) and workspace_id:
            return workspace_id
    return client.workspace_create(cwd=cwd, label=label, env=SHARED_WORKSPACE_ENV)


def write_state_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomic write: tmpfile in the *same directory* (so `os.replace` is a same-filesystem
    rename, not a copy), then the rename. A reader therefore never observes a partially
    written `state.json` (G-9), and a failure part-way leaves no file at `path` at all.

    Public since issue 056: `pipeline_run` advances `state.json` after every stage, and
    the atomicity guarantee is the whole point of this function — a second copy of the
    tmp+rename dance in `pipeline_run` would be free to lose it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def write_terminal_report(
    path: Path,
    *,
    run_id: str,
    outcome: str,
    lines: list[str],
    title: str = "pipeline-prepare report",
) -> None:
    """One writer for the report marker, used by every non-`ok` path. The `## Outcome:`
    line goes immediately after the title — the "near the top, trivial to `grep -m1`"
    rule the orchestrator prompt documents — so it can never drift between them.

    Public since issue 056 for the same reason as `write_state_json`: `pipeline_run` writes
    the terminal report on every abort/partial path, and the `## Outcome:` line's position
    is what `tick._classify_pipeline_outcome` greps for. `title` stays a parameter rather
    than being hardcoded to one writer's name because pipeline_prepare and pipeline_run are
    two different failure sources writing to the same file path, and a report that names
    the wrong one is the first thing a human reads at 5am."""
    body = [
        f"# Pipeline run {run_id} — {title}",
        "",
        f"## Outcome: {outcome}",
        "",
        *lines,
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body))


# Pre-issue-056 private names, kept as aliases so the ~20 existing call sites and
# tests/test_pipeline_prepare.py's import keep working without a rename sweep. New code
# must use the public names.
_write_state_json = write_state_json
_write_terminal_report = write_terminal_report
