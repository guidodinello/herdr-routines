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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import tick
from herdr_routines.auto_fix import (
    PostedBody,
    ReviewPR,
    build_review_agent_name,
    build_review_prompt,
    review_marker,
)
from herdr_routines.config import RoutinesConfig, TmpHygieneConfig, load_config_dir
from herdr_routines.history import HistoryRecord, append, read_job
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

    def __init__(self, *, fail_first_read: set[str], **kw: Any) -> None:
        super().__init__(**kw)
        self.fail_first_read = fail_first_read

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
                    body=review_marker(JOB, key),
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
