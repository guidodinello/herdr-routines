"""Tests for the out-of-tree pick-feature claim store (issue 041)."""

from __future__ import annotations

import fcntl
import logging
import multiprocessing
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from herdr_routines.claims import (
    claim_issue,
    is_claimed,
    load_claims,
    locked_claims,
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
    assert list(tmp_path.glob("*.tmp")) == []


def test_claims_persist_across_multiple_ids(tmp_path: Path) -> None:
    path = tmp_path / "claims.json"
    claim_issue(path, "001")
    claim_issue(path, "002")
    assert load_claims(path).keys() == {"001", "002"}


def _claim_in_subprocess(path: Path, issue_id: str) -> None:
    claim_issue(path, issue_id)


def test_concurrent_writers_all_land(tmp_path: Path) -> None:
    """Unlocked read-modify-write loses claims when writers interleave."""
    path = tmp_path / "claims.json"
    ids = [f"{n:03d}" for n in range(12)]
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_claim_in_subprocess, args=(path, i)) for i in ids]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0
    assert load_claims(path).keys() == set(ids)
    assert list(tmp_path.glob("*.tmp")) == []


def test_claim_issue_waits_for_the_store_lock(tmp_path: Path) -> None:
    path = tmp_path / "claims.json"
    lock_fd = os.open(tmp_path / "claims.json.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        writer = threading.Thread(target=claim_issue, args=(path, "001"))
        writer.start()
        writer.join(timeout=0.5)
        assert writer.is_alive()
        assert not path.exists()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    writer.join(timeout=10)
    assert is_claimed(path, "001")


def test_locked_claims_does_not_save_when_block_raises(tmp_path: Path) -> None:
    path = tmp_path / "claims.json"
    claim_issue(path, "001")
    with pytest.raises(RuntimeError), locked_claims(path) as claims:
        claims.clear()
        raise RuntimeError("boom")
    assert is_claimed(path, "001")


@pytest.mark.parametrize(
    "content",
    ["{not json", '{"001": {}}', '{"001": {"claimed_at": "yesterday"}}', "[1, 2]"],
)
def test_load_claims_corrupt_file_is_empty_and_left_in_place(
    tmp_path: Path, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "claims.json"
    path.write_text(content)
    with caplog.at_level(logging.WARNING):
        assert load_claims(path) == {}
    assert "corrupt" in caplog.text
    assert path.read_text() == content


def test_claim_issue_quarantines_a_corrupt_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "claims.json"
    path.write_text("{truncated")
    with caplog.at_level(logging.WARNING):
        claim_issue(path, "001")
    assert load_claims(path).keys() == {"001"}
    [aside] = tmp_path.glob("claims.json.corrupt-*")
    assert aside.read_text() == "{truncated"
    assert str(aside) in caplog.text
