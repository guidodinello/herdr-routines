"""Post-run verify gates (issue 071), driven through the real entry points: `execute_run`,
the tick's routine path and `herdr-routines run`, not only the gate functions."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import cli
from herdr_routines.auto_fix import PostedBody, RealGhClient
from herdr_routines.config import VALID_VERIFY_GATES, RoutinesConfig
from herdr_routines.history import read_job
from herdr_routines.runner import execute_run
from herdr_routines.tick import run_tick
from herdr_routines.verify import VERIFY_GATES, parse_declared_target
from tests.test_runner import ScriptedClient, make_job
from tests.test_tick import FakeFullClient
from tests.test_tick import make_job as make_tick_job

GOOD_REPORT = "# Review\n\nTarget: acme/widgets#839\n\nReviewed with /code-review.\n"
REVIEW_BODY = "### Code review\n\nNo blocking issues.\n"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("herdr_routines.runner.ensure_repo", lambda job: job.repo)
    monkeypatch.setattr("herdr_routines.tick.ensure_repo", lambda job: job.repo)


class FakeVerifyGh:
    """The slice of GhClient the verify gates touch."""

    def __init__(
        self,
        bodies: list[PostedBody] | None = None,
        *,
        bodies_fail: bool = False,
        raise_on: str | None = None,
    ) -> None:
        self.bodies = bodies or []
        self.bodies_fail = bodies_fail
        self.raise_on = raise_on
        self.bodies_calls: list[tuple[str, str, int]] = []

    def api_user(self) -> str:
        if self.raise_on == "api_user":
            raise RuntimeError("gh auth failed")
        return "me"

    def pr_review_bodies(
        self, *, owner: str, repo: str, number: int
    ) -> list[PostedBody] | None:
        self.bodies_calls.append((owner, repo, number))
        if self.raise_on == "pr_review_bodies":
            raise RuntimeError("boom")
        return None if self.bodies_fail else list(self.bodies)


def _posted(*, age: timedelta, author: str = "me") -> PostedBody:
    return PostedBody(
        author=author, body=REVIEW_BODY, created_at=datetime.now(UTC) - age
    )


def _run(
    tmp_path: Path,
    gh: FakeVerifyGh,
    *,
    report: str = GOOD_REPORT,
    verify: tuple[Any, ...] = ("review_posted",),
):
    run_id = "a-verify"
    job = replace(make_job(tmp_path), verify=verify)
    reports = tmp_path / "state" / "reports"
    client = ScriptedClient(
        write_report_at=reports / f"{run_id}.md", report_content=report
    )
    return execute_run(job, client, run_id=run_id, gh=gh)  # type: ignore[arg-type]


def test_routine_review_posted_gate_fails_when_no_review_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #839 incident: a good-looking report, no review on the PR. Through the tick and
    through `herdr-routines run` (the manual path the incident took)."""
    gh = FakeVerifyGh()
    monkeypatch.setattr("herdr_routines.runner.RealGhClient", lambda: gh)

    class ReportWithTarget(FakeFullClient):
        def agent_prompt_wait(self, *, target, text, timeout_ms):
            super().agent_prompt_wait(target=target, text=text, timeout_ms=timeout_ms)
            Path(text.rsplit(maxsplit=1)[-1]).write_text(GOOD_REPORT)
            return self._settle_status

    job = replace(make_tick_job(tmp_path), verify=("review_posted",))
    history = tmp_path / "state" / "history.jsonl"
    client = ReportWithTarget()
    config = RoutinesConfig(jobs=(job,))
    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history, client=client, now=t0)  # type: ignore[arg-type]
    outcome = run_tick(config, history, client=client, now=t0 + timedelta(minutes=1))  # type: ignore[arg-type]
    assert outcome.summaries == ("a: failed (verify_failed:review_posted)",)
    assert outcome.any_job_failed is True
    terminal = read_job(history, "a")[-1]
    assert terminal.state == "failed"
    assert terminal.extra is not None
    assert terminal.extra["reason"] == "verify_failed:review_posted"
    assert terminal.extra["verify"] == [
        {"gate": "review_posted", "passed": False, "reason": "review_not_posted"}
    ]
    assert [n[0] for n in client.notifications] == ["herdr-routines: a failed"]

    # The manual path.
    config_path = tmp_path / "jobs.yaml"
    config_path.write_text(
        f"version: 1\njobs:\n  - name: a\n    cron: '0 3 * * *'\n    repo: {tmp_path}\n"
        "    prompt: write the report to $ROUTINE_REPORT\n    verify: [review_posted]\n"
    )
    monkeypatch.setattr(cli, "default_config_path", lambda: config_path)
    monkeypatch.setattr(cli, "default_lock_path", lambda: tmp_path / "tick.lock")
    reports = tmp_path / "state" / "reports"
    scripted = ScriptedClient(
        write_report_at=reports / "a-manual.md", report_content=GOOD_REPORT
    )
    monkeypatch.setattr(cli, "HerdrClient", lambda: scripted)
    assert cli.main(["run", "a", "--run-id", "a-manual"]) == 1


def test_routine_review_posted_gate_passes_when_the_review_landed(
    tmp_path: Path,
) -> None:
    outcome = _run(tmp_path, FakeVerifyGh([_posted(age=timedelta(minutes=-1))]))
    assert outcome.state == "done"
    assert [(r.gate, r.passed) for r in outcome.verify] == [("review_posted", True)]


def test_review_posted_ignores_reviews_older_than_the_run(tmp_path: Path) -> None:
    old = _run(tmp_path, FakeVerifyGh([_posted(age=timedelta(hours=1))]))
    assert old.state == "failed"
    assert old.reason == "verify_failed:review_posted"
    # An untrusted author or a missing timestamp never counts either.
    stranger = _run(
        tmp_path, FakeVerifyGh([_posted(age=timedelta(minutes=-1), author="drive-by")])
    )
    assert stranger.state == "failed"
    undated = _run(tmp_path, FakeVerifyGh([PostedBody(author="me", body=REVIEW_BODY)]))
    assert undated.state == "failed"


@pytest.mark.parametrize(
    "report",
    [
        "# Review\n\nnothing declared\n",
        "# Review\n\nTarget: #839\n",
        "# Review\n\nTarget: acme/widgets\n",
        "# Review\n\nTarget: a/b#1\nTarget: c/d#2\n",
        "Reviewed #839 via code-review.\\nTarget: acme/widgets#839\\n",
    ],
)
def test_review_posted_requires_declared_target(tmp_path: Path, report: str) -> None:
    gh = FakeVerifyGh([_posted(age=timedelta(minutes=-1))])
    outcome = _run(tmp_path, gh, report=report)
    assert outcome.state == "failed"
    assert outcome.reason == "verify_failed:review_posted"
    assert outcome.verify[0].reason == "target_undeclared"
    assert gh.bodies_calls == []


@pytest.mark.parametrize(
    "gh",
    [
        FakeVerifyGh(bodies_fail=True),
        FakeVerifyGh(raise_on="pr_review_bodies"),
        FakeVerifyGh(raise_on="api_user"),
    ],
    ids=["bodies-none", "bodies-raises", "viewer-raises"],
)
def test_verify_probe_failure_fails_the_run(tmp_path: Path, gh: FakeVerifyGh) -> None:
    outcome = _run(tmp_path, gh)
    assert outcome.state == "failed"
    assert outcome.reason == "verify_failed:review_posted"
    assert outcome.verify[0].reason == "review_verification_failed"


def test_verify_probe_timeout_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RealGhClient` maps a `gh` timeout to exit 124; the gate reads that as unverified."""

    def fake_run(self: RealGhClient, argv: list[str]) -> tuple[int, str, str]:
        if argv[:3] == ["gh", "pr", "view"]:
            return 124, "", "gh timed out"
        return 0, "me\n", ""

    monkeypatch.setattr(RealGhClient, "_run", fake_run)
    outcome = _run(tmp_path, RealGhClient())  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.verify[0].reason == "review_verification_failed"


def test_report_wellformed_rejects_literal_escapes(tmp_path: Path) -> None:
    garbage = (
        "Reviewed #839 via code-review skill (PR review with inline comments; "
        "pending-review label swapped to reviewed)\\n\\nDone.\\n"
    )
    outcome = _run(
        tmp_path, FakeVerifyGh(), report=garbage, verify=("report_wellformed",)
    )
    assert outcome.state == "failed"
    assert outcome.reason == "verify_failed:report_wellformed"
    assert outcome.verify[0].reason == "report_literal_escapes"

    # A report that merely mentions `\n` in real Markdown is fine.
    discussing = "# Report\n\nThe splitter regex matches \\n between records.\n"
    ok = _run(
        tmp_path, FakeVerifyGh(), report=discussing, verify=("report_wellformed",)
    )
    assert ok.state == "done"


def test_every_listed_gate_runs_and_the_first_failure_names_the_reason(
    tmp_path: Path,
) -> None:
    gh = FakeVerifyGh()
    outcome = _run(
        tmp_path,
        gh,
        report="no target, literal \\n escapes\n",
        verify=("review_posted", "report_wellformed"),
    )
    assert outcome.reason == "verify_failed:review_posted"
    assert [(r.gate, r.passed) for r in outcome.verify] == [
        ("review_posted", False),
        ("report_wellformed", False),
    ]


def test_a_missing_report_keeps_no_report_and_runs_no_gates(tmp_path: Path) -> None:
    job = replace(make_job(tmp_path), verify=("report_wellformed",))
    outcome = execute_run(job, ScriptedClient(), run_id="a-none", gh=FakeVerifyGh())  # type: ignore[arg-type]
    assert outcome.reason == "no_report"
    assert outcome.verify == ()


def test_gate_registry_matches_the_config_names() -> None:
    assert set(VERIFY_GATES) == VALID_VERIFY_GATES


def test_real_gh_client_reads_review_and_comment_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    payload = {
        "reviews": [
            {
                "author": {"login": "me"},
                "body": "r",
                "submittedAt": "2026-10-24T01:04:56Z",
            },
            {"author": {"login": "me"}, "body": "pending", "submittedAt": None},
        ],
        "comments": [
            {
                "author": {"login": "me"},
                "body": "c",
                "createdAt": "2026-10-24T02:00:00Z",
            }
        ],
    }
    monkeypatch.setattr(
        RealGhClient, "_run", lambda self, argv: (0, json.dumps(payload), "")
    )
    bodies = RealGhClient().pr_review_bodies(owner="o", repo="r", number=1)
    assert bodies is not None
    assert [b.created_at for b in bodies] == [
        datetime(2026, 10, 24, 1, 4, 56, tzinfo=UTC),
        None,
        datetime(2026, 10, 24, 2, 0, 0, tzinfo=UTC),
    ]


def test_parse_declared_target() -> None:
    assert parse_declared_target("x\nTarget: acme/widgets#839\n") is not None
    assert parse_declared_target("Target: acme/widgets#839 and more") is None
    assert parse_declared_target("Target: a/b#1\nTarget: a/b#1\n") is not None


# -- issue 070: the fix-worker gates ---------------------------------------------------


def _fix_worker_ctx(tmp_path: Path, gh: Any, **overrides: Any):
    from herdr_routines.verify import VerifyContext, VerifyTarget

    fields: dict[str, Any] = {
        "job": make_job(tmp_path),
        "run_id": "r",
        "started_at": datetime(2026, 10, 21, 5, 0, tzinfo=UTC),
        "report_path": tmp_path / "report.md",
        "gh": gh,
        "trusted_review_authors": (),
        "target": VerifyTarget("acme", "widgets", 211),
        "trusted_authors": frozenset({"testuser"}),
        "worktree_path": tmp_path,
        "dispatch_head_sha": "sha-before",
        "given_thread_ids": ("t1",),
    }
    fields.update(overrides)
    return VerifyContext(**fields)


def test_worktree_clean_gate_reads_the_worktree(tmp_path: Path) -> None:
    import subprocess

    from herdr_routines.verify import worktree_clean

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    gh = FakeVerifyGh()

    assert worktree_clean(_fix_worker_ctx(tmp_path, gh, worktree_path=repo)).passed
    (repo / "leftover.txt").write_text("x")
    verdict = worktree_clean(_fix_worker_ctx(tmp_path, gh, worktree_path=repo))
    assert (verdict.passed, verdict.reason) == (
        False,
        "worker_left_uncommitted_changes",
    )

    # Fails closed: no path, and a directory git cannot read, are never a pass.
    assert worktree_clean(_fix_worker_ctx(tmp_path, gh, worktree_path=None)).reason == (
        "worktree_unknown"
    )
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    unreadable = worktree_clean(_fix_worker_ctx(tmp_path, gh, worktree_path=plain))
    assert (unreadable.passed, unreadable.reason) == (
        False,
        "worker_verification_failed",
    )


class _FixWorkerGh:
    """pr_view / graphql / api_user for `pushed_or_replied`."""

    def __init__(
        self,
        *,
        head: object = "sha-before",
        comments: dict[str, list[dict[str, Any]]] | None = None,
        raise_on: str | None = None,
    ) -> None:
        self.head = head
        self.comments = comments or {}
        self.raise_on = raise_on

    def api_user(self) -> str:
        return "testuser"

    def pr_view(self, *, owner: str, repo: str, number: int) -> dict[str, object]:
        if self.raise_on == "pr_view":
            raise RuntimeError("boom")
        return {} if self.head is None else {"headRefOid": self.head}

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        if self.raise_on == "graphql":
            raise RuntimeError("boom")
        nodes = [
            {
                "id": tid,
                "isResolved": True,
                "comments": {"totalCount": len(c), "nodes": c},
            }
            for tid, c in self.comments.items()
        ]
        return {
            "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}
        }


def _reply(login: str, at: str) -> dict[str, Any]:
    return {"author": {"login": login}, "body": "r", "createdAt": at}


_ROOT = _reply("testuser", "2026-10-21T04:00:00Z")


@pytest.mark.parametrize(
    ("gh", "overrides", "expected"),
    [
        pytest.param(_FixWorkerGh(head="sha-after"), {}, (True, None), id="head-moved"),
        pytest.param(
            _FixWorkerGh(),
            {"given_thread_ids": ()},
            (False, "worker_pushed_nothing"),
            id="empty-given-set-needs-a-push",
        ),
        pytest.param(
            _FixWorkerGh(),
            {"given_thread_ids": None},
            (False, "worker_pushed_nothing"),
            id="thread-fetch-failed-needs-a-push",
        ),
        pytest.param(
            _FixWorkerGh(
                comments={"t1": [_ROOT, _reply("testuser", "2026-10-21T05:00:00Z")]}
            ),
            {},
            (True, None),
            id="fresh-trusted-reply",
        ),
        pytest.param(
            _FixWorkerGh(
                comments={"t1": [_ROOT, _reply("testuser", "2026-10-21T04:59:59Z")]}
            ),
            {},
            (False, "worker_pushed_nothing"),
            id="reply-before-dispatch",
        ),
        pytest.param(
            _FixWorkerGh(
                comments={"t1": [_ROOT, _reply("stranger", "2026-10-21T05:00:00Z")]}
            ),
            {},
            (False, "worker_pushed_nothing"),
            id="untrusted-reply",
        ),
        pytest.param(
            _FixWorkerGh(comments={}),
            {},
            (False, "worker_pushed_nothing"),
            id="given-thread-gone-from-the-pr",
        ),
        pytest.param(
            _FixWorkerGh(raise_on="pr_view"),
            {},
            (False, "worker_verification_failed"),
            id="pr-view-error",
        ),
        pytest.param(
            _FixWorkerGh(raise_on="graphql"),
            {},
            (False, "worker_verification_failed"),
            id="graphql-error",
        ),
        pytest.param(
            _FixWorkerGh(head=None),
            {},
            (False, "worker_verification_failed"),
            id="head-unreadable",
        ),
        pytest.param(
            _FixWorkerGh(head="sha-after"),
            {"dispatch_head_sha": None},
            (False, "worker_verification_failed"),
            id="no-dispatch-head",
        ),
        pytest.param(
            _FixWorkerGh(head="sha-after"),
            {"target": None},
            (False, "worker_verification_failed"),
            id="no-target",
        ),
    ],
)
def test_pushed_or_replied_gate(
    tmp_path: Path,
    gh: _FixWorkerGh,
    overrides: dict[str, Any],
    expected: tuple[bool, str | None],
) -> None:
    from herdr_routines.verify import pushed_or_replied

    verdict = pushed_or_replied(_fix_worker_ctx(tmp_path, gh, **overrides))
    assert (verdict.passed, verdict.reason) == expected
