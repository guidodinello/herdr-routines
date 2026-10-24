"""Issue 076: `kind: review` is a routine; its selection must not move.

`test_review_job_selection_unchanged_after_migration` was committed against the review-only
dispatch (`_review_cycle`) before the migration and its literals were never edited after,
so a green run on the migrated code is the proof that the shipped job selects the same PRs,
in the same order, with the same skip reasons, and writes the same per-target records.

It drives `run_tick` over the real `deploy/jobs.d/` directory (so `defaults.yaml` merges as it
does on the Pi), a fake `gh` and two real local git origins.
"""

from __future__ import annotations

import dataclasses
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import cli, tick
from herdr_routines.auto_fix import (
    PostedBody,
    ReviewPR,
    build_review_agent_name,
    build_review_prompt,
    review_marker,
)
from herdr_routines.config import RoutinesConfig, TmpHygieneConfig, load_config_dir
from herdr_routines.history import (
    STALE_MARGIN,
    HistoryRecord,
    append,
    last_scheduled_terminal_run,
    read_job,
)
from herdr_routines.runner import default_reports_dir, substitute_prompt
from tests.test_review_scan import ReviewGh, ReviewHerdrClient, _setup_repo

JOBS_DIR = Path(__file__).resolve().parent.parent / "deploy" / "jobs.d"
JOB = "review-my-prs"
RUN_ID = "review-my-prs-20261010T113000Z"
REGISTERED = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
# 08:30 America/Montevideo (defaults.yaml's timezone) is 11:30 UTC.
NOW = datetime(2026, 10, 10, 11, 35, tzinfo=UTC)


def _pr(
    repo: str,
    number: int,
    *,
    created_at: str,
    head_ref: str = "feature-x",
    author: str = "me",
    head_repo: str | None = None,
    mode: str = "author",
) -> ReviewPR:
    return ReviewPR(
        number=number,
        title=f"PR {number}",
        url=f"https://github.com/{repo}/pull/{number}",
        created_at=created_at,
        updated_at=created_at,
        head_ref=head_ref,
        author=author,
        repo=repo,
        head_repo=head_repo if head_repo is not None else repo,
        mode=mode,
    )


class _MigrationGh(ReviewGh):
    """`ReviewGh` with per-PR review threads: seeded bodies, one unreadable first read, and
    the review the fake agent posts. Fresh, so the post-run gate counts it."""

    def __init__(
        self, *, fail_first_read: set[str], job_name: str = JOB, **kw: Any
    ) -> None:
        super().__init__(**kw)
        self.fail_first_read = fail_first_read
        self.job_name = job_name

    def pr_review_bodies(
        self, *, owner: str, repo: str, number: int
    ) -> list[PostedBody] | None:
        key = f"{owner}/{repo}#{number}"
        self.bodies_calls.append((owner, repo, number))
        if key in self.fail_first_read:
            self.fail_first_read.discard(key)
            return None
        bodies = list(self.raw_bodies.get(key, []))
        if key in self.posted:
            bodies.append(
                PostedBody(
                    author=self.user,
                    body=review_marker(self.job_name, key),
                    created_at=datetime.now(UTC),
                )
            )
        return bodies


class _MigrationClient(ReviewHerdrClient):
    """Serves the review-only path and the `execute_run` path alike: the agent posts the
    marker for its target and writes the report the prompt names."""

    def __init__(self, gh: _MigrationGh) -> None:
        super().__init__()
        self.gh = gh
        self.cwds: list[str] = []
        self.names: list[str] = []
        self.on_prompt = self._agent

    def _agent(self, text: str) -> None:
        key = re.search(r"key=(\S+)", text)
        report = re.search(r"Report: (\S+)", text)
        assert key and report, text
        self.gh.posted.add(key.group(1))
        path = Path(report.group(1))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("## Outcome: ok\nreviewed\n")

    def tab_create(self, *, cwd: Any, label: Any = None) -> str:
        self.cwds.append(str(cwd))
        return super().tab_create(cwd=cwd, label=label)

    def agent_start(self, *, name: str, **kw: Any) -> None:
        self.names.append(name)
        super().agent_start(name=name, **kw)

    def settled_agent_pane(self, name: str) -> str | None:
        return None

    def agent_session_id(self, target: str) -> str | None:
        return None


def test_review_job_selection_unchanged_after_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state))
    history = state / "history.jsonl"
    cfg = load_config_dir(JOBS_DIR)
    loaded = cfg.job(JOB)
    assert loaded is not None, cfg.errors
    job = dataclasses.replace(loaded, enabled=True)
    repos = state / "repos"

    widgets_origin, _ = _setup_repo(tmp_path, "acme", "widgets", "feature-x", repos)
    gadgets_origin, gadgets_checkout = _setup_repo(
        tmp_path, "acme", "gadgets", "feature-g", repos
    )
    shutil.rmtree(gadgets_checkout)  # a repo never seen before: cloned lazily

    # Nine distinct PRs. Pre-dispatch verdicts, in the order the report lists them:
    #   widgets#2 author_mismatch   widgets#3 routine_owned_branch   widgets#4 fork_pr
    #   widgets#1 max_attempts_exceeded (seeded history)   widgets#5 review_already_posted
    #   widgets#9 over_cap (the cap is 3)
    # and three dispatched: gadgets#6 and widgets#7 tie on created_at (key order breaks it),
    # widgets#7's thread is unreadable on the first read (unverified, so still a candidate) and
    # gadgets#6 carries an untrusted copy of its marker, which does not suppress it.
    prs = (
        _pr("acme/widgets", 1, created_at="2026-09-29T00:00:00Z"),
        _pr("acme/widgets", 2, created_at="2026-09-29T00:00:00Z", author="bob"),
        _pr("acme/widgets", 3, created_at="2026-09-29T00:00:00Z", head_ref="auto/x"),
        _pr(
            "acme/widgets",
            4,
            created_at="2026-09-29T00:00:00Z",
            head_repo="fork/widgets",
        ),
        _pr("acme/widgets", 5, created_at="2026-09-30T00:00:00Z"),
        _pr(
            "acme/gadgets",
            6,
            created_at="2026-10-02T00:00:00Z",
            head_ref="feature-g",
            author="carol",
            mode="review_requested",
        ),
        _pr("acme/widgets", 7, created_at="2026-10-02T00:00:00Z"),
        # The same PR found by the second search mode: the first sighting wins.
        _pr(
            "acme/widgets",
            7,
            created_at="2026-10-02T00:00:00Z",
            mode="review_requested",
        ),
        _pr("acme/widgets", 8, created_at="2026-10-03T00:00:00Z"),
        _pr("acme/widgets", 9, created_at="2026-10-04T00:00:00Z"),
    )
    gh = _MigrationGh(
        fail_first_read={"acme/widgets#7"},
        prs=prs,
        repo_infos={
            ("acme", "widgets"): {
                "clone_url": str(widgets_origin),
                "default_branch": "main",
            },
            ("acme", "gadgets"): {
                "clone_url": str(gadgets_origin),
                "default_branch": "main",
            },
        },
        raw_bodies={
            "acme/widgets#5": [
                PostedBody("me", review_marker(JOB, "acme/widgets#5"), REGISTERED)
            ],
            # A marker copied by someone outside the trusted set does not suppress a review.
            "acme/gadgets#6": [
                PostedBody("mallory", review_marker(JOB, "acme/gadgets#6"), REGISTERED)
            ],
        },
    )
    client = _MigrationClient(gh)
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh)
    append(history, HistoryRecord(ts=REGISTERED, job=JOB, state="registered"))
    append(
        history,
        HistoryRecord(
            ts=datetime(2026, 10, 9, 11, 30, 5, tzinfo=UTC),
            job=JOB,
            state="done",
            run_id="review-my-prs-20261009T113000Z",
            extra={"target_key": "acme/widgets#1"},
        ),
    )

    outcome = tick.run_tick(
        RoutinesConfig(jobs=(job,), tmp_hygiene=TmpHygieneConfig(enabled=False)),
        history,
        client=client,  # type: ignore[arg-type]
        now=NOW,
    )

    assert outcome.any_job_failed is False, outcome.summaries
    assert gh.search_calls == [(("author", "review_requested"), 100)]

    # Same PRs, same order, same skip reasons.
    report = (default_reports_dir() / f"review-{RUN_ID}.md").read_text()
    rows = re.findall(r"^- (\S+#\d+): (\S+)$", report, re.MULTILINE)
    assert rows == [
        ("acme/widgets#2", "author_mismatch"),
        ("acme/widgets#3", "routine_owned_branch"),
        ("acme/widgets#4", "fork_pr"),
        ("acme/widgets#1", "max_attempts_exceeded"),
        ("acme/widgets#5", "review_already_posted"),
        ("acme/widgets#9", "over_cap"),
        ("acme/gadgets#6", "dispatched"),
        ("acme/widgets#7", "dispatched"),
        ("acme/widgets#8", "dispatched"),
    ]

    records = read_job(history, JOB)
    per_target = [
        r
        for r in records
        if r.run_id == RUN_ID
        and r.state != "running"
        and "target_key" in (r.extra or {})
    ]
    assert [
        ((r.extra or {})["target_key"], r.state, (r.extra or {}).get("reason"))
        for r in per_target
    ] == [
        ("acme/gadgets#6", "done", None),
        ("acme/widgets#7", "done", None),
        ("acme/widgets#8", "done", None),
    ]
    # `status` and the digest read these records: their shape is pinned too.
    assert all(
        set(r.extra or {})
        == {
            "target_key",
            "repo",
            "pr_number",
            "headRefName",
            "select_mode",
            "attempt",
            "agent",
            "pane_id",
            "report_path",
            "verified",
            "target",
        }
        for r in per_target
    )
    first = per_target[0].extra
    assert first is not None
    assert (first["repo"], first["pr_number"], first["headRefName"]) == (
        "acme/gadgets",
        6,
        "feature-g",
    )
    assert (first["select_mode"], first["attempt"], first["verified"]) == (
        "review_requested",
        0,
        True,
    )
    assert first["target"] == "pr"
    modes = {
        r.extra["target_key"]: r.extra["select_mode"] for r in per_target if r.extra
    }
    assert modes["acme/widgets#7"] == "author"

    # The aggregate is the occurrence's last record, in the shape `status` has always read.
    last = records[-1]
    assert (last.run_id, last.state) == (RUN_ID, "done")
    assert last.extra == {
        "gate": "passed",
        "target": "pr",
        "enumerated": 9,
        "candidates": 6,
        "dispatched": 3,
        "skipped": 6,
        "failed": 0,
        "repos_failed": 0,
    }

    # One agent per target, repos in name order, each in its own repo's managed checkout.
    assert [Path(c).relative_to(repos).as_posix() for c in client.cwds] == [
        "acme/gadgets/.worktrees/review-pr6",
        "acme/widgets/.worktrees/review-pr7",
        "acme/widgets/.worktrees/review-pr8",
    ]
    assert (gadgets_checkout / ".git").exists()
    assert client.names == [
        build_review_agent_name(JOB, key, RUN_ID)
        for key in ("acme/gadgets#6", "acme/widgets#7", "acme/widgets#8")
    ]
    # The engine review prompt, unchanged: URL, marker and report path all resolved.
    report_path = (
        default_reports_dir() / f"auto-review-{RUN_ID}-{_h6('acme/gadgets#6')}.md"
    )
    assert client.prompt_texts[0] == substitute_prompt(
        build_review_prompt(
            job_name=JOB,
            target_key="acme/gadgets#6",
            url="https://github.com/acme/gadgets/pull/6",
            report_path=str(report_path),
        ),
        report_path=report_path,
        job_name=JOB,
        run_id=RUN_ID,
    )


def _h6(key: str) -> str:
    from herdr_routines.auto_fix import review_target_hash

    return review_target_hash(JOB, key)


# -- the routine path: fan-out, per-target checkout, the kind as sugar (issue 076) --------

ROUTINE_NOW = datetime(2026, 10, 10, 8, 35, tzinfo=UTC)


class _SilentClient(_MigrationClient):
    """An agent that writes its report but posts no review for the targets in `silent`."""

    silent: frozenset[str] = frozenset()

    def _agent(self, text: str) -> None:
        key = re.search(r"key=(\S+)", text)
        if key and key.group(1) in self.silent:
            report = re.search(r"Report: (\S+)", text)
            assert report
            path = Path(report.group(1))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("## Outcome: ok\nnothing posted\n")
            return
        super()._agent(text)


@dataclass(slots=True)
class _World:
    repos: Path
    own: Path
    gh: _MigrationGh
    client: _SilentClient
    history: Path
    jobs_dir: Path


def _world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    prs: tuple[ReviewPR, ...],
    silent: frozenset[str] = frozenset(),
    job_yaml: str = "",
) -> _World:
    """A plain routine job (`pick: my_open_prs`, not `kind: review`) whose own repo is
    acme/widgets, checked out at `own`; acme/gadgets has no checkout until a target needs it.
    The job file is `job_yaml` under a fixed header."""
    state = tmp_path / "state"
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state))
    repos = state / "repos"
    own_base = tmp_path / "own"
    _, widgets = _setup_repo(tmp_path, "acme", "widgets", "feature-x", own_base)
    gadgets_origin, gadgets = _setup_repo(
        tmp_path, "acme", "gadgets", "feature-g", repos
    )
    shutil.rmtree(gadgets)
    jobs_dir = tmp_path / "jobs.d"
    jobs_dir.mkdir()
    (jobs_dir / "pr-sweeper.yaml").write_text(
        "name: pr-sweeper\n"
        "cron: '30 8 * * *'\n"
        "timezone: UTC\n"
        f"repo: {widgets}\n"
        "repository: https://github.com/acme/widgets.git\n"
        "pick: my_open_prs\n"
        "select: [author]\n"
        "verify: [review_posted]\n"
        "agent_kind: opencode\n"
        "prompt: |\n"
        "  Review $ROUTINE_TARGET ($ROUTINE_TARGET_URL)\n"
        "  Report: $ROUTINE_REPORT\n"
        "  End the review with <!-- herdr-routines:review job=pr-sweeper "
        "key=$ROUTINE_TARGET -->\n" + job_yaml
    )
    gh = _MigrationGh(
        fail_first_read=set(),
        job_name="pr-sweeper",
        prs=prs,
        repo_infos={
            ("acme", "gadgets"): {
                "clone_url": str(gadgets_origin),
                "default_branch": "main",
            },
        },
    )
    client = _SilentClient(gh)
    client.silent = silent
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh)
    history = state / "history.jsonl"
    append(history, HistoryRecord(ts=REGISTERED, job="pr-sweeper", state="registered"))
    return _World(repos, widgets, gh, client, history, jobs_dir)


def _load_world_job(world: _World) -> Any:
    cfg = load_config_dir(world.jobs_dir)
    job = cfg.job("pr-sweeper")
    assert job is not None, cfg.errors
    return job


def _tick_world(world: _World, job: Any) -> tick.TickOutcome:
    return tick.run_tick(
        RoutinesConfig(jobs=(job,), tmp_hygiene=TmpHygieneConfig(enabled=False)),
        world.history,
        client=world.client,  # type: ignore[arg-type]
        now=ROUTINE_NOW,
    )


def _widgets_prs(*numbers: int) -> tuple[ReviewPR, ...]:
    return tuple(
        _pr("acme/widgets", n, created_at=f"2026-10-0{n}T00:00:00Z") for n in numbers
    )


def test_routine_fans_out_over_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(
        tmp_path,
        monkeypatch,
        prs=_widgets_prs(1, 2, 3, 4),
        silent=frozenset({"acme/widgets#2"}),
        job_yaml="max_targets_per_run: 3\n",
    )
    job = _load_world_job(world)
    assert (job.kind, job.max_targets_per_run) == ("routine", 3)
    runs: list[tuple[str, Any]] = []
    real_execute_run = tick.execute_run

    def spy(job: Any, client: Any, **kw: Any) -> Any:
        runs.append((kw["target"].ref, kw["placement"]))
        return real_execute_run(job, client, **kw)

    monkeypatch.setattr(tick, "execute_run", spy)

    outcome = _tick_world(world, job)

    # Three agents in one occurrence, one per target, oldest first; the fourth is over the cap.
    assert [ref for ref, _ in runs] == [
        "acme/widgets#1",
        "acme/widgets#2",
        "acme/widgets#3",
    ]
    assert len(world.client.names) == 3
    # Each is verified on its own PR: the one that posted nothing fails, the others do not.
    per_target = {
        (r.extra or {})["target_key"]: r
        for r in read_job(world.history, "pr-sweeper")
        if r.state in ("done", "failed") and "target_key" in (r.extra or {})
    }
    assert {k: v.state for k, v in per_target.items()} == {
        "acme/widgets#1": "done",
        "acme/widgets#2": "failed",
        "acme/widgets#3": "done",
    }
    failed = per_target["acme/widgets#2"].extra
    assert failed is not None
    assert failed["reason"] == "verify_failed:review_posted"
    assert failed["error"] == "review_not_posted"
    assert failed["verified"] is False
    assert outcome.any_job_failed is True
    aggregate = read_job(world.history, "pr-sweeper")[-1]
    assert aggregate.extra is not None
    assert (
        aggregate.extra["dispatched"],
        aggregate.extra["failed"],
        aggregate.extra["skipped"],
    ) == (2, 1, 1)
    report = (
        default_reports_dir()
        / f"{aggregate.run_id and 'review-' + aggregate.run_id}.md"
    ).read_text()
    assert "acme/widgets#4: over_cap" in report
    # The job's own prompt got each target: its ref and its URL.
    assert (
        "Review acme/widgets#1 (https://github.com/acme/widgets/pull/1)"
        in (world.client.prompt_texts[0])
    )


def test_cross_repo_target_uses_managed_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(
        tmp_path,
        monkeypatch,
        prs=(
            _pr("acme/widgets", 1, created_at="2026-10-01T00:00:00Z"),
            _pr(
                "acme/gadgets",
                2,
                created_at="2026-10-02T00:00:00Z",
                head_ref="feature-g",
            ),
        ),
        job_yaml="max_targets_per_run: 2\n",
    )
    job = _load_world_job(world)

    outcome = _tick_world(world, job)

    assert outcome.any_job_failed is False, outcome.summaries
    # The job's own repo runs in the job's own checkout; the other repo's target runs in
    # its managed checkout, cloned on first use. Dispatch goes by repo name.
    managed = world.repos / "acme" / "gadgets"
    assert world.client.cwds == [
        str(managed / ".worktrees" / "review-pr2"),
        str(world.own / ".worktrees" / "review-pr1"),
    ]
    assert (managed / ".git").exists()
    assert not (world.repos / "acme" / "widgets").exists()


def test_review_kind_dispatches_through_routine_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shipped `kind: review` job loads as a routine with the review's parts...
    shipped = load_config_dir(JOBS_DIR).job(JOB)
    assert shipped is not None
    assert (shipped.kind, shipped.pick, shipped.verify) == (
        "routine",
        "my_open_prs",
        ("review_posted",),
    )
    assert shipped.max_targets_per_run == shipped.max_workers_per_tick == 3
    assert shipped.prompt is not None and "$ROUTINE_TARGET_URL" in shipped.prompt
    # ...and the review-only dispatch is gone.
    for gone in (
        "_process_review_job",
        "_review_cycle",
        "_dispatch_review_target",
        "_review_terminal_failure",
    ):
        assert not hasattr(tick, gone), gone

    # A scheduled tick and `herdr-routines run` both run it through `execute_run`, one call
    # per target, each placed in that target's worktree.
    state = tmp_path / "state"
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state))
    history = state / "history.jsonl"
    origin, widgets = _setup_repo(
        tmp_path, "acme", "widgets", "feature-x", state / "repos"
    )
    gh = _MigrationGh(
        fail_first_read=set(),
        prs=_widgets_prs(1, 2),
        repo_infos={
            ("acme", "widgets"): {"clone_url": str(origin), "default_branch": "main"}
        },
    )
    client = _MigrationClient(gh)
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh)
    seen: list[tuple[str, str, Path]] = []
    real_execute_run = tick.execute_run

    def spy(job: Any, client: Any, **kw: Any) -> Any:
        seen.append((job.kind, kw["target"].ref, kw["placement"].workdir))
        return real_execute_run(job, client, **kw)

    monkeypatch.setattr(tick, "execute_run", spy)
    job = dataclasses.replace(shipped, enabled=True)
    append(history, HistoryRecord(ts=REGISTERED, job=JOB, state="registered"))

    _tick = tick.run_tick(
        RoutinesConfig(jobs=(job,), tmp_hygiene=TmpHygieneConfig(enabled=False)),
        history,
        client=client,  # type: ignore[arg-type]
        now=NOW,
    )
    assert _tick.any_job_failed is False, _tick.summaries
    assert seen == [
        ("routine", "acme/widgets#1", widgets / ".worktrees" / "review-pr1"),
        ("routine", "acme/widgets#2", widgets / ".worktrees" / "review-pr2"),
    ]

    # Manual run: marker already posted on both, so nothing dispatches, but it gets as far
    # as the selector through the same path and says so in its history.
    monkeypatch.setattr(cli, "default_lock_path", lambda: tmp_path / "tick.lock")
    monkeypatch.setattr(cli, "HerdrClient", lambda: client)
    monkeypatch.setattr(
        cli,
        "_load_config_or_exit",
        lambda args: RoutinesConfig(jobs=(job,)),
    )
    seen.clear()
    assert cli.main(["run", JOB]) == 0
    assert seen == []  # #1 and #2 now carry the marker: review_already_posted
    manual = [
        r
        for r in read_job(history, JOB)
        if (r.extra or {}).get("trigger") == "manual" and r.state == "running"
    ]
    assert len(manual) == 1


def test_per_target_running_records_carry_the_occurrences_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each target's `running` record says what started the run, like the occurrence's own:
    otherwise `last_scheduled_terminal_run` would take a manual run's terminal for a
    scheduled one and swallow the next cron occurrence."""
    world = _world(tmp_path, monkeypatch, prs=_widgets_prs(1))
    job = dataclasses.replace(_load_world_job(world), request=True)
    monkeypatch.setattr(cli, "default_lock_path", lambda: tmp_path / "tick.lock")
    monkeypatch.setattr(cli, "HerdrClient", lambda: world.client)
    monkeypatch.setattr(
        cli, "_load_config_or_exit", lambda args: RoutinesConfig(jobs=(job,))
    )

    _tick_world(world, job)  # the cron occurrence reviews #1
    # Manual and request runs of a newer PR each (the marker and budget guards hide #1).
    for number, extra_args in ((5, []), (6, ["--trigger", "request"])):
        world.gh.prs = (
            *_widgets_prs(1),
            _pr("acme/widgets", number, created_at=f"2026-10-0{number}T00:00:00Z"),
        )
        assert cli.main(["run", "pr-sweeper", *extra_args]) == 0

    targets = [
        (r.run_id, (r.extra or {})["target_key"], (r.extra or {}).get("trigger"))
        for r in read_job(world.history, "pr-sweeper")
        if r.state == "running" and "target_key" in (r.extra or {})
    ]
    assert [(key, trigger) for _, key, trigger in targets] == [
        ("acme/widgets#1", "cron"),
        ("acme/widgets#5", "manual"),
        ("acme/widgets#6", "request"),
    ]
    # Neither off-schedule run counts as the job's last scheduled one.
    last = last_scheduled_terminal_run(world.history, "pr-sweeper")
    assert last is not None and last.run_id == targets[0][0]


# -- the attempt budget under the routine path's failure reasons --------------------------


@pytest.mark.parametrize(
    ("state", "reason", "counts"),
    [
        ("done", None, True),
        ("failed", "verify_failed:review_posted", True),
        ("failed", "review_not_posted", True),  # history from the review-only dispatch
        ("failed", "review_verification_failed", True),
        (
            "failed",
            "no_report",
            True,
        ),  # the agent may have posted and skipped the report
        ("failed", "blocked", True),
        ("failed", "unsettled_status_working", True),
        ("interrupted_unknown", "unsettled_status_unknown", True),
        ("failed", "worktree_failed", False),
        ("failed", "clone_failed", False),
        ("failed", "pane_creation_failed", False),
        ("failed", "agent_start_failed", False),
        ("failed", "agent_not_interactive", False),
        ("failed", "agent_prompt_failed", False),
        ("failed", "quota_exhausted", False),
        ("skipped", "agent_live", False),
        ("running", None, False),
    ],
)
def test_attempt_budget_counts_a_target_only_when_the_agent_could_have_posted(
    tmp_path: Path, state: Any, reason: str | None, counts: bool
) -> None:
    from herdr_routines.auto_fix import attempt_count_for_target

    history = tmp_path / "history.jsonl"
    extra: dict[str, object] = {"target_key": "acme/widgets#1"}
    if reason is not None:
        extra["reason"] = reason
    append(
        history,
        HistoryRecord(ts=NOW, job=JOB, state=state, run_id="r", extra=extra),
    )
    assert attempt_count_for_target(history, JOB, "acme/widgets#1") == int(counts)
    assert attempt_count_for_target(history, JOB, "acme/widgets#2") == 0


# -- `kind: review` as config sugar ---------------------------------------------------------


def _loads(tmp_path: Path, files: dict[str, str]) -> Any:
    jobs_dir = tmp_path / "jobs.d"
    jobs_dir.mkdir(parents=True)
    for name, body in files.items():
        (jobs_dir / name).write_text(body)
    return load_config_dir(jobs_dir)


REVIEW_FILE = "name: r\ncron: '0 3 * * *'\nkind: review\nselect: [author]\n"


def test_kind_review_sugar_resolves_before_defaults_can_leak_in(tmp_path: Path) -> None:
    cfg = _loads(
        tmp_path,
        {
            "defaults.yaml": "workspace: root\nverify: [report_wellformed]\n"
            "pick: oldest_unreviewed_pr\n",
            "r.yaml": REVIEW_FILE,
        },
    )
    job = cfg.job("r")
    assert job is not None, cfg.errors
    assert job.max_attempts_per_target == 1  # not the generic 3
    assert job.max_targets_per_run == 3  # `max_workers_per_tick`'s default
    assert job.verify == ("review_posted",)
    assert job.pick == "my_open_prs"
    assert job.workspace == "worktree"  # each review closes its own pane
    assert (job.ignored_verify, job.ignored_pick) == ((), None)

    capped = _loads(
        tmp_path / "capped",
        {
            "r.yaml": REVIEW_FILE
            + "max_workers_per_tick: 7\nmax_attempts_per_target: 2\n"
        },
    ).job("r")
    assert capped is not None
    assert (capped.max_targets_per_run, capped.max_attempts_per_target) == (7, 2)


def test_several_targets_need_a_selector_that_can_return_several(
    tmp_path: Path,
) -> None:
    body = (
        f"name: a\ncron: '0 3 * * *'\nrepo: {tmp_path}\n"
        "prompt: write $ROUTINE_REPORT\nmax_targets_per_run: 2\n"
    )
    cfg = _loads(tmp_path / "one", {"a.yaml": body + "pick: oldest_unreviewed_pr\n"})
    assert cfg.jobs == ()
    assert any("multi-target selector" in e for e in cfg.errors)

    cfg = _loads(
        tmp_path / "many",
        {
            "a.yaml": body
            + "pick: my_open_prs\nselect: [author]\nverify: [review_posted]\n"
        },
    )
    job = cfg.job("a")
    assert job is not None, cfg.errors
    assert job.max_targets_per_run == 2
    assert job.max_attempts_per_target == 1  # a review is not repeated

    # The default is one target, whatever the selector.
    plain = _loads(
        tmp_path / "plain", {"a.yaml": body.replace("max_targets_per_run: 2\n", "")}
    )
    only = plain.job("a")
    assert only is not None and only.max_targets_per_run == 1


def test_target_url_placeholder_does_not_clobber_the_target_placeholder() -> None:
    target = tick.VerifyTarget("acme", "widgets", 7)
    text = substitute_prompt(
        "$ROUTINE_TARGET_URL | $ROUTINE_TARGET",
        report_path=Path("/r.md"),
        job_name="j",
        run_id="r",
        target=target,
    )
    assert text == "https://github.com/acme/widgets/pull/7 | acme/widgets#7"


def test_running_record_window_scales_with_the_target_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fan-out holds one `running` record for up to `max_targets_per_run` agents in turn,
    so a record older than one `timeout_ms` is still live while the cap's window is open."""
    world = _world(
        tmp_path, monkeypatch, prs=_widgets_prs(1), job_yaml="max_targets_per_run: 3\n"
    )
    job = dataclasses.replace(_load_world_job(world), timeout_ms=60_000)
    started = ROUTINE_NOW - STALE_MARGIN - timedelta(seconds=120)
    append(
        world.history,
        HistoryRecord(ts=started, job="pr-sweeper", state="running", run_id="old"),
    )

    summary, failed = tick._process_job(
        job,
        world.history,
        client=world.client,  # type: ignore[arg-type]
        now=ROUTINE_NOW,
    )

    assert (summary, failed) == ("pr-sweeper: skipped (already running)", False)
    assert world.client.cwds == []


def test_event_trigger_is_rejected_on_any_job_whose_repo_is_the_managed_base_dir(
    tmp_path: Path,
) -> None:
    """075's rule keyed on having no checkout, not on the kind's name: `kind: review` says it
    in its own words, a plain `repo:` that is the managed base dir says it generically."""
    from herdr_routines.repos import default_repos_dir

    event = "trigger:\n  event:\n    github_event: {query: 'prs:labels'}\n"
    review = _loads(
        tmp_path / "review",
        {"r.yaml": f"name: r\nkind: review\nselect: [author]\n{event}"},
    )
    assert any(
        "'trigger.event' is not supported on kind: review" in e for e in review.errors
    )
    plain = _loads(
        tmp_path / "plain",
        {
            "p.yaml": f"name: p\nrepo: {default_repos_dir()}\nprompt: x $ROUTINE_REPORT\n"
            f"{event}"
        },
    )
    assert any("whose repo is the managed base dir" in e for e in plain.errors)
