"""Tests for `herdr-routines pipeline-run` (issue 056 phases B and C) — the stage loop
that replaces the orchestrator LLM session, plus stage 4 in code.

Two things are deliberately *not* faked here, because faking them would make these
tests assert the fake rather than the code:

- **git**. Gates 1, 2 and 4 and stage 4's `git push -u` all run against a real
  bare-origin fixture (`_init_bare_git_repo`, borrowed from `test_sync_repo.py` /
  `test_pipeline_prepare.py`), through the same injectable `runner` seam
  `auto_fix.run_checks` already exposes. `rg` runs for real the same way, so
  `gate3_test_names`'s fixed-string search is exercised for real too.
- **the wait loop**. The fake `HerdrClient` honours
  `agent_prompt_wait_with_watchdog`'s documented contract (feed `on_poll` the screen,
  treat a non-None return as a confirmed marker and raise `PromptWatchdogKilled`),
  which means the marker scan, the two-poll stability gate and the heartbeat write
  under test are all the real code from `wait_loop.py` / `pipeline_run.py`.

What *is* faked: the Herdr server (agents, panes, session ids) and `gh` (PR create /
PR view). Neither can be exercised for real from a test, and both have a seam already.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import tick
from herdr_routines.auto_fix import GhClient
from herdr_routines.herdr import HerdrCliError, PromptWatchdogKilled
from herdr_routines.pipeline_run import RunOutcome, run_pipeline
from herdr_routines.pipeline_stages import STAGES
from herdr_routines.pipeline_watchdog import (
    heartbeat_log_path,
    validate_stage_sessions,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

RUN_ID = "20261001T050000Z"
BRANCH = f"auto/pipeline-{RUN_ID}"
SESSION_IDS = {
    1: "ses_aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    2: "ses_bbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    3: "ses_cccccccccccccccccccccccccccc",
    5: "ses_dddddddddddddddddddddddddddd",
}
REUSED_SESSION_ID = SESSION_IDS[3]

SPEC_BODY = """# spec v2 — do the thing

## problem

The thing is not done. Stage workers need a spec that says what to build.

## approach

Build it in `pipeline_run.py`.

## Acceptance criteria

1. The thing works. blocking. confidence: high
   Test: test_the_thing_works
2. A second thing works. non-blocking. confidence: medium
   Test: test_the_second_thing_works

## files touched

- `src/herdr_routines/pipeline_run.py`

## risks

1. None.

## Changelog v1→v2

v1 had no acceptance criteria; v2 adds two, each ending `Test: <name>`.
"""

# The gate-3 mechanical check only asks "does this test name exist under tests/".
# A fixture that satisfies it needs a real tests/ dir with a real file naming it.
TEST_NAMES = ("test_the_thing_works", "test_the_second_thing_works")


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc


def _init_bare_git_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    tmp = path.parent / f".bare-init-{path.name}"
    subprocess.run(
        ["git", "clone", str(path), str(tmp)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(tmp, "config", "user.email", "test@test.com")
    _git(tmp, "config", "user.name", "Test")
    (tmp / "README.md").write_text("init\n")
    _git(tmp, "add", "README.md")
    _git(tmp, "commit", "-m", "init")
    _git(tmp, "push", "-u", "origin", "HEAD")
    shutil.rmtree(tmp)


def _git_runner(argv: list[str], *, timeout_s: float | None) -> tuple[int, str, str]:
    """The injectable `runner` seam, running real `git`/`rg`. `gh` never reaches here:
    every `gh` call goes through the `GhClient` fake."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    return proc.returncode, proc.stdout, proc.stderr


class FakeHerdr:
    """A HerdrClient-shaped fake for the stage loop.

    `session_ids` is keyed by stage number; stage 6 reuses stage 3's, which the
    caller expresses by mapping 6 to the same value. `screens` scripts the visible
    screen per stage: a list is fed to `on_poll` one entry per poll and then repeats
    its last entry, so the two-consecutive-poll stability gate is exercised for real.
    """

    def __init__(
        self,
        session_ids: dict[int, str],
        *,
        settle_status: str = "idle",
        screens: dict[int, list[str]] | None = None,
        polls_per_stage: int = 3,
    ) -> None:
        self.session_ids = session_ids
        self.settle_status = settle_status
        self.screens = screens or {}
        self.polls_per_stage = polls_per_stage
        self.started: list[dict[str, Any]] = []
        self.prompts: list[tuple[str, str]] = []
        self.closed_panes: list[str] = []
        self.notifications: list[str] = []
        self.polls = 0
        self._next_pane = 0
        self._resumed: dict[str, str] = {}
        self._prompted: set[str] = set()

    # -- panes -----------------------------------------------------------------

    def tab_create(self, *, cwd: str, label: str | None = None) -> str:
        self._next_pane += 1
        return f"w1:p{self._next_pane}"

    # -- agents ----------------------------------------------------------------

    def agent_start(
        self,
        *,
        name: str,
        kind: str,
        pane_id: str,
        start_timeout_ms: int,
        model: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.started.append(
            {
                "name": name,
                "kind": kind,
                "pane_id": pane_id,
                "model": model,
                "session_id": session_id,
            }
        )
        if session_id is not None:
            # A resumed agent reports the session it resumed: verified against live herdr
            # 2026-08-25, `-s <session_id>` resumes rather than forks and
            # `agent_session.value` on the new agent matches the original. Modelling that
            # here is what makes stage 6's recorded id stage 3's id, which is the whole
            # point of G-16's close-then-resume.
            self._resumed[name] = session_id

    def agent_session_id(self, target: str) -> str | None:
        # opencode creates its session on the first prompt: a just-started agent reports
        # no `agent_session` at all (measured on the Pi 2026-10-08). A fake that answered
        # right after `agent_start` is what hid a stage loop reading the id too early.
        if target not in self._prompted:
            return None
        if target in self._resumed:
            return self._resumed[target]
        for stage, session_id in self.session_ids.items():
            if target.lower() == f"pl-{stage}-{RUN_ID}".lower():
                return session_id
        return None

    def agent_prompt_wait_with_watchdog(
        self,
        *,
        target: str,
        text: str,
        timeout_ms: int,
        poll_interval_s: float = 30.0,
        on_poll: Callable[[str], str | None] | None = None,
    ) -> str:
        self.prompts.append((target, text))
        self._prompted.add(target)
        stage = int(target.split("-")[1])
        screens = self.screens.get(stage, [""])
        for index in range(self.polls_per_stage):
            screen = screens[min(index, len(screens) - 1)]
            self.polls += 1
            if on_poll is None:
                continue
            marker = on_poll(screen)
            if marker is not None:
                raise PromptWatchdogKilled(
                    f"failure marker matched; prompt child terminated: {marker!r}",
                    marker=marker,
                    screen_text=screen,
                )
        return self.settle_status

    def pane_close(self, pane_id: str) -> None:
        self.closed_panes.append(pane_id)

    def notification_show(
        self, title: str, *, body: str | None = None, sound: str = "none"
    ) -> None:
        self.notifications.append(title)


class FakeGh:
    """GhClient-shaped fake: only the two calls stage 4 and gate 4/5/6 make."""

    def __init__(self, *, pr_number: int = 42, head_ref: str = BRANCH) -> None:
        self.pr_number = pr_number
        self.head_ref = head_ref
        self.created: list[dict[str, Any]] = []
        self.viewed: list[int] = []
        # Stage 5's model is what posts the review; the fake cannot run a model, so it
        # starts from the review a tier-structured run leaves behind. Gate 5 then checks
        # that structure rather than its absence.
        self.reviews: list[dict[str, Any]] = [
            {
                "body": (
                    "### blocking\n\n"
                    "[blocking] `pipeline_run` never re-reads state.json between stages\n\n"
                    "### non-blocking\n\n"
                    "[non-blocking] consider naming the heartbeat directory\n"
                )
            }
        ]
        self.threads: dict[str, Any] = {"data": {}}
        # Set to make gate 5's inline-comment fetch fail the way an expired token does.
        self.inline_comments_error: str | None = None

    def api_user(self) -> str:
        return "tester"

    def pr_create(
        self, *, owner: str, repo: str, branch: str, title: str, body: str
    ) -> int:
        self.created.append(
            {
                "owner": owner,
                "repo": repo,
                "branch": branch,
                "title": title,
                "body": body,
            }
        )
        return self.pr_number

    def pr_view(self, *, owner: str, repo: str, number: int) -> dict[str, object]:
        self.viewed.append(number)
        return {
            "state": "OPEN",
            "url": f"https://github.com/{owner}/{repo}/pull/{number}",
            "headRefName": self.head_ref,
            "statusCheckRollup": [],
            "reviews": self.reviews,
        }

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        return self.threads

    # The rest of the GhClient protocol: nothing on the stage-loop path calls these.

    def pr_list(
        self, *, owner: str, repo: str, state: str, limit: int
    ) -> list[dict[str, object]]:
        raise NotImplementedError

    def commit_check_runs(
        self, *, owner: str, repo: str, sha: str
    ) -> list[dict[str, object]]:
        raise NotImplementedError

    def pr_review_comments(
        self, *, owner: str, repo: str, number: int
    ) -> list[dict[str, object]]:
        if self.inline_comments_error is not None:
            raise RuntimeError(self.inline_comments_error)
        return []


class PipelineFixture:
    """A prepared run: real bare origin, real branch, real committed spec + issue."""

    def __init__(self, tmp_path: Path, *, issue_status: str = "done") -> None:
        self.tmp_path = tmp_path
        self.origin = tmp_path / "origin.git"
        _init_bare_git_repo(self.origin)

        self.worktree = tmp_path / "wt"
        subprocess.run(
            ["git", "clone", str(self.origin), str(self.worktree)],
            capture_output=True,
            text=True,
            check=True,
        )
        _git(self.worktree, "config", "user.email", "test@test.com")
        _git(self.worktree, "config", "user.name", "Test")
        _git(self.worktree, "checkout", "-b", BRANCH)

        spec_path = self.worktree / "docs" / "pipeline" / "runs" / RUN_ID / "spec.md"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text(SPEC_BODY)
        (self.worktree / "tests").mkdir()
        (self.worktree / "tests" / "test_thing.py").write_text(
            "\n".join(f"def {name}() -> None:\n    pass\n" for name in TEST_NAMES)
        )
        self.issue_rel = "docs/process/issues/056-orchestrator-stage-loop-in-code.md"
        issue_path = self.worktree / self.issue_rel
        issue_path.parent.mkdir(parents=True, exist_ok=True)
        issue_path.write_text(
            f'---\nid: "056"\ntitle: "stage loop"\nstatus: {issue_status}\n---\n'
        )
        _git(self.worktree, "add", "-A")
        _git(self.worktree, "commit", "-m", "spec: v2 acceptance")

        self.report = tmp_path / "reports" / f"pipeline-{RUN_ID}.md"
        self.state_json = self.worktree / "state.json"
        self.heartbeat_dir = tmp_path / "heartbeat"
        self.heartbeat_dir.mkdir()
        self.prompts_dir = REPO_ROOT / "docs" / "pipeline" / "stages"
        self.write_state()

    def write_state(self) -> None:
        self.state_json.write_text(
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "current_stage": 0,
                    "pr_number": None,
                    "shared_worktree": str(self.worktree),
                    "branch": BRANCH,
                    "shared_workspace": "w-shared",
                    "deadline_epoch": int(time.time()) + 36000,
                    "feature_source": self.issue_rel,
                    "artifact_paths": {
                        "spec": str(
                            self.worktree
                            / "docs"
                            / "pipeline"
                            / "runs"
                            / RUN_ID
                            / "spec.md"
                        ),
                        "report": str(self.report),
                    },
                    "stage_sessions": {},
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_json.read_text())


@pytest.fixture
def prepared(tmp_path: Path) -> PipelineFixture:
    return PipelineFixture(tmp_path)


def _run(
    prepared: PipelineFixture,
    *,
    client: FakeHerdr | None = None,
    gh: FakeGh | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[RunOutcome, FakeHerdr, FakeGh]:
    client = client or FakeHerdr(SESSION_IDS)
    gh = gh or FakeGh()
    outcome = run_pipeline(
        run_id=RUN_ID,
        state_json=prepared.state_json,
        report=prepared.report,
        prompts_dir=prepared.prompts_dir,
        failure_markers=("Free usage exceeded",),
        client=client,  # type: ignore[arg-type]
        gh=gh,
        heartbeat_dir=prepared.heartbeat_dir,
        runner=_git_runner,
        clock=clock or time.time,
    )
    return outcome, client, gh


def _client(**kwargs: Any) -> FakeHerdr:
    return FakeHerdr(SESSION_IDS, **kwargs)


# ---------------------------------------------------------------------------
# 1. stage_sessions recorded by code, never taken from model output
# ---------------------------------------------------------------------------


def test_pipeline_run_records_real_stage_sessions(prepared: PipelineFixture) -> None:
    outcome, client, _gh = _run(prepared)

    assert outcome.outcome == "ok"
    sessions = prepared.state()["stage_sessions"]
    # Every id is the one the started agent really reported, not a value any model
    # supplied: the fake only answers `agent_session_id` for a name it was started under.
    assert {int(k) for k in sessions} == {1, 2, 3, 5, 6}
    for stage, session_id in sessions.items():
        if int(stage) == 6:
            assert session_id == REUSED_SESSION_ID  # G-16 declared reuse
        else:
            assert session_id == SESSION_IDS[int(stage)]
    assert all(re.match(r"^ses_[A-Za-z0-9]{20,}$", v) for v in sessions.values())

    # The write happens from the started agent's id, so a stage whose session id could
    # not be read must abort rather than record a guess.
    orphan = FakeHerdr(SESSION_IDS)
    orphan.session_ids = {}
    # Its own starting state: the run above left `current_stage` at 6, and a loop that
    # correctly skips finished stages would otherwise have nothing left to do here.
    prepared.write_state()
    result = run_pipeline(
        run_id=RUN_ID,
        state_json=prepared.state_json,
        report=prepared.report,
        prompts_dir=prepared.prompts_dir,
        client=orphan,  # type: ignore[arg-type]
        gh=FakeGh(),
        heartbeat_dir=prepared.heartbeat_dir,
        runner=_git_runner,
    )
    assert result.outcome == "failed"
    assert prepared.state()["stage_sessions"] == {}

    # And the stage prompts never ask a model to write the map (spec v2 AC 1).
    for _target, text in client.prompts:
        assert "stage_sessions" not in text


class SpecWritingHerdr(FakeHerdr):
    """A FakeHerdr whose stage-1 agent writes the spec, as the real stage-1 prompt
    instructs — for a run that starts the way `pipeline-prepare` leaves it: with a
    `spec.md` path in `state.json` but no file at it yet."""

    def __init__(self, spec_path: Path) -> None:
        super().__init__(SESSION_IDS)
        self.spec_path = spec_path

    def agent_prompt_wait_with_watchdog(self, *, target: str, **kwargs: Any) -> str:
        if int(target.split("-")[1]) == 1:
            self.spec_path.parent.mkdir(parents=True, exist_ok=True)
            self.spec_path.write_text(SPEC_BODY)
        return super().agent_prompt_wait_with_watchdog(target=target, **kwargs)


def test_pipeline_run_starts_stage_1_before_the_spec_exists(
    prepared: PipelineFixture,
) -> None:
    """Stage 1 writes the spec, so the loop must not require it before stage 1. It did,
    and every nightly run died as `spec_unreadable` before any agent was started."""
    spec_path = Path(prepared.state()["artifact_paths"]["spec"])
    spec_path.unlink()

    outcome, client, _gh = _run(prepared, client=SpecWritingHerdr(spec_path))

    assert outcome.outcome == "ok", outcome
    assert client.started[0]["name"] == f"pl-1-{RUN_ID}".lower()


# ---------------------------------------------------------------------------
# 2. a failing gate aborts before the next stage, with a report naming it
# ---------------------------------------------------------------------------


def test_pipeline_run_aborts_on_gate_failure(prepared: PipelineFixture) -> None:
    client = _client()
    gh = FakeGh()
    gh.head_ref = "auto/pipeline-some-other-run"  # gate 4's right-head check fails

    outcome, client, _gh = _run(prepared, client=client, gh=gh)

    assert outcome.outcome == "failed"
    assert outcome.reason == "gate_4_failed"
    # Stages 5 and 6 never started — the abort happened at stage 4's gate.
    started = [call["name"] for call in client.started]
    assert started == [f"pl-{n}-{RUN_ID}".lower() for n in (1, 2, 3)]
    # The report names the gate that failed and why, near the top where tick greps it.
    report_text = prepared.report.read_text()
    assert "## Outcome: failed" in report_text
    assert "gate 4" in report_text
    assert "auto/pipeline-some-other-run" in report_text
    assert client.notifications  # the human is told, not just the report


def test_pipeline_run_turns_a_gh_error_in_a_gate_into_a_failed_report(
    prepared: PipelineFixture,
) -> None:
    """`run_pipeline` never raises: a `RuntimeError` from gate 5's `gh` call (expired
    token, rate limit) must become a failed outcome with the terminal report written,
    not an escaped exception that leaves only the launcher's generic backstop stub."""
    gh = FakeGh()
    gh.inline_comments_error = "gh api pulls/42/comments failed: HTTP 401 (auth)"

    outcome, _, _ = _run(prepared, gh=gh)

    assert outcome.outcome == "failed"
    assert outcome.reason == "gate_5_failed"
    report_text = prepared.report.read_text()
    assert "## Outcome: failed" in report_text
    assert "HTTP 401" in report_text


# ---------------------------------------------------------------------------
# 3. the heartbeat advances on every poll, from inside the wait loop
# ---------------------------------------------------------------------------


def test_pipeline_run_writes_heartbeat(prepared: PipelineFixture) -> None:
    client = _client(polls_per_stage=4)

    outcome, used_client, _gh = _run(prepared, client=client)

    assert outcome.outcome == "ok"
    heartbeat = heartbeat_log_path(prepared.heartbeat_dir, RUN_ID)
    assert heartbeat.exists()
    lines = [line for line in heartbeat.read_text().splitlines() if line.strip()]
    # One line per poll of every stage, at the path `pipeline_watchdog.is_stalled`
    # reads — five agent stages x 4 polls, written by code, not by a model.
    assert len(lines) == used_client.polls == 5 * 4
    assert all("stage" in line for line in lines)


# ---------------------------------------------------------------------------
# 4. past the deadline the in-flight stage finishes, the rest are skipped
# ---------------------------------------------------------------------------


def test_pipeline_run_partial_on_deadline(prepared: PipelineFixture) -> None:
    deadline = 1_790_597_715
    prepared.write_state()
    state = prepared.state()
    state["deadline_epoch"] = deadline
    prepared.state_json.write_text(json.dumps(state, indent=2, sort_keys=True))

    client = _client()
    prompted: list[str] = []

    def clock() -> float:
        # The deadline has not passed while stage 1 is still to be started, and has
        # passed the moment any stage has been prompted — so the check fires between
        # stage 1 and stage 2, exactly where the spec puts it.
        return deadline - 1 if not prompted else deadline + 1

    original_prompt = client.agent_prompt_wait_with_watchdog

    def recording_prompt(**kwargs: Any) -> str:
        prompted.append(kwargs["target"])
        return original_prompt(**kwargs)

    client.agent_prompt_wait_with_watchdog = recording_prompt  # type: ignore[method-assign]

    outcome, _used_client, _gh = _run(prepared, client=client, clock=clock)

    assert outcome.outcome == "partial"
    assert outcome.reason == "partial_deadline"
    # Stage 1 was allowed to finish rather than being killed mid-stage.
    assert prompted == [f"pl-1-{RUN_ID}".lower()]
    assert prepared.state()["current_stage"] == 1
    report_text = prepared.report.read_text()
    assert "## Outcome: partial (deadline exceeded)" in report_text


# ---------------------------------------------------------------------------
# 5. a quota marker ends the run as failed (quota_exhausted)
# ---------------------------------------------------------------------------


def test_pipeline_run_quota_marker_fast_fails(prepared: PipelineFixture) -> None:
    client = _client(
        screens={2: ["Free usage exceeded, subscribe to Go [retrying in 11h 59m]"]}
    )

    outcome, client, _gh = _run(prepared, client=client)

    assert outcome.outcome == "failed"
    assert outcome.reason == "quota_exhausted"
    # The exact reason string `tick._process_pipeline_job` gates its fallback_model
    # retry on, produced from the run's own report.
    report_text = prepared.report.read_text()
    assert "## Outcome: failed (quota_exhausted)" in report_text
    assert tick._classify_pipeline_outcome(report_text) == (
        "failed",
        "quota_exhausted",
    )
    # It fast-failed rather than waiting stage 2 out: stage 3 was never started.
    assert [call["name"] for call in client.started] == [
        f"pl-{n}-{RUN_ID}".lower() for n in (1, 2)
    ]
    # The wedged pane is reaped, so the next tick does not see a live worker.
    assert client.closed_panes


# ---------------------------------------------------------------------------
# 6. stage 4 opens the PR with no agent at all
# ---------------------------------------------------------------------------


def test_pipeline_stage4_opens_pr_without_agent(prepared: PipelineFixture) -> None:
    outcome, client, gh = _run(prepared)

    assert outcome.outcome == "ok"
    # No pl-4 agent exists at any point — this stage is git + gh and nothing else.
    assert not [call for call in client.started if call["name"].startswith("pl-4")]
    assert len(gh.created) == 1
    created = gh.created[0]
    assert created["branch"] == BRANCH
    assert created["title"] == "spec v2 — do the thing"
    assert "## problem" in created["body"]
    for name in TEST_NAMES:
        assert name in created["body"]
    assert "Closes #056" in created["body"]
    # pr_number is written to state.json *before* gate 4 runs, so the gate can read it.
    assert prepared.state()["pr_number"] == gh.pr_number
    # Gate 4's two conditions both held: the right head branch, and the issue file
    # committed as `status: done` on the branch.
    assert gh.head_ref == BRANCH
    assert _git(prepared.worktree, "show", f"HEAD:{prepared.issue_rel}").stdout.count(
        "status: done"
    )


# ---------------------------------------------------------------------------
# 7. the success report's outcome vocabulary is the one tick reads as `done`
# ---------------------------------------------------------------------------


def test_pipeline_run_writes_outcome_ok_that_tick_reads_as_done(
    prepared: PipelineFixture,
) -> None:
    outcome, _client, _gh = _run(prepared)

    assert outcome.outcome == "ok"
    report_text = prepared.report.read_text()
    assert "## Outcome: ok" in report_text
    # The single contract that matters: tick's classifier must read this as `done`.
    # A `done`-flavoured vocabulary would file every green night as interrupted.
    assert tick._classify_pipeline_outcome(report_text) == ("done", None)
    # ...and with it, the G-17 independence downgrade must not fire on a run whose
    # stage_sessions the run itself recorded.
    assert validate_stage_sessions(prepared.state()) is None


# ---------------------------------------------------------------------------
# 8. a finished run's stage_sessions satisfy the declared layout
# ---------------------------------------------------------------------------


def test_pipeline_run_stage_sessions_satisfy_declared_layout(
    prepared: PipelineFixture,
) -> None:
    outcome, _client, _gh = _run(prepared)

    assert outcome.outcome == "ok"
    state = prepared.state()
    sessions = {int(k): v for k, v in state["stage_sessions"].items()}

    # Every stage 1..6 is covered by the run; stage 4 starts no agent so it records
    # nothing, and stage 6's declared reuse (G-16) records stage 3's id verbatim.
    assert set(sessions) == {1, 2, 3, 5, 6}
    assert sessions[6] == sessions[3] == REUSED_SESSION_ID
    # The four independent stages are distinct real ids; the reused one does not count
    # toward the distinct total, which is what `validate_stage_sessions` enforces.
    independent = [sessions[stage] for stage in (1, 2, 3, 5)]
    assert len(set(independent)) == 4
    assert all(re.match(r"^ses_[A-Za-z0-9]{20,}$", v) for v in sessions.values())
    # current_stage reached the end of the workflow.
    assert state["current_stage"] == len(STAGES)
    # And the G-17 gate itself agrees, with the declared layout as the default.
    assert validate_stage_sessions(state) is None


# ---------------------------------------------------------------------------
# Structure: the workflow table and the prompt files it names cannot disagree
# ---------------------------------------------------------------------------


def test_stages_table_matches_the_prompt_files_on_disk() -> None:
    prompts_dir = REPO_ROOT / "docs" / "pipeline" / "stages"

    assert [s.stage for s in STAGES] == [1, 2, 3, 4, 5, 6]
    for spec in STAGES:
        if spec.model is None:
            assert spec.prompt_file is None
            assert spec.isolation == "none"
            continue
        assert spec.prompt_file is not None
        assert (prompts_dir / spec.prompt_file).is_file(), spec.prompt_file


def test_stages_declare_the_documented_models_and_timeouts() -> None:
    by_stage = {s.stage: s for s in STAGES}

    assert by_stage[1].model == "opencode/muse-spark-1.3-contributor-free"
    assert by_stage[2].model == "opencode/muse-spark-1.3-contributor-free"
    assert by_stage[3].model == "opencode/big-pickle"
    assert by_stage[4].model is None
    assert by_stage[5].model == "opencode/nemotron-3-ultra-free"
    # The reviewer is never the author's model (independent review).
    assert by_stage[5].model != by_stage[3].model
    assert by_stage[6].reuses_stage == 3
    assert all(s.start_timeout_ms == 120_000 for s in STAGES)
    assert by_stage[1].timeout_ms == by_stage[2].timeout_ms == 3_600_000
    assert by_stage[3].timeout_ms == 5_400_000
    assert by_stage[5].timeout_ms == 3_600_000


def test_prompt_substitution_uses_only_state_json_values(
    prepared: PipelineFixture,
) -> None:
    _outcome, client, _gh = _run(prepared)

    texts = {target: text for target, text in client.prompts}
    stage1 = texts[f"pl-1-{RUN_ID}".lower()]
    # Every documented placeholder is resolved, and none is left literal.
    assert "$RUN_ID" not in stage1
    assert "$WT" not in stage1
    assert RUN_ID in stage1
    assert str(prepared.worktree) in stage1
    # Stage 3 must keep the issue-close instruction (spec v2 §3: the flip rides the PR).
    stage3 = texts[f"pl-3-{RUN_ID}".lower()]
    assert prepared.issue_rel in stage3
    assert "status: done" in stage3
    # Stage 6 is prompted with the PR number, and resumes stage 3's session.
    stage6 = texts[f"pl-6-{RUN_ID}".lower()]
    assert "$PR_NUMBER" not in stage6
    assert "42" in stage6
    resumed = [call for call in client.started if call["name"].startswith("pl-6")]
    assert resumed and resumed[0]["session_id"] == REUSED_SESSION_ID


def test_pipeline_run_never_raises_on_a_broken_state_file(tmp_path: Path) -> None:
    """`run_pipeline` is the process the launcher unit supervises; an escaped exception
    would leave a night with no report at all, which is the failure mode the watchdog
    exists to catch after the fact."""
    missing = tmp_path / "nope" / "state.json"
    outcome = run_pipeline(
        run_id=RUN_ID,
        state_json=missing,
        report=tmp_path / "reports" / f"pipeline-{RUN_ID}.md",
        prompts_dir=REPO_ROOT / "docs" / "pipeline" / "stages",
        client=_client(),  # type: ignore[arg-type]
        gh=FakeGh(),
        runner=_git_runner,
    )

    assert outcome.outcome == "failed"
    assert outcome.reason == "state_unreadable"
    assert (
        "## Outcome: failed"
        in (tmp_path / "reports" / f"pipeline-{RUN_ID}.md").read_text()
    )


def test_pipeline_run_classifies_an_unreadable_herdr_start_as_failed(
    prepared: PipelineFixture,
) -> None:
    class BrokenHerdr(FakeHerdr):
        def agent_start(self, **kwargs: Any) -> None:
            raise HerdrCliError("herdr server down", exit_code=1)

    outcome, _client, _gh = _run(prepared, client=BrokenHerdr(SESSION_IDS))

    assert outcome.outcome == "failed"
    assert outcome.reason == "agent_start_failed"
    assert "## Outcome: failed" in prepared.report.read_text()


def test_gate_client_protocol_is_satisfied_by_the_real_client() -> None:
    """`run_pipeline` takes a `GhClient`, and stage 4 needs a `pr create` on it — a
    protocol without that method would only fail at 03:00, in production."""
    assert hasattr(GhClient, "pr_create")


def test_pipeline_run_preserves_preflight_state_keys(prepared: PipelineFixture) -> None:
    """`write_state_json` replaces the file wholesale, so every `commit` this loop makes
    has to write back phase A's whole record — not just the fields it manages.

    `artifact_paths.report` is the one that bites: phase A records the run's terminal
    report path, `ps._resolve_report_path` trusts it first, and
    `pipeline_watchdog._candidate_report_paths` reads it to tell a finished run from a
    stalled one. Dropping it would leave the watchdog guessing at two fallback filenames
    while the run is in flight, and the fields this loop does manage would look perfect
    the whole time."""
    client = _client(polls_per_stage=2)

    outcome, _used_client, _gh = _run(prepared, client=client)

    assert outcome.outcome == "ok"
    state = json.loads(prepared.state_json.read_text())
    assert state["artifact_paths"]["report"] == str(prepared.report)
    assert state["artifact_paths"]["spec"].endswith("spec.md")
    # And a key this loop knows nothing about survives too, so adding one to the
    # pre-flight does not require adding a field to _State for it to be kept.
    state["unrelated_future_key"] = {"kept": True}
    prepared.state_json.write_text(json.dumps(state))
    _run(prepared, client=_client(polls_per_stage=2))
    reread = json.loads(prepared.state_json.read_text())
    assert reread["unrelated_future_key"] == {"kept": True}


# ---------------------------------------------------------------------------
# Re-signing: an agent's `git -c commit.gpgsign=false` commit must not reach the PR
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_pipeline_pushes_signed_commits_when_the_clone_signs(
    prepared: PipelineFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixture's stage commits are unsigned, like PR #140's. With the clone set up
    per docs/pipeline/setup.md, what reaches origin must be signed."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    key = prepared.tmp_path / "signing"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
    )
    for k, v in {
        "gpg.format": "ssh",
        "user.signingkey": f"{key}.pub",
        "commit.gpgsign": "true",
    }.items():
        _git(prepared.worktree, "config", k, v)

    outcome, _client, _gh = _run(prepared)

    assert outcome.outcome == "ok", outcome
    _git(prepared.worktree, "fetch", "--quiet", "origin")
    pushed = _git(
        prepared.worktree, "rev-list", f"origin/main..origin/{BRANCH}"
    ).stdout.split()
    assert pushed
    for sha in pushed:
        header = _git(prepared.worktree, "cat-file", "commit", sha).stdout.split(
            "\n\n"
        )[0]
        assert "\ngpgsig" in header, f"{sha} reached origin unsigned"
    # Signed before the first push (stage 4), not repaired by force-push at the end:
    # the end-of-run check found nothing left to do.
    assert "re-signed" not in prepared.report.read_text()
