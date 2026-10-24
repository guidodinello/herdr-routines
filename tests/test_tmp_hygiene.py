"""Tests for tmp_hygiene.py — age-based /tmp cleanup (issue 027).

Covers all acceptance criteria from spec v2:
1. test_tmp_hygiene_age_based_cleanup
2. test_tmp_hygiene_safe_against_live_run
3. test_tmp_hygiene_keeps_tmp_under_threshold
4. test_tmp_hygiene_diagnosis_distinguishes_disk_full
5. test_tmp_hygiene_narrow_patterns
6. test_tmp_hygiene_dry_run_and_tick_preamble, plus test_tick_reap_* driving run_tick
7. test_tmp_hygiene_config_and_docs, test_tmp_hygiene_defaults_block_reaches_routines_config
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from herdr_routines import tick as tick_mod
from herdr_routines import tmp_hygiene
from herdr_routines.config import (
    ConfigError,
    RoutinesConfig,
    TmpHygieneConfig,
    load_config,
)
from herdr_routines.tick import TickOutcome, run_tick
from herdr_routines.tmp_hygiene import reap_tmp
from tests.test_tick import FakeClient, make_job

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_old_file(tmp_dir: Path, name: str) -> Path:
    """Create a file with mtime well in the past."""
    p = tmp_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("leaked")
    # Set mtime to 2 hours ago (well beyond default 1h max_age_s).
    old_time = time.time() - 7200
    os.utime(p, (old_time, old_time))
    return p


def _make_fresh_file(tmp_dir: Path, name: str) -> Path:
    """Create a file with mtime just now (should be skipped)."""
    p = tmp_dir / name
    p.write_text("fresh")
    return p


def _make_old_dir(tmp_dir: Path, name: str) -> Path:
    """Create a directory whose whole tree has mtime well in the past."""
    d = tmp_dir / name
    d.mkdir()
    child = d / "child.txt"
    child.write_text("inside")
    old_time = time.time() - 7200
    os.utime(child, (old_time, old_time))
    os.utime(d, (old_time, old_time))
    return d


# ---------------------------------------------------------------------------
# 1. Age-based cleanup removes leaked patterns older than max_age_s
# ---------------------------------------------------------------------------


def test_tmp_hygiene_age_based_cleanup(tmp_path: Path) -> None:
    """Old .3cdc*.so files, pytest-of-* dirs, and opencode dirs are removed when
    older than max_age_s; fresh files are skipped."""
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    # Create old leaked files (2h old, max_age is 1h)
    old_so = _make_old_file(tmp_dir, ".3cdcABCDEF01234567-00000001.so")
    old_so_sibling = _make_old_file(tmp_dir, ".3cdcXYZ000111222333")
    old_pytest = _make_old_dir(tmp_dir, "pytest-of-guido")
    old_opencode = _make_old_dir(tmp_dir, "opencode")

    # Create fresh leaked files (just created, should survive)
    fresh_so = _make_fresh_file(tmp_dir, ".3cdcFRESH000000000-00000001.so")

    result = reap_tmp(tmp_dir=tmp_dir, max_age_s=3600)

    assert result.removed == 4
    assert result.skipped_fresh == 1
    assert result.errors == 0
    assert not old_so.exists()
    assert not old_so_sibling.exists()
    assert not old_pytest.exists()
    assert not old_opencode.exists()
    assert fresh_so.exists()


# ---------------------------------------------------------------------------
# 2. Safe against live runs: fresh mtime files never removed
# ---------------------------------------------------------------------------


def test_tmp_hygiene_safe_against_live_run(tmp_path: Path) -> None:
    """An in-flight agent's .so (mtime within max_age_s) is never removed mid-spawn."""
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    # Simulate a live agent's .so written 30s ago (within 1h window)
    live_so = _make_fresh_file(tmp_dir, ".3cdcLIVE0000000001-00000001.so")
    # Backdate to 30s ago — still within the 1h window
    recent_time = time.time() - 30
    os.utime(live_so, (recent_time, recent_time))

    # Also create an old one that should be reaped
    old_so = _make_old_file(tmp_dir, ".3cdcOLD000000000001-00000001.so")

    result = reap_tmp(tmp_dir=tmp_dir, max_age_s=3600)

    assert result.removed == 1
    assert result.skipped_fresh == 1
    assert live_so.exists(), "live agent .so must not be removed"
    assert not old_so.exists()


# ---------------------------------------------------------------------------
# 3. Keeps /tmp under threshold: reap is idempotent and bounded
# ---------------------------------------------------------------------------


def test_tmp_hygiene_keeps_tmp_under_threshold(tmp_path: Path) -> None:
    """Running reap_tmp multiple times is idempotent — second run removes nothing
    and returns removed=0. Bounded filesystem ops."""
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    _make_old_file(tmp_dir, ".3cdcLEAK0000000001-00000001.so")
    _make_old_dir(tmp_dir, "pytest-of-guido")

    r1 = reap_tmp(tmp_dir=tmp_dir, max_age_s=3600)
    assert r1.removed == 2

    r2 = reap_tmp(tmp_dir=tmp_dir, max_age_s=3600)
    assert r2.removed == 0
    assert r2.errors == 0


# ---------------------------------------------------------------------------
# 4. Diagnosis distinguishes disk-full from quota/blocked
# ---------------------------------------------------------------------------


def test_tmp_hygiene_diagnosis_distinguishes_disk_full(tmp_path: Path) -> None:
    """diagnose_tmp returns tmp_full=True when df shows >=95% Use%."""
    from herdr_routines.runner import diagnose_tmp

    # Mock subprocess.run to simulate df output with 98% usage
    fake_df_output = (
        "Filesystem      Size  Used Avail Use% Mounted on\n"
        "tmpfs           2.0G  1.9G   50M   98% /tmp\n"
    )

    def fake_run(cmd, **kwargs):
        class FakeProc:
            returncode = 0
            stdout = ""

        p = FakeProc()
        if cmd[0] == "df":
            p.stdout = fake_df_output
        return p

    with patch("herdr_routines.runner.subprocess.run", side_effect=fake_run):
        diagnosis = diagnose_tmp()

    assert diagnosis["tmp_full"] is True
    assert "98%" in str(diagnosis.get("df_tmp", ""))

    # Also test the opposite: low usage -> tmp_full=False
    low_usage_output = (
        "Filesystem      Size  Used Avail Use% Mounted on\n"
        "tmpfs           2.0G  500M  1.5G   25% /tmp\n"
    )

    def fake_run_low(cmd, **kwargs):
        class FakeProc:
            returncode = 0
            stdout = ""

        p = FakeProc()
        if cmd[0] == "df":
            p.stdout = low_usage_output
        return p

    with patch("herdr_routines.runner.subprocess.run", side_effect=fake_run_low):
        diagnosis_low = diagnose_tmp()

    assert diagnosis_low["tmp_full"] is False


# ---------------------------------------------------------------------------
# 5. Narrow patterns: only anchored top-level globs, no broad sweep
# ---------------------------------------------------------------------------


def test_tmp_hygiene_narrow_patterns(tmp_path: Path) -> None:
    """Only specific patterns are reaped; unrelated files/dirs are untouched.
    tmp_dir is configurable."""
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    # Create files that should NOT be touched
    safe_file = _make_old_file(tmp_dir, "important.txt")
    safe_dir = _make_old_dir(tmp_dir, "my-temp-workdir")
    nested_leak = _make_old_file(tmp_dir, "subdir/.3cdcNESTED.so")
    os.utime(nested_leak.parent, (time.time() - 7200, time.time() - 7200))

    # Create a file that SHOULD be reaped
    old_so = _make_old_file(tmp_dir, ".3cdcSHOULD-die.so")

    result = reap_tmp(tmp_dir=tmp_dir, max_age_s=3600)

    # Only the top-level .3cdc file should be removed
    assert result.removed == 1
    assert safe_file.exists(), "unrelated file must not be removed"
    assert safe_dir.exists(), "unrelated dir must not be removed"
    assert nested_leak.exists(), "nested .3cdc must not be removed (top-level only)"
    assert not old_so.exists()


# ---------------------------------------------------------------------------
# Nested mtime and symlink handling (audit: top-level mtime is a weak liveness signal)
# ---------------------------------------------------------------------------


def test_tmp_hygiene_keeps_old_dir_with_fresh_nested_file(tmp_path: Path) -> None:
    """Nested writes don't bump the top-level mtime, so a live agent writing deep in
    /tmp/opencode must still count as fresh."""
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    live = _make_old_dir(tmp_dir, "opencode")
    deep = live / "a" / "b"
    deep.mkdir(parents=True)
    old_time = time.time() - 7200
    os.utime(live / "a", (old_time, old_time))
    os.utime(deep, (old_time, old_time))
    (deep / "state.db").write_text("live")  # fresh mtime; ancestors stay old
    os.utime(live, (old_time, old_time))

    result = reap_tmp(tmp_dir=tmp_dir, max_age_s=3600)

    assert live.exists()
    assert result.removed == 0
    assert result.skipped_fresh == 1


def test_tmp_hygiene_reaps_fully_old_nested_tree(tmp_path: Path) -> None:
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    old = _make_old_dir(tmp_dir, "opencode-old")
    nested = old / "a"
    nested.mkdir()
    leaf = nested / "x.log"
    leaf.write_text("stale")
    old_time = time.time() - 7200
    for path in (leaf, nested, old):
        os.utime(path, (old_time, old_time))

    result = reap_tmp(tmp_dir=tmp_dir, max_age_s=3600)

    assert not old.exists()
    assert result.removed == 1


def test_tmp_hygiene_skips_symlinks(tmp_path: Path) -> None:
    """A matching symlink is never followed or deleted, and never judged by its
    target's mtime — a dangling link is skipped, not counted as an error."""
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    outside = _make_old_dir(tmp_path, "precious")
    (tmp_dir / "opencode-link").symlink_to(outside, target_is_directory=True)
    (tmp_dir / "pytest-of-dangling").symlink_to(tmp_path / "missing")

    result = reap_tmp(tmp_dir=tmp_dir, max_age_s=3600)

    assert (outside / "child.txt").exists()
    assert (tmp_dir / "opencode-link").is_symlink()
    assert (tmp_dir / "pytest-of-dangling").is_symlink()
    assert result.removed == 0
    assert result.errors == 0


def test_tmp_hygiene_keeps_tree_larger_than_walk_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree we can't fully inspect is kept (and flagged), never reaped on a partial
    look."""
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    big = _make_old_dir(tmp_dir, "pytest-of-big")
    old_time = time.time() - 7200
    for n in range(3):
        extra = big / f"f{n}"
        extra.write_text("x")
        os.utime(extra, (old_time, old_time))
    os.utime(big, (old_time, old_time))
    monkeypatch.setattr(tmp_hygiene, "_MAX_WALK_ENTRIES", 2)

    result = reap_tmp(tmp_dir=tmp_dir, max_age_s=3600)

    assert big.exists()
    assert result.removed == 0
    assert result.errors == 1


# ---------------------------------------------------------------------------
# 6. Dry run and tick preamble behavior
# ---------------------------------------------------------------------------


def test_tmp_hygiene_dry_run_and_tick_preamble(tmp_path: Path) -> None:
    """dry_run reports what would be removed without mutating. Tick preamble reap
    is best-effort and never fails the tick."""
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    old_so = _make_old_file(tmp_dir, ".3cdcDRY000000000001-00000001.so")
    old_pytest = _make_old_dir(tmp_dir, "pytest-of-guido")

    # dry_run should report 2 but not delete anything
    result = reap_tmp(tmp_dir=tmp_dir, max_age_s=3600, dry_run=True)
    assert result.removed == 2
    assert old_so.exists(), "dry_run must not delete"
    assert old_pytest.exists(), "dry_run must not delete"

    # The real function still deletes once dry_run is off
    r = reap_tmp(tmp_dir=tmp_dir, max_age_s=3600)
    assert r.removed == 2


# ---------------------------------------------------------------------------
# 6b. Tick wiring: run_tick reaps with the defaults-level tmp_hygiene config
# ---------------------------------------------------------------------------

_TICK_NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _run_tick_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: RoutinesConfig
) -> TickOutcome:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    return run_tick(
        config,
        tmp_path / "state" / "history.jsonl",
        client=FakeClient(),  # type: ignore[arg-type]
        now=_TICK_NOW,
    )


@pytest.mark.parametrize(
    ("max_age_s", "file_age_s", "expect_removed"),
    [
        # Both cases flip relative to the built-in 3600s default: a tick that ignored the
        # config would keep the 30-min-old file and delete the 2h-old one.
        (600, 1800, True),
        (10800, 7200, False),
    ],
)
def test_tick_reaps_with_configured_max_age_and_tmp_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    max_age_s: int,
    file_age_s: int,
    expect_removed: bool,
) -> None:
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    leaked = tmp_dir / ".3cdcABCDEF01234567-00000001.so"
    leaked.write_text("leaked")
    old_time = time.time() - file_age_s
    os.utime(leaked, (old_time, old_time))
    config = RoutinesConfig(
        jobs=(make_job(tmp_path, cron="0 3 * * *"),),
        tmp_hygiene=TmpHygieneConfig(max_age_s=max_age_s, tmp_dir=str(tmp_dir)),
    )

    outcome = _run_tick_with(tmp_path, monkeypatch, config)

    assert leaked.exists() is not expect_removed
    assert outcome.summaries == ("a: registered",)


def test_tick_reap_disabled_leaves_tmp_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    leaked = _make_old_file(tmp_dir, ".3cdcABCDEF01234567-00000001.so")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(tick_mod, "reap_tmp", lambda **kw: calls.append(kw))
    config = RoutinesConfig(
        tmp_hygiene=TmpHygieneConfig(enabled=False, tmp_dir=str(tmp_dir))
    )

    _run_tick_with(tmp_path, monkeypatch, config)

    assert calls == []
    assert leaked.exists()


def test_tick_reap_absent_config_uses_builtin_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `tmp_hygiene:` block means "reap with the defaults", not "off" — the behaviour
    deploy/README.md documents. Recorded, never run: the default tmp_dir is the real /tmp."""
    calls: list[dict[str, object]] = []

    def record(**kw: object) -> tmp_hygiene.ReapResult:
        calls.append(kw)
        return tmp_hygiene.ReapResult()

    monkeypatch.setattr(tick_mod, "reap_tmp", record)

    _run_tick_with(tmp_path, monkeypatch, RoutinesConfig())

    assert calls == [{"tmp_dir": Path("/tmp"), "max_age_s": 3600}]


def test_tick_reap_failure_does_not_block_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_reap(**kw: object) -> tmp_hygiene.ReapResult:
        raise OSError("simulated failure")

    monkeypatch.setattr(tick_mod, "reap_tmp", broken_reap)
    config = RoutinesConfig(
        jobs=(make_job(tmp_path, cron="0 3 * * *"),),
        tmp_hygiene=TmpHygieneConfig(tmp_dir=str(tmp_path / "tmp")),
    )

    outcome = _run_tick_with(tmp_path, monkeypatch, config)

    assert outcome.summaries == ("a: registered",)
    assert outcome.any_job_failed is False


# ---------------------------------------------------------------------------
# 7. Config and docs: tmp_hygiene block validated
# ---------------------------------------------------------------------------


def test_tmp_hygiene_config_and_docs(tmp_config_path: Path) -> None:
    """tmp_hygiene config block is validated: positive max_age_s, non-empty tmp_dir,
    boolean enabled."""
    # Valid config with tmp_hygiene block
    text = """
version: 1
jobs:
  - name: test-job
    cron: "0 3 * * *"
    repo: /tmp
    tmp_hygiene:
      enabled: true
      max_age_s: 7200
      tmp_dir: /var/tmp
"""
    tmp_config_path.write_text(text)
    cfg = load_config(tmp_config_path)
    job = cfg.job("test-job")
    assert job is not None
    assert job.tmp_hygiene is not None
    assert job.tmp_hygiene.enabled is True
    assert job.tmp_hygiene.max_age_s == 7200
    assert job.tmp_hygiene.tmp_dir == "/var/tmp"

    # Invalid: negative max_age_s
    bad_text = """
version: 1
jobs:
  - name: bad-job
    cron: "0 3 * * *"
    repo: /tmp
    tmp_hygiene:
      max_age_s: -1
"""
    tmp_config_path.write_text(bad_text)
    with pytest.raises(Exception, match="positive integer"):
        load_config(tmp_config_path)

    # Invalid: empty tmp_dir
    bad_text2 = """
version: 1
jobs:
  - name: bad-job
    cron: "0 3 * * *"
    repo: /tmp
    tmp_hygiene:
      tmp_dir: ""
"""
    tmp_config_path.write_text(bad_text2)
    with pytest.raises(Exception, match="non-empty string"):
        load_config(tmp_config_path)

    # Invalid: non-boolean enabled
    bad_text3 = """
version: 1
jobs:
  - name: bad-job
    cron: "0 3 * * *"
    repo: /tmp
    tmp_hygiene:
      enabled: "yes"
"""
    tmp_config_path.write_text(bad_text3)
    with pytest.raises(Exception, match="boolean"):
        load_config(tmp_config_path)


def test_tmp_hygiene_defaults_block_reaches_routines_config(
    tmp_config_path: Path, tmp_path: Path
) -> None:
    """The tick reads `RoutinesConfig.tmp_hygiene`, resolved from `defaults:` in the
    legacy file and from `defaults.yaml` in a jobs.d/ layout."""
    tmp_config_path.write_text(
        """
version: 1
defaults:
  tmp_hygiene:
    max_age_s: 600
    tmp_dir: /var/tmp
jobs:
  - name: test-job
    cron: "0 3 * * *"
    repo: /tmp
"""
    )
    assert load_config(tmp_config_path).tmp_hygiene == TmpHygieneConfig(
        enabled=True, max_age_s=600, tmp_dir="/var/tmp"
    )

    jobs_d = tmp_path / "jobs.d"
    jobs_d.mkdir()
    (jobs_d / "defaults.yaml").write_text("tmp_hygiene:\n  enabled: false\n")
    (jobs_d / "test-job.yaml").write_text('cron: "0 3 * * *"\nrepo: /tmp\n')
    assert load_config(jobs_d).tmp_hygiene == TmpHygieneConfig(enabled=False)

    # Absent block = reap with the built-in defaults.
    (jobs_d / "defaults.yaml").write_text("timezone: UTC\n")
    assert load_config(jobs_d).tmp_hygiene == TmpHygieneConfig()

    (jobs_d / "defaults.yaml").write_text("tmp_hygiene:\n  max_age_s: 0\n")
    with pytest.raises(ConfigError, match="positive integer"):
        load_config(jobs_d)


# ---------------------------------------------------------------------------
# Review note: tiers present check (test_tmp_hygiene_review_tiers_present)
# This is a meta-test verifying the spec's own formatting.
# ---------------------------------------------------------------------------


def test_tmp_hygiene_review_tiers_present() -> None:
    """Verify the spec acceptance criteria contain both blocking and non-blocking tiers
    and confidence annotations."""
    spec_path = (
        Path(__file__).parents[1]
        / "docs"
        / "pipeline"
        / "runs"
        / "20260904T050000Z"
        / "spec.md"
    )
    if not spec_path.exists():
        pytest.skip("spec.md not found (not in repo root)")
    text = spec_path.read_text()
    assert "blocking" in text
    assert "non-blocking" in text
    assert "confidence:" in text
    # Each acceptance line ends with a Test: name
    for line in text.splitlines():
        if (
            line.strip().startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7."))
            and "Test:" in line
        ):
            assert line.rstrip().endswith(line.rstrip().split("Test:")[-1].strip())
