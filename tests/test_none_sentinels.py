"""Absent values are ``None``, not ``""``: the review-search parser, the PR lister, the
audit prompt builder and the job loader's ``prompt`` / ``fix_prompt``."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest
import yaml

from herdr_routines.auto_fix import (
    PRInfo,
    ReviewPR,
    _parse_review_node,
    build_audit_prompt,
    list_open_prs,
)
from herdr_routines.config import ConfigError, load_config
from herdr_routines.review_scan import _skip_reason


def _node(**over: object) -> dict[str, object]:
    node: dict[str, object] = {
        "number": 7,
        "title": "t",
        "url": "https://example.invalid/7",
        "createdAt": "2026-10-01T00:00:00Z",
        "updatedAt": "2026-10-01T00:00:00Z",
        "headRefName": "feature-x",
        "author": {"login": "me"},
        "repository": {"nameWithOwner": "acme/widgets"},
        "headRepository": {"nameWithOwner": "acme/widgets"},
    }
    node.update(over)
    return node


def _parsed(**over: object) -> ReviewPR:
    pr = _parse_review_node(_node(**over), "author")
    assert pr is not None
    return pr


def test_parse_review_node_present_values_are_unchanged() -> None:
    pr = _parsed()
    assert (pr.author, pr.repo, pr.head_repo) == ("me", "acme/widgets", "acme/widgets")


@pytest.mark.parametrize("head_repository", [None, {}, {"nameWithOwner": None}])
def test_null_head_repository_reads_as_unknown_and_is_a_fork(
    head_repository: object,
) -> None:
    pr = _parsed(headRepository=head_repository)
    assert pr.head_repo is None
    assert _skip_reason(pr, login="me", include_auto_branches=False) == "fork_pr"


@pytest.mark.parametrize("author", [None, {}, {"login": None}])
def test_null_author_reads_as_unknown(author: object) -> None:
    pr = _parsed(author=author)
    assert pr.author is None
    # author mode: someone else's (deleted) PR never matches the operator login.
    assert (
        _skip_reason(pr, login="me", include_auto_branches=False) == "author_mismatch"
    )


def test_null_author_is_kept_in_review_requested_mode() -> None:
    pr = _parse_review_node(_node(author=None), "review_requested")
    assert pr is not None
    assert pr.author is None
    assert _skip_reason(pr, login="me", include_auto_branches=False) is None


@pytest.mark.parametrize(
    "over",
    [
        {"repository": None},
        {"repository": {"nameWithOwner": ""}},
        {"headRefName": ""},
    ],
)
def test_node_without_repo_or_head_ref_is_dropped(over: dict[str, object]) -> None:
    assert _parse_review_node(_node(**over), "author") is None


class _ListGh:
    def __init__(self, prs: list[dict[str, object]]) -> None:
        self._prs = prs

    def pr_list(self, **_: Any) -> list[dict[str, object]]:
        return self._prs


def _listed(pr: dict[str, object]) -> list[PRInfo]:
    return list_open_prs(
        _ListGh([pr]),  # type: ignore[arg-type]
        owner="o",
        repo="r",
        branch_prefix="auto/",
        author="testuser",
    )


def test_bot_pr_without_login_is_accepted_with_author_none() -> None:
    (info,) = _listed(
        {
            "number": 1,
            "headRefName": "auto/bot-fix",
            "author": {"is_bot": True},
            "url": "https://example.invalid/1",
            "isCrossRepository": False,
        }
    )
    assert info.author is None


def test_pr_without_login_is_rejected_when_not_a_bot() -> None:
    assert (
        _listed(
            {
                "number": 1,
                "headRefName": "auto/x",
                "author": {},
                "url": "https://example.invalid/1",
            }
        )
        == []
    )


@pytest.mark.parametrize("instructions", [None, "", "   \n"])
def test_audit_prompt_without_instructions_loads_the_skill(
    instructions: str | None,
) -> None:
    prompt = build_audit_prompt(
        skill="type-health",
        base="main",
        report_path="/r.md",
        findings_path="/f.json",
        instructions=instructions,
    )
    assert prompt.startswith("Run the `type-health` skill against this repository.")


def test_audit_prompt_with_instructions_is_inline() -> None:
    prompt = build_audit_prompt(
        skill="type-health",
        base="main",
        report_path="/r.md",
        findings_path="/f.json",
        instructions="scan it",
    )
    assert "audit described below" in prompt
    assert "scan it" in prompt


def _routine(**over: object) -> dict[str, Any]:
    job: dict[str, Any] = {
        "name": "j",
        "cron": "0 4 * * 1",
        "repo": "/repo/x",
        "workspace": "worktree",
        "base": "main",
    }
    job.update(over)
    return job


def _load(tmp_path: Path, job: dict[str, Any]) -> Any:
    path = tmp_path / "jobs.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "jobs": [job]}))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return load_config(path).job("j")


def test_omitted_and_empty_prompt_load_as_none(tmp_path: Path) -> None:
    assert _load(tmp_path, _routine()).prompt is None
    assert _load(tmp_path, _routine(prompt="")).prompt is None


def test_whitespace_prompt_stays_a_string(tmp_path: Path) -> None:
    assert _load(tmp_path, _routine(prompt="  ")).prompt == "  "
    assert _load(tmp_path, _routine(prompt="go")).prompt == "go"


def test_null_prompt_is_still_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="'prompt' must be a string"):
        _load(tmp_path, _routine(prompt=None))


def test_omitted_and_empty_fix_prompt_load_as_none(tmp_path: Path) -> None:
    assert _load(tmp_path, _routine()).fix_prompt is None
    assert _load(tmp_path, _routine(fix_prompt="")).fix_prompt is None
    assert _load(tmp_path, _routine(fix_prompt="fix it")).fix_prompt == "fix it"
