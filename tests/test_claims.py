"""Tests for the out-of-tree pick-feature claim store (issue 041)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from herdr_routines.claims import (
    claim_issue,
    is_claimed,
    load_claims,
    release_claim,
    save_claims,
)


def test_load_claims_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_claims(tmp_path / "claims.json") == {}


def test_claim_issue_then_is_claimed(tmp_path: Path) -> None:
    path = tmp_path / "claims.json"
    claim_issue(path, "001")
    assert is_claimed(path, "001")
    assert not is_claimed(path, "002")


def test_claim_issue_records_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "claims.json"
    now = datetime(2026, 9, 5, tzinfo=UTC)
    claim_issue(path, "001", now=now)
    claims = load_claims(path)
    assert claims["001"].claimed_at == now


def test_claim_issue_overwrites_prior_claim_for_same_id(tmp_path: Path) -> None:
    path = tmp_path / "claims.json"
    claim_issue(path, "001", now=datetime(2026, 9, 1, tzinfo=UTC))
    claim_issue(path, "001", now=datetime(2026, 9, 5, tzinfo=UTC))
    claims = load_claims(path)
    assert len(claims) == 1
    assert claims["001"].claimed_at == datetime(2026, 9, 5, tzinfo=UTC)


def test_release_claim_removes_it(tmp_path: Path) -> None:
    path = tmp_path / "claims.json"
    claim_issue(path, "001")
    release_claim(path, "001")
    assert not is_claimed(path, "001")


def test_release_claim_missing_id_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "claims.json"
    release_claim(path, "001")  # doesn't raise
    assert load_claims(path) == {}


def test_save_claims_no_leftover_tmp_file(tmp_path: Path) -> None:
    path = tmp_path / "claims.json"
    save_claims(path, {})
    assert not path.with_suffix(".json.tmp").exists()


def test_claims_persist_across_multiple_ids(tmp_path: Path) -> None:
    path = tmp_path / "claims.json"
    claim_issue(path, "001")
    claim_issue(path, "002")
    assert load_claims(path).keys() == {"001", "002"}
