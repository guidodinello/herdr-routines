"""The pipeline's stage loop, in code (issue 056 phases B and C).

Before this module the overnight pipeline's stage sequence lived in two places that
could disagree: `docs/pipeline/orchestrator-prompt.md` prose describing six stages and
their gates, and a bash marker-poll loop in `scripts/pipeline-launch.sh`. An LLM read
the prose and executed it, so "run gate 5" meant "run something that resembles gate 5
and report the outcome you need". On PR #81 that produced a green pipeline history line
for a run whose spec review had never happened.

So the loop is here, in `STAGES` order, and the only thing a model is asked to do is the
work of one stage. Everything else is code:

- `stage_sessions` is written from the started agent's real `agent_session.value`
  (`HerdrClient.agent_session_id`) after `agent_start` returns and before the prompt is
  sent, so a fabricated id is unrepresentable rather than merely detectable.
- Each stage's gate is a `GateVerdict` value, not prose. A failing gate aborts before the
  next stage and names itself and its reason in the terminal report.
- The liveness heartbeat is written from inside the shared wait loop's poll closure, so
  it advances whether or not anything remembers to advance it.
- The deadline comes from `state.json` and is checked between stages, never mid-stage.

`RunOutcome.outcome` is `ok | partial | failed` — `ok`, not `done`.
`tick._classify_pipeline_outcome` maps `## Outcome: ok…` to `done` and anything it does
not recognise to `interrupted_unknown`, so a `done`-flavoured vocabulary would file every
successful night as interrupted.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from logger import get_logger

from herdr_routines.auto_fix import GhClient, RealGhClient
from herdr_routines.gates import (
    GateVerdict,
    gate3_test_names,
    gate3_test_names_present,
    remote_owner_and_repo,
    run_stage_gate,
)
from herdr_routines.herdr import (
    HerdrClient,
    HerdrCliError,
    PromptWatchdogKilled,
)
from herdr_routines.pipeline_prepare import write_state_json, write_terminal_report
from herdr_routines.pipeline_stages import STAGES, StageSpec
from herdr_routines.pipeline_watchdog import (
    heartbeat_log_path,
    validate_stage_sessions,
)
from herdr_routines.runner import SUCCESS_AGENT_STATUSES
from herdr_routines.signing import (
    ResignError,
    resign_local_commits,
    resign_unsigned_branch,
)
from herdr_routines.wait_loop import prompt_with_watchdog

log = get_logger(__name__)

# The agent kind every stage starts. `opencode` is the only kind
# `build_agent_start_args` knows a native model flag for (AGENT_MODEL_FLAGS), and it is
# what every row of the prompt's harness table uses.
STAGE_AGENT_KIND = "opencode"

# `pipeline_prepare.EXIT_*` is the convention: 0 ok, 1 partial-or-failed, 2 argparse
# usage. A partial night is a bad night, and `systemctl status` should not have to read a
# report to know that.
EXIT_OK = 0
EXIT_PARTIAL_OR_FAILED = 1

Outcome = Literal["ok", "partial", "failed"]

# The exact strings `tick._classify_pipeline_outcome` parses. `reason` is folded into the
# marker line because the classifier matches on the whole parenthetical — emitting a bare
# `## Outcome: failed` for a quota wedge would file it as `orchestrator_failed` and lose
# the signal `_process_pipeline_job` retries `fallback_model` on.
_REPORT_OUTCOME: dict[tuple[Outcome, str | None], str] = {
    ("partial", "partial_deadline"): "partial (deadline exceeded)",
    ("failed", "quota_exhausted"): "failed (quota_exhausted)",
}

# How long a git/gh command may take inside a stage. Long enough for a push of a real
# branch, short enough that the deadline check still runs on a human timescale.
GIT_TIMEOUT_S = 300.0

# The branch every pipeline run is cut from (pipeline_prepare's default base) — needed
# here only to know which commits are the run's own when re-signing them.
PIPELINE_BASE = "main"


class _Abort(Exception):
    """A stage's own failure, carrying the report lines it wants written.

    An exception rather than a return value because every failure path has to unwind the
    same way — close the pane, notify, write the report — and `run_pipeline` should have
    exactly one place that does that rather than one per stage.
    """

    def __init__(
        self, reason: str, *, detail: str | None = None, lines: list[str] | None = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.lines = lines or []


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What one `run_pipeline` call did. Mirrors `runner.RunOutcome`'s shape (outcome /
    reason / error) so the launcher and tick read one vocabulary — with `outcome` in the
    spec's `ok | partial | failed` rather than runner's `done | failed | ...`."""

    outcome: Outcome
    run_id: str
    reason: str | None = None
    error: str | None = None
    stage: int | None = None

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.outcome == "ok" else EXIT_PARTIAL_OR_FAILED


@dataclass(frozen=True, slots=True)
class _State:
    """The parsed `state.json`, holding exactly the fields this loop reads.

    Re-read from disk after every write rather than mutated in place: `state.json` is
    also what the watchdog and `tick`'s reconcile read while the run is in flight, and a
    run whose own view has drifted from the file is how two processes end up disagreeing
    about which stage the night is on.
    """

    run_id: str
    worktree: Path
    branch: str
    spec: Path
    issue_source: str | None
    deadline_epoch: int | None
    shared_workspace: str | None
    current_stage: int
    pr_number: int | None
    stage_sessions: dict[str, str]
    # The document as it was read, kept so writing it back cannot silently drop a key
    # this loop does not know about. See `payload`.
    raw: dict[str, object] = field(default_factory=dict)

    @classmethod
    def read(cls, path: Path) -> _State:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise TypeError(f"state.json is not a JSON object: {path}")

        def opt_str(key: str) -> str | None:
            value = raw.get(key)
            return value if isinstance(value, str) and value else None

        def opt_int(key: str) -> int | None:
            value = raw.get(key)
            # bool is an int subclass; `True` as a stage number is a bug, not a 1.
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            return None

        artifacts = raw.get("artifact_paths")
        spec_raw = artifacts.get("spec") if isinstance(artifacts, dict) else None
        worktree_raw = opt_str("shared_worktree")
        branch_raw = opt_str("branch")
        if not isinstance(spec_raw, str) or not worktree_raw or not branch_raw:
            raise ValueError(
                "state.json is missing one of artifact_paths.spec, shared_worktree, "
                "branch — all three are written by pipeline-prepare"
            )
        sessions = raw.get("stage_sessions")
        stage = opt_int("current_stage")
        return cls(
            run_id=opt_str("run_id") or "",
            worktree=Path(worktree_raw),
            branch=branch_raw,
            spec=Path(spec_raw),
            issue_source=opt_str("feature_source"),
            deadline_epoch=opt_int("deadline_epoch"),
            shared_workspace=opt_str("shared_workspace"),
            current_stage=stage or 0,
            pr_number=opt_int("pr_number"),
            stage_sessions=(
                {str(k): v for k, v in sessions.items() if isinstance(v, str)}
                if isinstance(sessions, dict)
                else {}
            ),
            raw=dict(raw),
        )

    def payload(self) -> dict[str, object]:
        """The full document to write back: the file as read, with this loop's fields
        replaced on top.

        Start from the parsed document rather than from a literal, because
        `write_state_json` replaces the file wholesale — a payload built only from the
        fields listed here would erase everything else on the first `commit`. Two
        concrete casualties of that, both load-bearing elsewhere: `artifact_paths.report`
        (phase A records it; `ps._resolve_report_path` trusts it first and the watchdog's
        `_candidate_report_paths` uses it to tell a finished run from a stalled one) and
        any key a future pre-flight adds. Preserving the document rather than widening
        this dataclass means neither can be lost by forgetting to add a field here."""
        doc = dict(self.raw)
        existing = doc.get("artifact_paths")
        artifacts = dict(existing) if isinstance(existing, dict) else {}
        artifacts["spec"] = str(self.spec)
        doc.update(
            {
                "run_id": self.run_id,
                "current_stage": self.current_stage,
                "pr_number": self.pr_number,
                "shared_worktree": str(self.worktree),
                "branch": self.branch,
                "shared_workspace": self.shared_workspace,
                "deadline_epoch": self.deadline_epoch,
                "feature_source": self.issue_source,
                "artifact_paths": artifacts,
                "stage_sessions": dict(self.stage_sessions),
            }
        )
        return doc

    def commit(self, state_json: Path, **changes: object) -> _State:
        """Atomically write the changed state and return it. The write is the resume
        point, so it happens only after a gate has passed or a session id is real."""
        updated = replace(self, **changes)  # type: ignore[arg-type]
        write_state_json(state_json, updated.payload())
        return updated


# ---------------------------------------------------------------------------
# Names, ids and prompt text
# ---------------------------------------------------------------------------


def _stage_agent_name(stage: int, run_id: str) -> str:
    """`pl-<N>-<run_id>`, lowercased.

    Lowercased because `herdr` lowercases the branch into the worktree directory name, and
    the agent name has to match what `agent_session_id` is later asked about — an agent
    started under one casing is not found under the other on every host whose filesystem
    folded it."""
    return f"pl-{stage}-{run_id}".lower()


def _issue_id(feature_source: str | None) -> str | None:
    """The `NNN` in `docs/process/issues/056-<slug>.md`, or None.

    Read from the recorded `feature_source` rather than a new flag, so stage 4 cannot be
    pointed at a different issue than the one prepare picked — and so gate 4's
    `git show HEAD:<path>` flip check checks the same file stage 3 was told to edit."""
    if not feature_source:
        return None
    match = re.match(r"^(\d+)", Path(feature_source).name)
    return match.group(1) if match else None


def _spec_title(spec_text: str) -> str:
    """The spec's first `# ` heading, which becomes the PR title.

    Not a constructed `feat: <thing> (<run_id>)` string: the title is the one line a
    human reads in a list of forty PRs, and a spec whose first line already says what
    the feature is should not need re-summarising by whoever opened the PR."""
    for line in spec_text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "pipeline run"


def _spec_section(spec_text: str, heading: str) -> str:
    """The body of `## <heading>` up to the next `## `, or "" when the heading is absent.

    Anchored on `^## <heading>$` with `re.MULTILINE` rather than a substring search,
    because `## problem` must not match `## problems we hit`."""
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL
    )
    match = pattern.search(spec_text)
    return match.group(1).strip() if match else ""


def _pr_body(spec_text: str, *, issue_id: str | None, run_id: str) -> str:
    """The PR body: the spec's problem statement, the acceptance tests, and the close.

    `Closes #<id>` is what makes "merging this PR closes it" real, and it is only real
    if the issue closes on merge — which is why the `status: done` flip rides this
    branch rather than landing separately. When no issue id is recorded the body says so
    instead of omitting the line, because a reviewer cannot tell an omitted reference
    from an oversight."""
    names = gate3_test_names(spec_text)
    lines = [
        "## problem",
        "",
        _spec_section(spec_text, "problem"),
        "",
        "## Acceptance tests",
        "",
        *(f"- `{name}`" for name in names),
        "",
    ]
    if issue_id:
        lines.append(
            f"Closes #{issue_id} — the `status: done` flip rides this PR and lands on main "
            f"on merge."
        )
    else:
        lines.append(
            f"Pipeline run {run_id}: `state.json:feature_source` named no issue id, so "
            f"this PR carries no closing reference."
        )
    return "\n".join(lines).rstrip() + "\n"


def _prompt_values(
    state: _State,
    *,
    run_id: str,
    report: Path,
    state_json: Path,
    stage: int,
) -> dict[str, str]:
    """Every substitution a stage prompt may use, all derived from `state.json`.

    The spec's rule: the prompts are the orchestrator prompt's stage text with only
    state-derived values substituted, so nothing in a prompt is a fact the run decided
    for itself. `deadline_epoch` is passed through *verbatim from the file* and never
    recomputed — a fallback model once wrote one a year in the past and got a live run
    reaped fifteen minutes in (2026-09-28)."""
    return {
        "RUN_ID": run_id,
        "BRANCH": state.branch,
        "WT": str(state.worktree),
        "SPEC": str(state.spec),
        "REPORT": str(report),
        "STATE_JSON": str(state_json),
        "STAGE": str(stage),
        "FEATURE_SOURCE": state.issue_source or "",
        "ISSUE_FILE": state.issue_source or "",
        "SHARED_WORKSPACE": state.shared_workspace or "",
        "DEADLINE_EPOCH": (
            "" if state.deadline_epoch is None else str(state.deadline_epoch)
        ),
        # Stages 1-4 run before a PR exists; stage 5 and 6 are prompted with the number
        # stage 4 recorded, which is why this is "" rather than a placeholder.
        "PR_NUMBER": "" if state.pr_number is None else str(state.pr_number),
    }


def _substitute(text: str, values: dict[str, str]) -> str:
    """Replace `$NAME` and `${NAME}` from `values`.

    Every key is substituted even when its value is empty, so a stage that runs before
    the value exists gets an empty string rather than a literal `$PR_NUMBER` sent to a
    model as if it were text to act on."""
    for key, value in values.items():
        text = text.replace(f"${{{key}}}", value).replace(f"${key}", value)
    return text


# `$FOO`, `${FOO}` — an upper-case word after a dollar sign. Deliberately not matching
# `$(` so a prompt can still contain a shell command substitution as example text.
_PLACEHOLDER_RE = re.compile(r"\$\{?[A-Z][A-Z0-9_]*\}?")


def _read_prompt(prompts_dir: Path, spec: StageSpec, values: dict[str, str]) -> str:
    """Read `spec.prompt_file`, substitute, and refuse to send an unresolved placeholder.

    A prompt still carrying `$WT` after substitution is a stage that will read a
    literal dollar-word as a path and write its work somewhere no gate will ever look —
    so this fails the stage loudly instead of burning its full timeout discovering it."""
    if spec.prompt_file is None:
        raise _Abort(
            "prompt_file_missing",
            detail=f"stage {spec.stage} declares a model but no prompt file",
        )
    path = prompts_dir / spec.prompt_file
    try:
        text = _substitute(path.read_text(), values)
    except OSError as e:
        raise _Abort("prompt_unreadable", detail=f"{path}: {e}") from e
    unresolved = sorted(set(_PLACEHOLDER_RE.findall(text)))
    if unresolved:
        raise _Abort(
            "prompt_placeholder_unresolved",
            detail=f"{path}: {', '.join(unresolved)}",
        )
    return text


# ---------------------------------------------------------------------------
# Subprocess seam
# ---------------------------------------------------------------------------


def _run_bounded(argv: Sequence[str], *, timeout_s: float) -> tuple[int, str, str]:
    """Run a command with a wall-clock bound; 124 on overrun.

    An exception on timeout rather than a return, because this is the stage loop: a
    `TimeoutExpired` escaping would skip the terminal report that tick reconciles the
    night from."""
    try:
        proc = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(argv)}: timed out after {timeout_s:.0f}s"
    except OSError as e:
        return 127, "", f"{' '.join(argv)}: {e}"
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Settle mapping and the session-layout gate (G-17)
# ---------------------------------------------------------------------------


def _settle_outcome(status: str) -> tuple[bool, str | None]:
    """Map a settled agent status onto (continue?, reason).

    `idle`/`done` is `runner.SUCCESS_AGENT_STATUSES`, the same set the routine-job path
    treats as success — one definition of "the agent finished", so the two paths cannot
    disagree about which status means done.

    `blocked` and everything else are terminal, and for different reasons. A `blocked`
    agent settled on a prompt nothing will ever answer (there is no one at the keyboard
    at 03:00); an unrecognised status means the run was interrupted in a way whose cause
    this process did not observe. Filing both as one reason would make a wedge
    indistinguishable from a crash in the morning history."""
    if status in SUCCESS_AGENT_STATUSES:
        return True, None
    if status == "blocked":
        return False, "stage_blocked"
    return False, "interrupted_unknown"


def _heartbeat_hook(
    path: Path, stage: int, clock: Callable[[], float]
) -> Callable[[], None]:
    """A no-arg callback for the shared wait loop that appends one line per poll.

    Appended, never rewritten: `pipeline_watchdog.is_stalled` decides liveness from this
    file's mtime, and one truncating write per poll would be a read-modify-write race
    with the watchdog's stat over a file two processes touch.

    Never raises into the wait loop. The heartbeat exists to describe a run that is, by
    construction, still going; letting a full `/tmp` — a real failure mode on the host
    this pipeline runs on — kill the stage it is trying to keep alive would turn a
    monitoring problem into a build failure."""

    def hook() -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                handle.write(f"{int(clock())} stage={stage} poll\n")
        except OSError as e:
            log.warning("heartbeat write failed: %s", e)

    return hook


# ---------------------------------------------------------------------------
# Stage 4 — the PR, with no agent at all
# ---------------------------------------------------------------------------


def _issue_committed_done(
    state: _State, runner: Callable[..., tuple[int, str, str]]
) -> tuple[bool, str]:
    """Gate 4's second condition: the picked issue is committed as `status: done`.

    Both halves are needed and neither alone suffices. `git status --porcelain <path>`
    empty proves the flip was committed with nothing left dangling; `git show
    HEAD:<path>` containing a `status: done` line proves it was actually flipped. A
    flipped-but-uncommitted file fails the first, a committed-but-unflipped file fails
    the second, and "merging this PR closes it" is false in both cases."""
    if not state.issue_source:
        return (
            False,
            "state.json records no feature_source, so there is no issue to close",
        )
    code, stdout, stderr = runner(
        [
            "git",
            "-C",
            str(state.worktree),
            "status",
            "--porcelain",
            state.issue_source,
        ],
        timeout_s=GIT_TIMEOUT_S,
    )
    if code != 0:
        return False, f"git status failed: {stderr.strip()}"
    if stdout.strip():
        return False, f"{state.issue_source} has uncommitted changes"
    code, stdout, stderr = runner(
        ["git", "-C", str(state.worktree), "show", f"HEAD:{state.issue_source}"],
        timeout_s=GIT_TIMEOUT_S,
    )
    if code != 0:
        return False, f"issue file is not on HEAD: {stderr.strip()}"
    if not any(line.strip() == "status: done" for line in stdout.splitlines()):
        return False, f"{state.issue_source} on HEAD is not `status: done`"
    return True, "issue committed as status: done"


def _open_pr(
    state: _State,
    *,
    spec_text: str,
    gh: GhClient,
    owner: str,
    repo: str,
    runner: Callable[..., tuple[int, str, str]],
) -> int:
    """Push the branch, open the PR, return its number. No agent is involved.

    `gh pr create` has no machine-readable output mode — it prints the new PR's URL — so
    the number comes back through `GhClient.pr_create`, which parses it out of that URL.
    It is never predicted from a list of open PRs, because a number invented here would
    be handed to gate CI and stage 5 as if it were real.

    Push before create, always: `gh pr create` refuses a branch that is not on the remote,
    and the failure mode of doing it the other way round is a PR that silently does not
    exist.

    Re-signs the stage agents' commits first: no clone config can stop an agent running
    `git -c commit.gpgsign=false commit` (PR #140), and the ruleset blocks an unsigned PR
    at merge. Nothing is on origin yet, so this needs no force-push. Best-effort — an
    unsigned push still leaves a PR for stage 5 to review."""
    try:
        resign_local_commits(state.worktree, base=PIPELINE_BASE)
    except (ResignError, OSError, subprocess.TimeoutExpired) as e:
        log.warning("pipeline %s: could not re-sign before push: %s", state.run_id, e)
    code, _stdout, stderr = runner(
        ["git", "-C", str(state.worktree), "push", "-u", "origin", state.branch],
        timeout_s=GIT_TIMEOUT_S,
    )
    if code != 0:
        raise _Abort("push_failed", detail=stderr.strip() or f"exit {code}")

    try:
        return gh.pr_create(
            owner=owner,
            repo=repo,
            branch=state.branch,
            title=_spec_title(spec_text),
            body=_pr_body(
                spec_text, issue_id=_issue_id(state.issue_source), run_id=state.run_id
            ),
        )
    except RuntimeError as e:
        # gh's own failure, or a URL this version of gh printed in a shape we cannot read
        # a number out of. Either way there is no PR to gate, so the run cannot continue.
        raise _Abort("pr_create_failed", detail=str(e)) from e


# ---------------------------------------------------------------------------
# One agent stage
# ---------------------------------------------------------------------------


def _run_agent_stage(
    spec: StageSpec,
    state: _State,
    *,
    run_id: str,
    state_json: Path,
    report: Path,
    prompts_dir: Path,
    failure_markers: Sequence[str],
    client: HerdrClient,
    heartbeat: Path,
    runner: Callable[..., tuple[int, str, str]],
    clock: Callable[[], float],
) -> str:
    """Start one stage's agent, prompt it, wait for settle, record its real session id.

    Returns the pane id so the caller can close it once the gate has passed (G-16:
    close on gate-pass, not only at end of run — and never hold stage 3's pane open from
    stage 4 through stage 6, which is what made the resume a fork instead of a resume).

    Raises `_Abort` for every failure, having already closed the pane: a pane left open
    by a crashed stage is a live agent the next tick's reconcile has to adopt or reap,
    and the whole point of closing per-stage is that there is nothing to reap."""
    assert spec.model is not None, "caller must route model=None stages to _open_pr"
    agent_name = _stage_agent_name(spec.stage, run_id)
    pane_id = _open_pane(client, state, spec, agent_name)

    resume_session = (
        state.stage_sessions.get(str(spec.reuses_stage))
        if spec.reuses_stage is not None
        else None
    )
    if spec.reuses_stage is not None and not resume_session:
        raise _Abort(
            "resume_session_missing",
            detail=(
                f"stage {spec.stage} resumes stage {spec.reuses_stage}, whose session id "
                f"is not in state.json — there is nothing to resume"
            ),
        )

    try:
        client.agent_start(
            name=agent_name,
            kind=STAGE_AGENT_KIND,
            pane_id=pane_id,
            start_timeout_ms=spec.start_timeout_ms,
            model=spec.model,
            session_id=resume_session,
        )
    except (HerdrCliError, OSError, ValueError) as e:
        _close_pane(client, pane_id)
        raise _Abort("agent_start_failed", detail=str(e)) from e

    prompt = _read_prompt(
        prompts_dir,
        spec,
        _prompt_values(
            state, run_id=run_id, report=report, state_json=state_json, stage=spec.stage
        ),
    )

    try:
        status = prompt_with_watchdog(
            client,
            job_name=f"pipeline-stage-{spec.stage}",
            target=agent_name,
            text=prompt,
            timeout_ms=spec.timeout_ms,
            markers=failure_markers,
            prompt_text=prompt,
            on_poll_hook=_heartbeat_hook(heartbeat, spec.stage, clock),
            # No start-race retry here: the wait loop's retry exists for a session
            # backend rejecting the *first* prompt seconds after start, and every stage
            # prompt has already been delivered once by the time it would apply. A
            # resend of a 90-minute implementation prompt would double the run's side
            # effects, which is the cost the retry whitelist exists to avoid.
            retry_delays_s=(),
        )
    except PromptWatchdogKilled as e:
        _close_pane(client, pane_id)
        raise _Abort(
            "quota_exhausted",
            detail=str(e),
            lines=[
                f"failure marker confirmed on screen for stage {spec.stage}",
                "the pane was closed; the next tick may retry with a fallback model",
            ],
        ) from e
    except (HerdrCliError, OSError) as e:
        _close_pane(client, pane_id)
        raise _Abort("stage_prompt_failed", detail=str(e)) from e

    ok, reason = _settle_outcome(status)
    if not ok:
        _close_pane(client, pane_id)
        raise _Abort(
            reason or "interrupted_unknown",
            detail=f"stage {spec.stage} settled {status!r}",
        )

    # Read the id the agent really reported and record it before the gate or any later
    # stage can depend on it (spec §5). This has to come AFTER the prompt: opencode
    # creates its session on the first prompt, so a just-started agent reports no
    # `agent_session` at all (measured on the Pi 2026-10-08; reading it before the
    # prompt failed every run's stage 1 as `session_id_unreadable`). `current_stage`
    # is committed together with the id so the watchdog's in-flight
    # `validate_stage_sessions` never sees a reached stage without its session, and a
    # same-RUN_ID relaunch redoes an interrupted stage rather than skipping it. An id
    # that cannot be read still aborts: writing a placeholder is exactly what made
    # G-17's gate necessary in the first place.
    try:
        session_id = client.agent_session_id(agent_name)
    except (HerdrCliError, OSError) as e:
        _close_pane(client, pane_id)
        raise _Abort("session_id_unreadable", detail=str(e)) from e
    if not session_id:
        _close_pane(client, pane_id)
        raise _Abort(
            "session_id_unreadable",
            detail=f"{agent_name} reported no agent_session.value",
        )

    state.commit(
        state_json,
        current_stage=spec.stage,
        stage_sessions={**state.stage_sessions, str(spec.stage): session_id},
    )
    return pane_id


def _open_pane(
    client: HerdrClient, state: _State, spec: StageSpec, agent_name: str
) -> str:
    """A fresh tab for one stage's agent.

    A tab in the shared workspace, not a worktree: the pipeline has exactly one worktree
    (created once by prepare) and every stage works in it. What makes the stages
    independent is the *session*, which is why `session_id` is per stage and the pane is
    disposable (G-17 plus G-16 together)."""
    try:
        return client.tab_create(cwd=str(state.worktree), label=agent_name)
    except (HerdrCliError, OSError) as e:
        raise _Abort("pane_creation_failed", detail=str(e)) from e


def _close_pane(client: HerdrClient, pane_id: str) -> None:
    """Close one stage's pane, best-effort. Never raises: a pane that will not close is
    something the watchdog's agent sweep has to handle later, and it must not convert a
    reportable stage failure into an unhandled exception on the way out."""
    try:
        client.pane_close(pane_id)
    except (HerdrCliError, OSError) as e:
        log.warning("could not close pane %s: %s", pane_id, e)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def _gate_for_stage(
    spec: StageSpec,
    state: _State,
    *,
    gh: GhClient,
    owner: str,
    repo: str,
    spec_text: str,
    runner: Callable[..., tuple[int, str, str]],
) -> GateVerdict:
    """The gate that must pass before stage `spec.stage + 1` starts.

    One place that says which gate belongs to which stage, because "gate N runs after
    stage N" is the pipeline's whole contract and it has been stated in prose in three
    different files so far."""
    if spec.stage == 3:
        # Gate 3's lint/pytest half stays prose (issue 034: Gate CI below is
        # authoritative for a PR). The mechanical half — every acceptance test named in
        # the spec exists under tests/ — is a command, so it runs in code.
        return gate3_test_names_present(spec_text, repo_path=state.worktree)
    if spec.stage == 4:
        head = run_stage_gate(
            "4",
            repo_path=state.worktree,
            gh=gh,
            owner=owner,
            repo=repo,
            branch=state.branch,
            pr=state.pr_number,
        )
        if not head.passed:
            return head
        ok, why = _issue_committed_done(state, runner)
        return GateVerdict(passed=ok, reason=why)
    if spec.stage == 5 or spec.stage == 6:
        return run_stage_gate(
            str(spec.stage),
            repo_path=state.worktree,
            gh=gh,
            owner=owner,
            repo=repo,
            pr=state.pr_number,
        )
    return run_stage_gate(
        str(spec.stage),
        repo_path=state.worktree,
        gh=gh,
        owner=owner,
        repo=repo,
        spec=state.spec,
    )


def run_pipeline(
    *,
    run_id: str,
    state_json: Path,
    report: Path,
    prompts_dir: Path,
    failure_markers: Sequence[str] = ("Free usage exceeded",),
    client: HerdrClient | None = None,
    gh: GhClient | None = None,
    heartbeat_dir: Path | None = None,
    runner: Callable[..., tuple[int, str, str]] | None = None,
    clock: Callable[[], float] = time.time,
) -> RunOutcome:
    """Run `STAGES` in order, gating each, and return what happened.

    Never raises. This is the process `systemd-run` supervises, and an escaped exception
    would leave a night with no report at all — precisely the silent death
    `pipeline_watchdog` exists to notice hours later. Every failure path writes the
    terminal report first, so "no report ⇒ look at systemd" stays a complete morning
    checklist rather than one with a hole in it.

    Resumable: `state.json`'s `current_stage` is the resume point and advances only after
    a gate passes, so re-running a half-finished night replays a stage rather than
    skipping one. Re-running a stage is recoverable; silently skipping one is not.
    """
    client = client or HerdrClient()
    gh_client = gh or RealGhClient()
    run = runner or _run_bounded
    heartbeat_dir = heartbeat_dir or Path("/tmp")

    def finish(
        outcome: Outcome,
        reason: str | None,
        *,
        error: str | None = None,
        stage: int | None = None,
        lines: Sequence[str] = (),
    ) -> RunOutcome:
        detail = [*lines]
        if stage is not None:
            detail.append(f"last stage: {stage}")
        if error:
            detail.append(f"error: {error}")
        detail.append(f"branch: {state.branch}")
        detail.append(f"worktree: {state.worktree}")
        write_terminal_report(
            report,
            run_id=run_id,
            outcome=_REPORT_OUTCOME.get((outcome, reason), outcome),
            lines=detail,
            title="pipeline-run report",
        )
        log.info("pipeline %s: %s (%s)", run_id, outcome, reason)
        return RunOutcome(
            outcome=outcome, run_id=run_id, reason=reason, error=error, stage=stage
        )

    def notify(title: str) -> None:
        """Fire the same notification channel routine-job failures use. Best-effort: a
        host with no notification daemon must not turn a bad night into a crash."""
        try:
            client.notification_show(title, sound="request")
        except (HerdrCliError, OSError) as e:
            log.warning("notification failed: %s", e)

    try:
        state = _State.read(state_json)
    except (OSError, ValueError, TypeError) as e:
        # Nothing to describe the branch with yet; the report is still the one artefact
        # that must exist, or the night looks like an interrupted run to `tick`.
        write_terminal_report(
            report,
            run_id=run_id,
            outcome="failed",
            lines=[f"error: {e}"],
            title="pipeline-run report",
        )
        return RunOutcome(
            outcome="failed", run_id=run_id, reason="state_unreadable", error=str(e)
        )

    run_id = state.run_id or run_id
    heartbeat = heartbeat_log_path(heartbeat_dir, run_id)
    spec_text = ""

    try:
        owner, repo = remote_owner_and_repo(state.worktree)
    except (RuntimeError, OSError) as e:
        return finish("failed", "origin_unresolvable", error=str(e))

    for spec in STAGES:
        if spec.stage <= state.current_stage:
            continue

        # The deadline is checked here — between stages, after the previous one settled,
        # never mid-stage. Killing a stage halfway through implementing is how a run ends
        # with a branch that neither builds nor has a PR.
        if state.deadline_epoch is not None and clock() >= state.deadline_epoch:
            notify(f"pipeline {run_id}: partial, deadline exceeded")
            return finish(
                "partial",
                "partial_deadline",
                stage=spec.stage - 1,
                lines=[
                    f"deadline_epoch: {state.deadline_epoch}",
                    f"skipped stage(s): {spec.stage}..{len(STAGES)}",
                ],
            )

        log.info("pipeline %s: stage %d", run_id, spec.stage)
        # Stage 1 is what writes the spec, so it can only be read from stage 2 on. Reading
        # it before stage 1 failed every run as `spec_unreadable` before any agent started
        # (2026-10-07); only gate 3 and stage 4's PR body consume it.
        if spec.stage > 1:
            try:
                spec_text = state.spec.read_text()
            except OSError as e:
                return finish(
                    "failed", "spec_unreadable", error=str(e), stage=spec.stage
                )

        pane_id: str | None = None
        try:
            if spec.model is None:
                pr_number = _open_pr(
                    state,
                    spec_text=spec_text,
                    gh=gh_client,
                    owner=owner,
                    repo=repo,
                    runner=run,
                )
                state = state.commit(state_json, pr_number=pr_number)
            else:
                pane_id = _run_agent_stage(
                    spec,
                    state,
                    run_id=run_id,
                    state_json=state_json,
                    report=report,
                    prompts_dir=prompts_dir,
                    failure_markers=failure_markers,
                    client=client,
                    heartbeat=heartbeat,
                    runner=run,
                    clock=clock,
                )
                # Re-read rather than carry a returned copy: the stage already wrote this
                # file, and `state.json` is also what the watchdog and `tick`'s reconcile
                # read while the run is in flight. Carrying a pre-stage copy forward would
                # let the final `current_stage` commit write back a `stage_sessions` map
                # with the stage's real id missing.
                state = _State.read(state_json)
        except _Abort as abort:
            notify(f"pipeline {run_id}: {abort.reason}")
            return finish(
                "failed",
                abort.reason,
                error=abort.detail,
                stage=spec.stage,
                lines=abort.lines,
            )
        except (OSError, ValueError, TypeError) as e:
            return finish("failed", "state_unreadable", error=str(e), stage=spec.stage)

        # A gate's `gh` call raises RuntimeError on any non-zero exit (expired token, rate
        # limit, network). Gate 4 catches its own; gates 5/6 do not, and an escape here
        # would break this function's never-raises contract and skip the terminal report
        # (spec §8) — so an unreachable gate is a failed gate, with the error as reason.
        try:
            verdict = _gate_for_stage(
                spec,
                state,
                gh=gh_client,
                owner=owner,
                repo=repo,
                spec_text=spec_text,
                runner=run,
            )
        except RuntimeError as e:
            verdict = GateVerdict(passed=False, reason=f"gate could not run: {e}")
        if not verdict.passed:
            if pane_id is not None:
                _close_pane(client, pane_id)
            notify(f"pipeline {run_id}: gate {spec.stage} failed")
            return finish(
                "failed",
                f"gate_{spec.stage}_failed",
                stage=spec.stage,
                lines=[
                    f"gate {spec.stage} failed: {verdict.reason}",
                    f"branch: {state.branch}",
                ],
            )

        if pane_id is not None:
            _close_pane(client, pane_id)

        # G-17, gates 1i/2i: the process-fidelity checks the prompt used to run as
        # `herdr agent list | jq` are now the in-code layout check, run against the ids
        # this same process recorded — strictly stronger, because a fabricated id is not
        # representable rather than merely detectable.
        issue = validate_stage_sessions(state.payload())
        if issue is not None:
            notify(f"pipeline {run_id}: session layout unverifiable")
            return finish(
                "failed",
                f"stage_sessions_invalid_{spec.stage}",
                stage=spec.stage,
                lines=[f"stage_sessions: {issue}"],
            )

        state = state.commit(state_json, current_stage=spec.stage)

        if spec.stage == 4:
            # Gate CI does not abort the run: a lint slip is exactly what stage 6 exists
            # to clean up, and the prompt is explicit that a CI failure rides into stage 6
            # as a must-fix item rather than ending the night.
            try:
                ci = run_stage_gate(
                    "ci",
                    repo_path=state.worktree,
                    gh=gh_client,
                    owner=owner,
                    repo=repo,
                    pr=state.pr_number,
                )
            except RuntimeError as e:
                # Same never-raises contract as the stage gate above; and since a CI
                # failure is non-fatal anyway, an unreachable CI gate must not be fatal.
                ci = GateVerdict(passed=False, reason=f"gate could not run: {e}")
            if not ci.passed:
                log.warning("pipeline %s: Gate CI: %s", run_id, ci.reason)

    # Stage 6's agent commits and pushes its own fixes, after stage 4's local re-sign —
    # so check origin once more, now that no stage will write to the branch again.
    lines = ["all stages green"]
    try:
        resigned = resign_unsigned_branch(
            state.worktree, branch=state.branch, base=PIPELINE_BASE
        )
    except (ResignError, OSError, subprocess.TimeoutExpired) as e:
        log.warning("pipeline %s: could not re-sign %s: %s", run_id, state.branch, e)
    else:
        if resigned:
            lines.append(f"re-signed {resigned} unsigned commit(s) on {state.branch}")
    return finish("ok", None, lines=lines)
