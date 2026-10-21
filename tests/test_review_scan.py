"""Acceptance tests for issue 055: ``kind: review`` cross-repo @me PR review.

19 tests, one per acceptance criterion in
``docs/pipeline/runs/20261011T050000Z/spec.md``, named exactly as the spec requires.
Phase A exercises the pure selector (``review_scan``); Phase B drives the tick's
``_review_cycle`` against real local git repos and an in-memory ``gh``.
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from herdr_routines import tick
from herdr_routines.auto_fix import (
    REVIEW_SEARCH_QUALIFIERS,
    PostedBody,
    ReviewPR,
    ReviewSearch,
    attempt_count_for_target,
    build_review_agent_name,
    build_review_agent_prefix,
    build_review_prompt,
    review_marker,
)
from herdr_routines.config import (
    _DEFAULTS_ALLOWED_KEYS,
    VALID_JOB_KINDS,
    ConfigError,
    Job,
    load_config,
    load_config_dir,
)
from herdr_routines.history import HistoryRecord, append, read_job
from herdr_routines.review_scan import (
    Candidate,
    apply_attempt_budget,
    apply_marker_guard,
    cap_candidates,
    render_review_report,
    scan_review_targets,
)

NOW = datetime(2026, 10, 10, 8, 30, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _load_jobs(tmp_path: Path, *jobs: dict[str, Any]) -> Any:
    """Legacy single-file loader: it *raises* ConfigError on a bad job, unlike
    load_config_dir which collects per-file errors."""
    path = tmp_path / "jobs.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "jobs": list(jobs)}))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return load_config(path)


def _base_review_job_dict() -> dict[str, Any]:
    return {
        "name": "review-my-prs",
        "cron": "30 8 * * *",
        "kind": "review",
        "select": ["author", "review_requested"],
    }


def _jobs_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    jobs_dir = tmp_path / "jobs.d"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (jobs_dir / name).write_text(content)
    return jobs_dir


def _review_job(tmp_path: Path, **over: Any) -> Job:
    job = Job(
        name="review-my-prs",
        enabled=True,
        cron="30 8 * * *",
        repo=tmp_path / "repos",
        workspace="root",
        base="main",
        agent_kind="opencode",
        model=None,
        prompt="",
        timeout_ms=60_000,
        start_timeout_ms=5_000,
        catch_up_minutes=720,
        timezone="UTC",
        on_missed="log",
        kind="review",
        select=("author",),
        include_auto_branches=False,
        max_workers_per_tick=5,
        max_attempts_per_target=1,
        notify_policy="on-finding",
    )
    return dataclasses.replace(job, **over)


def _pr(
    number: int,
    *,
    repo: str = "acme/widgets",
    head_ref: str = "feature-x",
    author: str = "me",
    head_repo: str | None = None,
    created_at: str = "2026-10-01T00:00:00Z",
    updated_at: str = "2026-10-01T00:00:00Z",
    mode: str = "author",
) -> ReviewPR:
    return ReviewPR(
        number=number,
        title=f"PR {number}",
        url=f"https://example.invalid/{repo}/{number}",
        created_at=created_at,
        updated_at=updated_at,
        head_ref=head_ref,
        author=author,
        repo=repo,
        head_repo=head_repo if head_repo is not None else repo,
        mode=mode,
    )


class ReviewGh:
    """In-memory GhClient for the review search trio. Fully implements the GhClient
    protocol (including the pre-existing methods the review path never touches) so it
    can be handed to ``GhClient``-annotated parameters without a type ignore."""

    def __init__(
        self,
        *,
        user: str = "me",
        prs: tuple[ReviewPR, ...] = (),
        repo_infos: dict[tuple[str, str], dict[str, object] | None] | None = None,
        raw_bodies: dict[str, list[PostedBody]] | None = None,
        search_fail: bool = False,
        bodies_fail: bool = False,
        truncated_modes: tuple[str, ...] = (),
    ) -> None:
        self.user = user
        self.prs = prs
        self.repo_infos = repo_infos or {}
        self.raw_bodies = raw_bodies or {}
        self.truncated_modes = truncated_modes
        self.search_fail = search_fail
        self.bodies_fail = bodies_fail
        self.posted: set[str] = set()
        self.search_calls: list[tuple[tuple[str, ...], int]] = []
        self.repo_info_calls: list[tuple[str, str]] = []
        self.bodies_calls: list[tuple[str, str, int]] = []

    def api_user(self) -> str:
        return self.user

    def pr_list(
        self, *, owner: str, repo: str, state: str, limit: int
    ) -> list[dict[str, object]]:
        return []

    def pr_create(
        self, *, owner: str, repo: str, branch: str, title: str, body: str
    ) -> int:
        return 1

    def pr_view(self, *, owner: str, repo: str, number: int) -> dict[str, object]:
        return {}

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        return {"data": {}}

    def commit_check_runs(
        self, *, owner: str, repo: str, sha: str
    ) -> list[dict[str, object]]:
        return []

    def pr_review_comments(
        self, *, owner: str, repo: str, number: int
    ) -> list[dict[str, object]]:
        return []

    def search_prs(self, modes: Any, limit: int = 100) -> ReviewSearch | None:
        self.search_calls.append((tuple(modes), limit))
        if self.search_fail:
            return None
        return ReviewSearch(prs=self.prs, truncated_modes=self.truncated_modes)

    def pr_review_bodies(
        self, *, owner: str, repo: str, number: int
    ) -> list[PostedBody] | None:
        self.bodies_calls.append((owner, repo, number))
        if self.bodies_fail:
            return None
        key = f"{owner}/{repo}#{number}"
        if key in self.raw_bodies:
            return list(self.raw_bodies[key])
        # The review worker posts with the owner's gh account, i.e. `user`.
        marker = review_marker("review-my-prs", key)
        return [PostedBody(author=self.user, body=marker)] if key in self.posted else []

    def repo_info(self, *, owner: str, repo: str) -> dict[str, object] | None:
        self.repo_info_calls.append((owner, repo))
        return self.repo_infos.get((owner, repo))


class ReviewHerdrClient:
    """Enough of HerdrClient for ``_dispatch_review_target`` to run end-to-end.
    ``on_prompt`` lets a test stand in for the agent posting its review."""

    def __init__(
        self,
        *,
        ready: bool = True,
        settle_status: str = "idle",
        on_prompt: Any = None,
        statuses: dict[str, str] | None = None,
    ) -> None:
        self.ready = ready
        self.settle_status = settle_status
        self.on_prompt = on_prompt
        self.statuses = statuses or {}
        self.tabs: list[tuple[str, str | None]] = []
        self.started: list[str] = []
        self.closed: list[str] = []
        self.prompt_texts: list[str] = []
        self.notifications: list[tuple[str, str | None, str]] = []

    def tab_create(self, *, cwd: Any, label: Any = None) -> str:
        self.tabs.append((str(cwd), label))
        return f"w{len(self.tabs)}:p1"

    def agent_start(
        self,
        *,
        name: str,
        kind: str,
        pane_id: str,
        start_timeout_ms: int,
        model: Any = None,
        session_id: Any = None,
    ) -> None:
        self.started.append(name)

    def agent_interactive_ready(self, target: str) -> bool:
        return self.ready

    def agent_prompt_wait_with_watchdog(
        self,
        *,
        target: str,
        text: str,
        timeout_ms: int,
        poll_interval_s: float = 30.0,
        on_poll: Any = None,
    ) -> str:
        self.prompt_texts.append(text)
        if self.on_prompt is not None:
            self.on_prompt(text)
        return self.settle_status

    def agent_read(self, target: str, *, lines: int = 200) -> str:
        return ""

    def agent_read_visible(self, target: str, *, lines: int = 200) -> str:
        return ""

    def pane_close(self, pane_id: str) -> None:
        self.closed.append(pane_id)

    def agent_statuses(self) -> dict[str, str]:
        return dict(self.statuses)

    def notification_show(
        self, title: str, *, body: str | None = None, sound: str = "none"
    ) -> None:
        self.notifications.append((title, body, sound))


def _mark_posted(gh: ReviewGh) -> Any:
    import re

    def _cb(text: str) -> None:
        match = re.search(r"key=(\S+)", text)
        if match:
            gh.posted.add(match.group(1))

    return _cb


def _setup_repo(
    tmp_path: Path, owner: str, repo: str, head_branch: str, base_dir: Path
) -> tuple[Path, Path]:
    """A bare ``origin`` with ``main`` + ``head_branch``, and a clone of it at
    ``<base_dir>/<owner>/<repo>`` (the per-repo checkout ``_ensure_checkout`` targets)."""
    origin = tmp_path / "origins" / f"{owner}__{repo}.git"
    origin.parent.mkdir(parents=True, exist_ok=True)
    seed = tmp_path / "seed" / f"{owner}__{repo}"
    seed.mkdir(parents=True)
    _git("init", "-b", "main", cwd=seed)
    _git("config", "user.email", "t@example.com", cwd=seed)
    _git("config", "user.name", "Test", cwd=seed)
    (seed / "README.md").write_text("hi\n")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    _git("init", "--bare", str(origin), cwd=tmp_path)
    _git("remote", "add", "origin", str(origin), cwd=seed)
    _git("push", "origin", "main", cwd=seed)
    _git("checkout", "-b", head_branch, cwd=seed)
    (seed / "pr.txt").write_text("change\n")
    _git("add", "pr.txt", cwd=seed)
    _git("commit", "-m", "pr", cwd=seed)
    _git("push", "origin", head_branch, cwd=seed)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=origin)
    checkout = base_dir / owner / repo
    checkout.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "clone", str(origin), str(checkout)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return origin, checkout


def _run_cycle(
    job: Job,
    history_path: Path,
    gh: ReviewGh,
    client: ReviewHerdrClient,
    *,
    run_id: str,
) -> tuple[str, bool]:
    return tick._review_cycle(
        job,
        history_path,
        client=client,  # type: ignore[arg-type]
        now=NOW,
        run_id=run_id,
    )


def _terminal(history_path: Path, job_name: str) -> Any:
    records = read_job(history_path, job_name)
    assert records, "no history records"
    return records[-1]


# ---------------------------------------------------------------------------
# AC 1 — config validation
# ---------------------------------------------------------------------------


def test_review_job_config_validation(tmp_path: Path) -> None:
    job = _load_jobs(tmp_path, _base_review_job_dict()).job("review-my-prs")
    assert job is not None
    assert job.kind == "review"
    assert job.select == ("author", "review_requested")
    assert job.include_auto_branches is False
    assert job.max_attempts_per_target == 1  # kind: review default
    assert job.target == "pr"
    from herdr_routines.repos import default_repos_dir

    assert job.repo == default_repos_dir()

    assert "review" in VALID_JOB_KINDS
    assert "select" not in _DEFAULTS_ALLOWED_KEYS
    assert "include_auto_branches" not in _DEFAULTS_ALLOWED_KEYS

    def _reject(over: dict[str, Any], match: str) -> None:
        job_dict = _base_review_job_dict()
        job_dict.update(over)
        with pytest.raises(ConfigError, match=match):
            _load_jobs(tmp_path, job_dict)

    _reject({"repo": "/repo/x"}, "not applicable")
    _reject({"repository": "https://example.invalid/x.git"}, "not applicable")
    _reject({"workspace": "worktree"}, "not applicable")
    _reject({"checks": [{"pr_health": None}]}, "not applicable")
    _reject({"select": []}, "non-empty 'select'")
    _reject({"select": ["author", "author"]}, "duplicates")
    _reject({"select": ["bogus"]}, "select\\[0\\]")
    _reject({"include_auto_branches": "yes"}, "boolean")
    _reject({"target": "base"}, "absent or 'pr'")

    # select/include_auto_branches require kind: review.
    with pytest.raises(ConfigError, match="require kind: review"):
        _load_jobs(
            tmp_path,
            {
                "name": "plain",
                "cron": "0 1 * * *",
                "repo": "/repo/x",
                "select": ["author"],
            },
        )


# ---------------------------------------------------------------------------
# AC 2 — one batched search with @me qualifiers
# ---------------------------------------------------------------------------


def test_review_scan_selects_open_non_draft_prs(tmp_path: Path) -> None:
    assert "is:pr" in REVIEW_SEARCH_QUALIFIERS["author"]
    assert "is:open" in REVIEW_SEARCH_QUALIFIERS["author"]
    assert "archived:false" in REVIEW_SEARCH_QUALIFIERS["author"]
    assert "draft:false" in REVIEW_SEARCH_QUALIFIERS["author"]
    assert "author:@me" in REVIEW_SEARCH_QUALIFIERS["author"]
    assert "review-requested:@me" in REVIEW_SEARCH_QUALIFIERS["review_requested"]

    job = _review_job(tmp_path, select=("author", "review_requested"))
    # A review_requested PR is someone else's by definition: its author never matches
    # the login, and that must not skip it as author_mismatch.
    gh = ReviewGh(prs=(_pr(1), _pr(2, author="teammate", mode="review_requested")))
    outcome = scan_review_targets(gh, job, login="me")
    assert outcome is not None
    assert len(gh.search_calls) == 1  # one batched call, not one per mode
    modes, _limit = gh.search_calls[0]
    assert modes == ("author", "review_requested")
    assert {c.number for c in outcome.candidates} == {1, 2}
    assert outcome.skipped == ()


# ---------------------------------------------------------------------------
# AC 3 — auto/ branches skipped unless opted in
# ---------------------------------------------------------------------------


def test_review_scan_skips_auto_branches_unless_opted_in(tmp_path: Path) -> None:
    job = _review_job(tmp_path)
    pr = _pr(1, head_ref="auto/pipeline-20261010T080000Z")
    outcome = scan_review_targets(ReviewGh(prs=(pr,)), job, login="me")
    assert outcome is not None
    assert outcome.candidates == ()
    assert len(outcome.skipped) == 1
    assert outcome.skipped[0].skip_reason == "routine_owned_branch"

    opted_in = _review_job(tmp_path, include_auto_branches=True)
    outcome2 = scan_review_targets(ReviewGh(prs=(pr,)), opted_in, login="me")
    assert outcome2 is not None
    assert len(outcome2.candidates) == 1
    assert outcome2.candidates[0].skip_reason is None


# ---------------------------------------------------------------------------
# AC 4 — fork PRs skipped
# ---------------------------------------------------------------------------


def test_review_scan_skips_fork_prs(tmp_path: Path) -> None:
    job = _review_job(tmp_path)
    fork = _pr(9, repo="acme/widgets", head_repo="someone/widgets")
    outcome = scan_review_targets(ReviewGh(prs=(fork,)), job, login="me")
    assert outcome is not None
    assert outcome.candidates == ()
    assert outcome.skipped[0].skip_reason == "fork_pr"
    # author_mismatch takes precedence (a stale login) and is a distinct reason.
    stale = _pr(10, author="someone-else")
    outcome2 = scan_review_targets(ReviewGh(prs=(stale,)), job, login="me")
    assert outcome2 is not None
    assert outcome2.skipped[0].skip_reason == "author_mismatch"


# ---------------------------------------------------------------------------
# AC 5 — oldest-first ordering and newest-first cap
# ---------------------------------------------------------------------------


def test_review_scan_orders_oldest_first_and_caps_newest(tmp_path: Path) -> None:
    job = _review_job(tmp_path)
    prs = (
        _pr(3, created_at="2026-10-03T00:00:00Z"),
        _pr(1, created_at="2026-10-01T00:00:00Z"),
        _pr(2, created_at="2026-10-02T00:00:00Z"),
    )
    outcome = scan_review_targets(ReviewGh(prs=prs), job, login="me")
    assert outcome is not None
    assert [c.number for c in outcome.candidates] == [1, 2, 3]

    kept, over = cap_candidates(outcome.candidates, 2)
    assert [c.number for c in kept] == [
        1,
        2,
    ]  # newest (3) is starved, never the backlog
    assert [c.number for c in over] == [3]
    assert over[0].skip_reason == "over_cap"


# ---------------------------------------------------------------------------
# AC 6 — marker guard is exact-match; search fails closed
# ---------------------------------------------------------------------------


def test_review_scan_marker_guard_and_search_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    job = _review_job(tmp_path)
    cand = Candidate(
        target_key="acme/widgets#1",
        repo="acme/widgets",
        owner="acme",
        repo_name="widgets",
        number=1,
        title="PR 1",
        url="https://example.invalid/1",
        created_at="2026-10-01T00:00:00Z",
        updated_at="2026-10-01T00:00:00Z",
        head_ref="feature-x",
        author="me",
        select_mode="author",
    )

    posted = ReviewGh()
    posted.posted.add("acme/widgets#1")
    trusted = frozenset({"me"})
    dispatchable, skipped = apply_marker_guard(
        posted, "review-my-prs", [cand], trusted_authors=trusted
    )
    assert dispatchable == []
    assert skipped[0].skip_reason == "review_already_posted"

    # Another job's marker for the same key is not a match.
    other = ReviewGh(
        raw_bodies={
            "acme/widgets#1": [
                PostedBody(
                    author="me", body=review_marker("some-other-job", "acme/widgets#1")
                ),
            ]
        }
    )
    dispatchable2, skipped2 = apply_marker_guard(
        other, "review-my-prs", [cand], trusted_authors=trusted
    )
    assert dispatchable2 == [cand]
    assert skipped2 == []

    # A body read failure (None) is unverified → still dispatchable.
    failing = ReviewGh(bodies_fail=True)
    dispatchable3, skipped3 = apply_marker_guard(
        failing, "review-my-prs", [cand], trusted_authors=trusted
    )
    assert dispatchable3 == [cand]
    assert skipped3 == []

    # Search failure: scan returns None and the cycle records failed (search_failed).
    history_path = tmp_path / "state" / "history.jsonl"
    gh = ReviewGh(search_fail=True)
    client = ReviewHerdrClient()
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh)
    summary, failed = _run_cycle(
        job, history_path, gh, client, run_id="review-my-prs-20261010T083000Z"
    )
    assert failed is True
    assert "failed" in summary
    record = _terminal(history_path, job.name)
    assert record.state == "failed"
    assert record.extra is not None and record.extra["reason"] == "search_failed"


# ---------------------------------------------------------------------------
# AC 7 — attempt budget is counted per target key
# ---------------------------------------------------------------------------


def test_review_attempts_are_counted_per_target_key(tmp_path: Path) -> None:
    history_path = tmp_path / "state" / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    for target in ("acme/widgets#7", "acme/widgets#7"):
        append(
            history_path,
            HistoryRecord(
                ts=NOW,
                job="review-my-prs",
                state="done",
                run_id="r",
                extra={"target_key": target},
            ),
        )
    append(
        history_path,
        HistoryRecord(
            ts=NOW,
            job="review-my-prs",
            state="skipped",
            run_id="r",
            extra={"target_key": "acme/widgets#7"},
        ),
    )
    append(
        history_path,
        HistoryRecord(
            ts=NOW,
            job="review-my-prs",
            state="done",
            run_id="r",
            extra={"target_key": "other/repo#7"},
        ),
    )
    # Failures before the agent could post (infra) never consume budget; failures
    # after it ran to settle (a review may have landed) always do.
    for reason in ("worktree_failed", "agent_start_failed", "agent_prompt_failed"):
        append(
            history_path,
            HistoryRecord(
                ts=NOW,
                job="review-my-prs",
                state="failed",
                run_id="r",
                extra={"target_key": "acme/widgets#8", "reason": reason},
            ),
        )

    assert (
        attempt_count_for_target(history_path, "review-my-prs", "acme/widgets#7") == 2
    )
    assert attempt_count_for_target(history_path, "review-my-prs", "other/repo#7") == 1
    assert (
        attempt_count_for_target(history_path, "review-my-prs", "acme/widgets#8") == 0
    )
    for i, reason in enumerate(("review_not_posted", "review_verification_failed")):
        append(
            history_path,
            HistoryRecord(
                ts=NOW,
                job="review-my-prs",
                state="failed",
                run_id="r",
                extra={"target_key": "acme/widgets#11", "reason": reason},
            ),
        )
        assert (
            attempt_count_for_target(history_path, "review-my-prs", "acme/widgets#11")
            == i + 1
        )

    def _c(key: str) -> Candidate:
        owner, _, rest = key.partition("/")
        repo_name, _, num = rest.partition("#")
        return Candidate(
            target_key=key,
            repo=f"{owner}/{repo_name}",
            owner=owner,
            repo_name=repo_name,
            number=int(num),
            title="",
            url="",
            created_at="",
            updated_at="",
            head_ref="b",
            author="me",
            select_mode="author",
        )

    dispatchable, skipped = apply_attempt_budget(
        history_path,
        "review-my-prs",
        [_c("acme/widgets#7"), _c("other/repo#9"), _c("acme/widgets#8")],
        max_attempts=2,
    )
    assert [c.target_key for c in dispatchable] == ["other/repo#9", "acme/widgets#8"]
    assert [c.target_key for c in skipped] == ["acme/widgets#7"]
    assert skipped[0].skip_reason == "max_attempts_exceeded"


# ---------------------------------------------------------------------------
# AC 8 — aggregate report lists candidates, dispositions and counts
# ---------------------------------------------------------------------------


def test_review_report_lists_candidates_and_skips() -> None:
    def _c(key: str, reason: str | None = None) -> Candidate:
        owner, _, rest = key.partition("/")
        repo_name, _, num = rest.partition("#")
        return Candidate(
            target_key=key,
            repo=f"{owner}/{repo_name}",
            owner=owner,
            repo_name=repo_name,
            number=int(num),
            title="",
            url="",
            created_at="",
            updated_at="",
            head_ref="b",
            author="me",
            select_mode="author",
            skip_reason=reason,
        )

    rows = [
        (_c("acme/widgets#1"), "dispatched"),
        (_c("acme/widgets#2", "review_already_posted"), "review_already_posted"),
        (_c("acme/widgets#3", "routine_owned_branch"), "routine_owned_branch"),
        (_c("acme/widgets#4"), "review_not_posted"),
    ]
    report = render_review_report(
        job_name="review-my-prs",
        run_id="review-my-prs-20261010T083000Z",
        now=NOW,
        enumerated=4,
        candidate_count=2,
        repos_failed=1,
        rows=rows,
        truncated_modes=("author",),
    )
    assert "acme/widgets#1: dispatched" in report
    assert "acme/widgets#2: review_already_posted" in report
    assert "acme/widgets#3: routine_owned_branch" in report
    assert "**Enumerated**: 4" in report
    assert "**Candidates**: 2" in report
    assert "**Dispatched**: 1" in report
    assert "acme/widgets#4: review_not_posted" in report
    assert "**Skipped**: 2" in report  # a failure is not a skip
    assert "**Failed**: 1" in report
    assert "**Repos failed**: 1" in report
    assert "**Search truncated**: author" in report


# ---------------------------------------------------------------------------
# AC 9 — shipped example config
# ---------------------------------------------------------------------------


def test_shipped_review_job_config_validates() -> None:
    example_dir = Path(__file__).resolve().parent.parent / "deploy" / "jobs.d"
    cfg = load_config_dir(example_dir)
    job = cfg.job("review-my-prs")
    assert job is not None, cfg.errors
    assert job.kind == "review"
    assert job.enabled is False
    assert job.select == ("author", "review_requested")


# ---------------------------------------------------------------------------
# AC 10 — per-repo checkout, default-branch lookup cached per tick
# ---------------------------------------------------------------------------


def test_review_dispatch_creates_per_repo_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    base = tmp_path / "repos"
    origin, checkout = _setup_repo(tmp_path, "acme", "widgets", "feature-x", base)
    job = _review_job(tmp_path, repo=base)
    gh = ReviewGh(
        prs=(_pr(1), _pr(2, head_ref="feature-x")),
        repo_infos={
            ("acme", "widgets"): {"clone_url": str(origin), "default_branch": "main"}
        },
    )
    client = ReviewHerdrClient(on_prompt=_mark_posted(gh))
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh)

    summary, failed = _run_cycle(
        job,
        tmp_path / "state" / "history.jsonl",
        gh,
        client,
        run_id="review-my-prs-20261010T083000Z",
    )
    assert failed is False, summary
    assert checkout.is_dir()
    assert (checkout / ".git").exists()
    # One default-branch lookup per distinct repo, even with two PRs in it.
    assert gh.repo_info_calls == [("acme", "widgets")]
    assert len(gh.search_calls) == 1

    # The refactor must keep the report's aggregate block byte-identical (issue 066):
    # same numbers today as after `_review_cycle`'s row-building moves into the shared
    # classifier.
    report_path = (
        tick.default_reports_dir() / "review-review-my-prs-20261010T083000Z.md"
    )
    report = report_path.read_text()
    for line in (
        "**Enumerated**: 2",
        "**Candidates**: 2",
        "**Dispatched**: 2",
        "**Skipped**: 0",
        "**Failed**: 0",
        "**Repos failed**: 0",
    ):
        assert line in report
    assert report.count("acme/widgets#1: dispatched") == 1
    assert report.count("acme/widgets#2: dispatched") == 1

    # The terminal history record carries the same aggregate numbers, unchanged.
    record = _terminal(tmp_path / "state" / "history.jsonl", job.name)
    assert record.state == "done"
    assert record.extra is not None
    assert (
        record.extra["enumerated"],
        record.extra["candidates"],
        record.extra["skipped"],
        record.extra["failed"],
        record.extra["repos_failed"],
    ) == (2, 2, 0, 0, 0)


def test_marker_guard_ignores_a_marker_from_an_untrusted_author() -> None:
    """The marker is plain text: a copy posted by someone outside the trusted set must
    not suppress the review, nor may a deleted account's."""
    cand = Candidate(
        target_key="acme/widgets#1",
        repo="acme/widgets",
        owner="acme",
        repo_name="widgets",
        number=1,
        title="PR 1",
        url="https://example.invalid/1",
        created_at="2026-10-01T00:00:00Z",
        updated_at="2026-10-01T00:00:00Z",
        head_ref="feature-x",
        author="me",
        select_mode="author",
    )
    marker = review_marker("review-my-prs", cand.target_key)
    gh = ReviewGh(
        raw_bodies={
            cand.target_key: [
                PostedBody(author="drive-by", body=marker),
                PostedBody(author=None, body=marker),
            ]
        }
    )
    dispatchable, skipped = apply_marker_guard(
        gh, "review-my-prs", [cand], trusted_authors=frozenset({"me"})
    )
    assert dispatchable == [cand]
    assert skipped == []


def test_review_post_condition_ignores_an_untrusted_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR carrying only someone else's copy of the marker still gets its review
    dispatched, and that copy does not satisfy the post-condition either: the review
    counts as not posted."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    base = tmp_path / "repos"
    origin, _checkout = _setup_repo(tmp_path, "acme", "widgets", "feature-x", base)
    job = _review_job(tmp_path, repo=base)
    key = "acme/widgets#1"
    gh = ReviewGh(
        prs=(_pr(1),),
        repo_infos={
            ("acme", "widgets"): {"clone_url": str(origin), "default_branch": "main"}
        },
        raw_bodies={
            key: [
                PostedBody(author="drive-by", body=review_marker("review-my-prs", key))
            ]
        },
    )
    client = ReviewHerdrClient()
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh)
    history = tmp_path / "state" / "history.jsonl"

    _, failed = _run_cycle(job, history, gh, client, run_id="review-my-prs-r1")

    assert len(client.started) == 1, "an untrusted marker suppressed the review"
    assert failed is True
    reasons = [r.extra.get("reason") for r in read_job(history, job.name) if r.extra]
    assert "review_not_posted" in reasons


# ---------------------------------------------------------------------------
# AC 11 — a detached review worktree never contends for the PR head branch
# ---------------------------------------------------------------------------


def test_review_dispatch_does_not_contend_for_head_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    base = tmp_path / "repos"
    origin, checkout = _setup_repo(tmp_path, "acme", "widgets", "feature-x", base)
    # Another owner (e.g. the orchestrator) has the PR head branch checked out.
    other_wt = tmp_path / "orchestrator-worktree"
    _git(
        "worktree",
        "add",
        "-B",
        "feature-x",
        str(other_wt),
        "origin/feature-x",
        cwd=checkout,
    )
    branches_before = _git("branch", "--list", cwd=checkout)

    job = _review_job(tmp_path, repo=base)
    info: dict[str, object] = {"clone_url": str(origin), "default_branch": "main"}
    gh = ReviewGh(prs=(_pr(1),), repo_infos={("acme", "widgets"): info})
    client = ReviewHerdrClient(on_prompt=_mark_posted(gh))
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh)

    _, failed = _run_cycle(
        job,
        tmp_path / "state" / "history.jsonl",
        gh,
        client,
        run_id="review-my-prs-20261010T083000Z",
    )
    # The review still runs, on a detached HEAD, without touching the other worktree
    # or creating a local branch in the shared checkout.
    assert failed is False
    assert len(client.started) == 1
    assert other_wt.is_dir()
    assert _git("branch", "--list", cwd=checkout) == branches_before
    record = _terminal(tmp_path / "state" / "history.jsonl", job.name)
    assert record.state == "done"
    assert record.extra is not None and record.extra["dispatched"] == 1


# ---------------------------------------------------------------------------
# AC 12 — own clean review worktree is reused; a stale one is re-added
# ---------------------------------------------------------------------------


def test_review_dispatch_reuses_own_review_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    base = tmp_path / "repos"
    origin, checkout = _setup_repo(tmp_path, "acme", "widgets", "feature-x", base)
    wt_path = checkout / ".worktrees" / "review-pr1"
    # A clean worktree left behind by a prior tick — exactly what "reuse" targets.
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    _git(
        "worktree",
        "add",
        "--detach",
        str(wt_path),
        "origin/feature-x",
        cwd=checkout,
    )

    job = _review_job(tmp_path, repo=base)
    gh = ReviewGh(
        prs=(_pr(1),),
        repo_infos={
            ("acme", "widgets"): {"clone_url": str(origin), "default_branch": "main"}
        },
    )
    client = ReviewHerdrClient(on_prompt=_mark_posted(gh))
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh)

    cleanup_calls = {"n": 0}
    real_cleanup = tick._cleanup_worktree

    def _spy(repo: Path, path: Path) -> None:
        cleanup_calls["n"] += 1
        real_cleanup(repo, path)

    monkeypatch.setattr(tick, "_cleanup_worktree", _spy)

    _run_cycle(
        job,
        tmp_path / "state" / "history.jsonl",
        gh,
        client,
        run_id="review-my-prs-20261010T083000Z",
    )
    # Reused in place (one cleanup at settle, not a pre-dispatch force-remove + re-add).
    assert client.tabs[0][0] == str(wt_path)
    assert cleanup_calls["n"] == 1
    assert not wt_path.exists()  # removed after settle

    # A stale own worktree (dirty) is force-removed and re-added, then still cleaned up.
    dirty = checkout / ".worktrees" / "review-pr1"
    _git(
        "worktree",
        "add",
        "--detach",
        str(dirty),
        "origin/feature-x",
        cwd=checkout,
    )
    (dirty / "scratch.txt").write_text("uncommitted\n")
    cleanup_calls["n"] = 0
    gh2 = ReviewGh(
        prs=(_pr(1),),
        repo_infos={
            ("acme", "widgets"): {"clone_url": str(origin), "default_branch": "main"}
        },
    )
    client2 = ReviewHerdrClient(on_prompt=_mark_posted(gh2))
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh2)
    _run_cycle(
        job,
        tmp_path / "state2" / "history.jsonl",
        gh2,
        client2,
        run_id="review-my-prs-20261010T090000Z",
    )
    assert cleanup_calls["n"] == 2  # pre-dispatch force-remove + post-settle removal
    assert not dirty.exists()


# ---------------------------------------------------------------------------
# AC 13 — worktree removed with --force after settle on success and failure
# ---------------------------------------------------------------------------


def test_review_dispatch_removes_worktree_after_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    base = tmp_path / "repos"
    origin, checkout = _setup_repo(tmp_path, "acme", "widgets", "feature-x", base)
    wt_path = checkout / ".worktrees" / "review-pr1"

    job = _review_job(tmp_path, repo=base)
    gh = ReviewGh(
        prs=(_pr(1),),
        repo_infos={
            ("acme", "widgets"): {"clone_url": str(origin), "default_branch": "main"}
        },
    )
    client = ReviewHerdrClient(on_prompt=_mark_posted(gh))
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh)

    recorded: list[list[str]] = []
    real_run = tick.subprocess.run

    def _spy(argv: list[str], *a: Any, **k: Any) -> Any:
        recorded.append(list(argv))
        return real_run(argv, *a, **k)

    monkeypatch.setattr(tick.subprocess, "run", _spy)

    _run_cycle(
        job,
        tmp_path / "state" / "history.jsonl",
        gh,
        client,
        run_id="review-my-prs-20261010T083000Z",
    )
    assert not wt_path.exists()
    assert [
        "git",
        "-C",
        str(checkout),
        "worktree",
        "remove",
        "--force",
        str(wt_path),
    ] in recorded

    # Failure path (verification fails): the worktree is still removed.
    gh2 = ReviewGh(
        prs=(_pr(2),),
        repo_infos={
            ("acme", "widgets"): {"clone_url": str(origin), "default_branch": "main"}
        },
    )
    client2 = ReviewHerdrClient()  # never posts
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh2)
    _run_cycle(
        job,
        tmp_path / "state" / "history.jsonl",
        gh2,
        client2,
        run_id="review-my-prs-20261010T090000Z",
    )
    assert not (checkout / ".worktrees" / "review-pr2").exists()


# ---------------------------------------------------------------------------
# AC 14 — post-condition is fail-closed
# ---------------------------------------------------------------------------


def test_review_verification_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    base = tmp_path / "repos"
    origin, _checkout = _setup_repo(tmp_path, "acme", "widgets", "feature-x", base)
    job = _review_job(tmp_path, repo=base)
    info: dict[str, object] = {"clone_url": str(origin), "default_branch": "main"}

    # (a) marker present → done + verified True + gate passed.
    gh = ReviewGh(prs=(_pr(1),), repo_infos={("acme", "widgets"): info})
    client = ReviewHerdrClient(on_prompt=_mark_posted(gh))
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh)
    history = tmp_path / "state" / "history.jsonl"
    _, failed = _run_cycle(job, history, gh, client, run_id="r1")
    assert failed is False
    rec = _terminal(history, job.name)
    assert rec.extra is not None
    assert rec.extra["gate"] == "passed" and rec.extra["dispatched"] == 1
    target_rec = [
        r for r in read_job(history, job.name) if r.extra and r.extra.get("target_key")
    ]
    assert target_rec[-1].state == "done"
    assert target_rec[-1].extra is not None and target_rec[-1].extra["verified"] is True

    # (b) marker absent → failed (review_not_posted), never done.
    gh2 = ReviewGh(prs=(_pr(2),), repo_infos={("acme", "widgets"): info})
    client2 = ReviewHerdrClient()  # never posts
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh2)
    _, failed2 = _run_cycle(job, history, gh2, client2, run_id="r2")
    assert failed2 is True
    rec2 = _terminal(history, job.name)
    assert rec2.state == "failed"
    assert rec2.extra is not None and rec2.extra["gate"] == "failed"

    # (c) body read failure → failed (review_verification_failed), never done.
    gh3 = ReviewGh(
        prs=(_pr(3),), repo_infos={("acme", "widgets"): info}, bodies_fail=True
    )
    client3 = ReviewHerdrClient()
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh3)
    _, failed3 = _run_cycle(job, history, gh3, client3, run_id="r3")
    assert failed3 is True
    rec3 = _terminal(history, job.name)
    assert rec3.state == "failed"


# ---------------------------------------------------------------------------
# AC 15 — per-repo failure isolation
# ---------------------------------------------------------------------------


def test_review_dispatch_isolates_per_repo_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    base = tmp_path / "repos"
    origin_a, _checkout_a = _setup_repo(tmp_path, "acme", "widgets", "feature-x", base)
    job = _review_job(tmp_path, repo=base)
    gh = ReviewGh(
        prs=(
            _pr(1, repo="acme/widgets"),
            _pr(2, repo="acme/broken", head_ref="feature-y"),
        ),
        repo_infos={
            ("acme", "widgets"): {"clone_url": str(origin_a), "default_branch": "main"},
            # A clone that cannot possibly succeed: no such path.
            ("acme", "broken"): {
                "clone_url": str(tmp_path / "does-not-exist.git"),
                "default_branch": "main",
            },
        },
    )
    client = ReviewHerdrClient(on_prompt=_mark_posted(gh))
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh)

    history = tmp_path / "state" / "history.jsonl"
    _, failed = _run_cycle(job, history, gh, client, run_id="r1")
    assert failed is True  # the broken repo makes the tick red...
    rec = _terminal(history, job.name)
    assert rec.extra is not None
    assert rec.extra["dispatched"] == 1  # ...but the healthy repo still dispatched
    assert rec.extra["repos_failed"] == 1


# ---------------------------------------------------------------------------
# AC 16 — shared prefix, name cap, live-agent guard
# ---------------------------------------------------------------------------


def test_review_live_agent_prefix_guard_and_name_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_job = "review-all-the-me-prs"  # 21 chars
    key = "acme/widgets#1"
    prefix = build_review_agent_prefix(long_job, key)
    assert prefix.startswith("rt-")
    assert len(prefix) <= 25

    name = build_review_agent_name(long_job, key, f"{long_job}-20261010T080000Z")
    assert name.startswith(prefix)
    assert len(name) <= 32
    assert len(name) - len(prefix) - 1 >= 6  # a full run tail always fits

    # The liveness guard matches by the shared prefix over live agents.
    client = ReviewHerdrClient(statuses={name: "working"})
    assert tick._live_review_agent_exists(client, long_job, key) is True  # type: ignore[arg-type]
    idle = ReviewHerdrClient(statuses={name: "idle"})
    assert tick._live_review_agent_exists(idle, long_job, key) is False  # type: ignore[arg-type]
    unrelated = ReviewHerdrClient(statuses={"something-else": "working"})
    assert tick._live_review_agent_exists(unrelated, long_job, key) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC 17 — default prompt is read-only and idempotent
# ---------------------------------------------------------------------------


def test_review_prompt_is_read_only_and_idempotent() -> None:
    prompt = build_review_prompt(
        job_name="review-my-prs",
        target_key="acme/widgets#1",
        url="https://example.invalid/acme/widgets/1",
        report_path="/tmp/reports/auto-review-r1-ab12cd.md",
    )
    marker = review_marker("review-my-prs", "acme/widgets#1")
    assert "read-only" in prompt.lower()
    assert "do not" in prompt.lower()
    assert "code-review" in prompt
    assert "blocking" in prompt and "non-blocking" in prompt
    assert "single-session" in prompt
    # A COMMENT review is the only kind GitHub accepts on the user's own PR.
    assert "gh pr review --comment" in prompt
    assert "$ROUTINE_REPORT" in prompt
    assert "## Outcome:" in prompt
    # Idempotence: the exact marker is embedded and the agent must re-check it first.
    assert marker in prompt
    assert "POST NOTHING" in prompt
    # Only the worker's own earlier review stops it, as in the engine's marker guard.
    assert "your own gh login" in prompt
    # Read-only by construction: none of the mutating verbs appear as instructions.
    for forbidden in ("git push", "git commit", "resolveReviewThread"):
        assert forbidden not in prompt


# ---------------------------------------------------------------------------
# AC 18 — not repeated across ticks (marker guard + attempt budget)
# ---------------------------------------------------------------------------


def test_review_is_not_repeated_across_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    base = tmp_path / "repos"
    origin, _checkout = _setup_repo(tmp_path, "acme", "widgets", "feature-x", base)
    job = _review_job(tmp_path, repo=base)
    info: dict[str, object] = {"clone_url": str(origin), "default_branch": "main"}
    history = tmp_path / "state" / "history.jsonl"

    gh = ReviewGh(prs=(_pr(1),), repo_infos={("acme", "widgets"): info})
    client = ReviewHerdrClient(on_prompt=_mark_posted(gh))
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh)

    # Tick 1: dispatches and (via the fake agent) posts the marker.
    _, failed1 = _run_cycle(job, history, gh, client, run_id="t1")
    assert failed1 is False
    assert len(client.tabs) == 1

    # Tick 2: marker present → review_already_posted, no second dispatch.
    client2 = ReviewHerdrClient(on_prompt=_mark_posted(gh))
    _, failed2 = _run_cycle(job, history, gh, client2, run_id="t2")
    assert failed2 is False
    assert client2.tabs == []

    # Tick 3 (same process restart): even if the marker is unverifiable, the attempt
    # budget stops re-dispatch.
    gh.posted.clear()
    client3 = ReviewHerdrClient(on_prompt=_mark_posted(gh))
    _, failed3 = _run_cycle(job, history, gh, client3, run_id="t3")
    assert failed3 is False
    assert client3.tabs == []
    assert attempt_count_for_target(history, job.name, "acme/widgets#1") == 1


# ---------------------------------------------------------------------------
# AC 19 — _ensure_checkout refactor preserves babysit behavior
# ---------------------------------------------------------------------------


def test_ensure_checkout_refactor_preserves_babysit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from herdr_routines import repos
    from herdr_routines.config import Job as CfgJob

    # A plain `repo:` job (clone_url None) is never cloned, only synced — even when the
    # checkout is already present.
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    job = CfgJob(
        name="babysit-prs",
        enabled=True,
        cron="*/30 * * * *",
        repo=repo,
        workspace="worktree",
        base="main",
        agent_kind="claude",
        model=None,
        prompt="",
        timeout_ms=1000,
        start_timeout_ms=1000,
        catch_up_minutes=120,
        timezone="UTC",
        on_missed="log",
        repository=None,
    )

    clone_calls: list[str] = []
    ff_calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(repos, "_clone", lambda url, dest: clone_calls.append(url))
    monkeypatch.setattr(
        repos,
        "_fetch_and_fast_forward",
        lambda checkout, base="main": ff_calls.append((checkout, base)),
    )

    assert repos.ensure_repo(job) == repo
    assert clone_calls == []  # clone_url None ⇒ never cloned
    assert ff_calls == [(repo, "main")]

    # A `repository:` job with a missing checkout clones; a present one syncs.
    repo2 = tmp_path / "repo2"
    import dataclasses

    job2 = dataclasses.replace(
        job,
        name="r",
        repo=repo2,
        repository="https://example.invalid/x.git",
    )
    clone_calls.clear()
    ff_calls.clear()
    repos.ensure_repo(job2)
    assert clone_calls == ["https://example.invalid/x.git"]
    assert ff_calls == []

    (repo2 / ".git").mkdir(parents=True)
    clone_calls.clear()
    ff_calls.clear()
    repos.ensure_repo(job2)
    assert clone_calls == []
    assert ff_calls == [(repo2, "main")]


def test_real_pr_review_bodies_carry_their_authors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`gh pr view --json comments,reviews` names the author `author.login`; a deleted
    account comes back as `author: null` and is kept with no author, so it can never be
    trusted. Empty bodies are dropped as before."""
    import json

    from herdr_routines.auto_fix import RealGhClient

    payload = {
        "reviews": [{"author": {"login": "me"}, "body": "review"}],
        "comments": [
            {"author": None, "body": "ghost"},
            {"author": {"login": "you"}, "body": ""},
        ],
    }
    monkeypatch.setattr(
        RealGhClient, "_run", lambda self, argv: (0, json.dumps(payload), "")
    )
    assert RealGhClient().pr_review_bodies(owner="o", repo="r", number=1) == [
        PostedBody(author="me", body="review"),
        PostedBody(author=None, body="ghost"),
    ]


# ---------------------------------------------------------------------------
# Issue 066 — shared classification helper (spec 20261021T050000Z)
# ---------------------------------------------------------------------------


def _mk_candidate(key: str, *, reason: str | None = None) -> Candidate:
    owner, _, rest = key.partition("/")
    repo_name, _, num = rest.partition("#")
    return Candidate(
        target_key=key,
        repo=f"{owner}/{repo_name}",
        owner=owner,
        repo_name=repo_name,
        number=int(num),
        title="",
        url="",
        created_at="",
        updated_at="",
        head_ref="b",
        author="me",
        select_mode="author",
        skip_reason=reason,
    )


def test_classify_review_candidates_pipeline_order_and_none_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper runs the pipeline in the documented, load-bearing order — scan →
    attempt budget → marker guard → cap against `job.max_workers_per_tick` — and a
    `None` scan result propagates as `None` (fail closed) without running any later
    stage."""
    from herdr_routines import review_scan as rs
    from herdr_routines.review_scan import (
        Classification,
        classify_review_candidates,
    )

    # The documented dataclass shape: frozen, and exactly these fields.
    assert [f.name for f in dataclasses.fields(Classification)] == [
        "rows",
        "dispatchable",
        "truncated_modes",
        "enumerated",
        "candidate_count",
    ]

    job = _review_job(tmp_path, max_workers_per_tick=2)
    history_path = tmp_path / "state" / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    trusted = frozenset({"me"})
    # Three fresh candidates, budget nowhere near: #3 is starved by the 2-worker cap.
    gh = ReviewGh(
        prs=(
            _pr(1, created_at="2026-10-01T00:00:00Z"),
            _pr(2, created_at="2026-10-02T00:00:00Z"),
            _pr(3, created_at="2026-10-03T00:00:00Z"),
        )
    )

    order: list[str] = []
    real: dict[str, Any] = {
        "scan_review_targets": rs.scan_review_targets,
        "apply_attempt_budget": rs.apply_attempt_budget,
        "apply_marker_guard": rs.apply_marker_guard,
        "cap_candidates": rs.cap_candidates,
    }

    def _wrap(name: str) -> Any:
        fn = real[name]

        def _recorded(*args: Any, **kwargs: Any) -> Any:
            order.append(name)
            return fn(*args, **kwargs)

        return _recorded

    for name in real:
        monkeypatch.setattr(rs, name, _wrap(name))

    result = classify_review_candidates(
        gh, job, login="me", history_path=history_path, trusted_authors=trusted
    )
    assert result is not None
    assert order == [
        "scan_review_targets",
        "apply_attempt_budget",
        "apply_marker_guard",
        "cap_candidates",
    ]
    # The marker guard costs one `gh pr view` per budget survivor (2), not per
    # enumerated candidate (3).
    assert len(gh.bodies_calls) == 2
    assert result.truncated_modes == ()
    assert result.enumerated == 3
    assert result.candidate_count == 3
    assert [c.number for c in result.dispatchable] == [1, 2]
    assert [(c.target_key, d) for c, d in result.rows] == [
        ("acme/widgets#3", "over_cap")
    ]
    # The helper is pure: Classification itself is frozen.
    params = result.__dataclass_params__
    assert params.frozen is True

    # Fail closed: a `None` scan result propagates as `None` and no later stage runs.
    order.clear()
    gh_fail = ReviewGh(search_fail=True)
    assert (
        classify_review_candidates(
            gh_fail,
            job,
            login="me",
            history_path=history_path,
            trusted_authors=trusted,
        )
        is None
    )
    assert order == ["scan_review_targets"]


def test_classify_review_candidates_rows_reproduce_tick_order_and_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Classification.rows` reproduces `_review_cycle`'s pre-dispatch row contract
    exactly: scan skips, then max_attempts_exceeded skips, then review_already_posted
    skips, then over_cap — each with the candidate's own `skip_reason`, falling back to
    the four literal strings the tick has always used when a stage hands back a skip
    with no reason."""
    from herdr_routines import review_scan as rs
    from herdr_routines.review_scan import ScanOutcome, classify_review_candidates

    job = _review_job(tmp_path, max_workers_per_tick=1)
    history_path = tmp_path / "state" / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    # --- part 1: the real pipeline, all four stages with their own skip_reasons.
    gh = ReviewGh(
        prs=(
            _pr(1, created_at="2026-10-01T00:00:00Z"),
            _pr(2, created_at="2026-10-02T00:00:00Z"),
            _pr(3, created_at="2026-10-03T00:00:00Z"),
            _pr(4, created_at="2026-10-04T00:00:00Z"),
            # Scan skips: sorted by target_key, in front of every later stage's rows.
            _pr(5, author="someone-else"),
            _pr(6, head_ref="auto/nightly"),
            _pr(7, head_repo="someone/widgets"),
        )
    )
    gh.posted.add("acme/widgets#2")
    append(
        history_path,
        HistoryRecord(
            ts=NOW,
            job=job.name,
            state="done",
            run_id="prior",
            extra={"target_key": "acme/widgets#1"},
        ),
    )

    result = classify_review_candidates(
        gh,
        job,
        login="me",
        history_path=history_path,
        trusted_authors=frozenset({"me"}),
    )
    assert result is not None
    assert [(c.target_key, d) for c, d in result.rows] == [
        ("acme/widgets#5", "author_mismatch"),
        ("acme/widgets#6", "routine_owned_branch"),
        ("acme/widgets#7", "fork_pr"),
        ("acme/widgets#1", "max_attempts_exceeded"),
        ("acme/widgets#2", "review_already_posted"),
        ("acme/widgets#4", "over_cap"),
    ]
    assert [c.number for c in result.dispatchable] == [3]
    # `enumerated` and `candidate_count` are the post-scan, pre-budget numbers the tick
    # already writes as `enumerated=` / `candidates=`.
    assert result.enumerated == 7
    assert result.candidate_count == 4

    # --- part 2: the four fallback strings, for skips that carry no `skip_reason`.
    monkeypatch.setattr(
        rs,
        "scan_review_targets",
        lambda _gh, _job, *, login="", limit=100: ScanOutcome(
            candidates=(
                _mk_candidate("acme/z#9"),
                _mk_candidate("acme/z#10"),
                _mk_candidate("acme/z#11"),
            ),
            skipped=(_mk_candidate("acme/z#8"),),
            enumerated=4,
        ),
    )
    monkeypatch.setattr(
        rs,
        "apply_attempt_budget",
        lambda _h, _j, cands, **_k: (
            list(cands[1:]),
            [dataclasses.replace(cands[0], skip_reason=None)],
        ),
    )
    monkeypatch.setattr(
        rs,
        "apply_marker_guard",
        lambda _g, _j, cands, **_k: (
            list(cands[1:]),
            [dataclasses.replace(cands[0], skip_reason=None)],
        ),
    )
    monkeypatch.setattr(
        rs,
        "cap_candidates",
        lambda cands, _mw: ([], [dataclasses.replace(cands[0], skip_reason=None)]),
    )

    fallback = classify_review_candidates(
        ReviewGh(),
        job,
        login="me",
        history_path=history_path,
        trusted_authors=frozenset({"me"}),
    )
    assert fallback is not None
    assert fallback.dispatchable == ()
    assert [(c.target_key, d) for c, d in fallback.rows] == [
        ("acme/z#8", "skipped"),
        ("acme/z#9", "max_attempts_exceeded"),
        ("acme/z#10", "review_already_posted"),
        ("acme/z#11", "over_cap"),
    ]
    assert fallback.enumerated == 4
    assert fallback.candidate_count == 3


_TARGET_LINE = re.compile(r"^- ([\w/.-]+#\d+): (.+)$")


def test_review_scan_drift_tick_report_matches_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drift test: `classify_review_candidates` called directly, and `_review_cycle`
    run with the same fake `GhClient` (`default_reports_dir` → `tmp_path`), produce the
    same pre-dispatch target lines in the same order — the tick's report rows are the
    shared helper's rows, then the post-dispatch appends."""
    from herdr_routines.auto_fix import effective_trusted_authors
    from herdr_routines.review_scan import classify_review_candidates

    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    base = tmp_path / "repos"
    origin, _checkout = _setup_repo(tmp_path, "acme", "widgets", "feature-x", base)
    job = _review_job(tmp_path, repo=base, max_workers_per_tick=1)
    history = tmp_path / "state" / "history.jsonl"
    # #1 is at its attempt budget, #2 already carries a trusted review marker, and the
    # one-worker cap starves #4 while #3 would dispatch.
    append(
        history,
        HistoryRecord(
            ts=NOW,
            job=job.name,
            state="done",
            run_id="prior",
            extra={"target_key": "acme/widgets#1"},
        ),
    )
    gh = ReviewGh(
        prs=(
            _pr(1, created_at="2026-10-01T00:00:00Z"),
            _pr(2, created_at="2026-10-02T00:00:00Z"),
            _pr(3, created_at="2026-10-03T00:00:00Z"),
            _pr(4, created_at="2026-10-04T00:00:00Z"),
        ),
        repo_infos={
            ("acme", "widgets"): {"clone_url": str(origin), "default_branch": "main"}
        },
    )
    gh.posted.add("acme/widgets#2")

    classification = classify_review_candidates(
        gh,
        job,
        login=gh.user,
        history_path=history,
        trusted_authors=effective_trusted_authors((), viewer_login=gh.user),
    )
    assert classification is not None

    monkeypatch.setattr(tick, "RealGhClient", lambda: gh)
    client = ReviewHerdrClient(on_prompt=_mark_posted(gh))
    _summary, _failed = _run_cycle(job, history, gh, client, run_id="drift1")

    report = (tick.default_reports_dir() / "review-drift1.md").read_text()
    target_lines = [
        m.groups() for m in map(_TARGET_LINE.match, report.splitlines()) if m
    ]
    expected = [(c.target_key, d) for c, d in classification.rows]
    assert expected == [
        ("acme/widgets#1", "max_attempts_exceeded"),
        ("acme/widgets#2", "review_already_posted"),
        ("acme/widgets#4", "over_cap"),
    ]
    assert target_lines == [
        *expected,
        ("acme/widgets#3", "dispatched"),
    ]
