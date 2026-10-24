"""Tests for the host drift check (`host_drift.check_host`) and its three surfaces:
`herdr-routines host-check`, the self-update notification and the daily digest.

Git is real, as in `tests/test_self_update.py` (whose `Sandbox` this reuses): the
`runner_behind` finding is only meaningful against a real `origin/<base>` ref and a real
FETCH_HEAD. Only `systemctl --user` is faked, through the same `runner=` seam the check
uses for git — `FakeSystemctl` answers `systemctl` and passes `git` through.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import cli
from herdr_routines.host_drift import DriftFinding, check_host
from tests.test_digest import write_config
from tests.test_self_update import FakeValidate, Recorder, Sandbox, _commit, _git

TIMER = (
    "[Unit]\nDescription=t\n\n[Timer]\nOnCalendar=daily\n\n"
    "[Install]\nWantedBy=timers.target\n"
)
# A timer-driven service: no [Install], so it is never expected to be enabled.
SERVICE = "[Unit]\nDescription=s\n\n[Service]\nType=oneshot\nExecStart=/bin/true\n"
# A plain service that must be enabled itself (the herdr-server.service shape).
SERVER = (
    "[Unit]\nDescription=srv\n\n[Service]\nExecStart=/bin/true\n\n"
    "[Install]\nWantedBy=default.target\n"
)
UNITS = {"job.timer": TIMER, "job.service": SERVICE, "server.service": SERVER}


class FakeSystemctl:
    """The `runner=` seam: answers `systemctl --user show` from `states` (default
    enabled/active), passes every `git` call to the real subprocess.run, and can be
    told to raise instead (a missing binary, a hung bus)."""

    def __init__(
        self,
        states: dict[str, tuple[str, str]] | None = None,
        *,
        raise_with: BaseException | None = None,
    ) -> None:
        self.states = states or {}
        self.raise_with = raise_with
        self.shown: list[str] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        if argv[0] != "systemctl":
            kwargs.pop("check", None)
            return subprocess.run(argv, check=False, **kwargs)
        if self.raise_with is not None:
            raise self.raise_with
        unit = argv[-1]
        self.shown.append(unit)
        enabled, active = self.states.get(unit, ("enabled", "active"))
        return subprocess.CompletedProcess(
            argv, 0, f"ActiveState={active}\nUnitFileState={enabled}\n", ""
        )


def _ship_units(sb: Sandbox, units: dict[str, str] = UNITS) -> None:
    """Commit the units into the runner checkout and push, so it is neither dirty
    nor ahead — and fetch, so FETCH_HEAD is fresh."""
    for name, text in units.items():
        _commit(sb.checkout, f"deploy/systemd/{name}", text, f"add {name}")
    _git(sb.checkout, "push", "origin", f"HEAD:{sb.base}")
    _git(sb.checkout, "fetch", "origin")


def _install(sb: Sandbox, unit_dir: Path) -> None:
    unit_dir.mkdir(parents=True, exist_ok=True)
    for unit in (sb.checkout / "deploy" / "systemd").iterdir():
        shutil.copy(unit, unit_dir / unit.name)


@pytest.fixture
def host(tmp_path: Path) -> tuple[Sandbox, Path]:
    """A runner checkout whose units are all installed verbatim."""
    sb = Sandbox(tmp_path / "repo")
    _ship_units(sb)
    unit_dir = tmp_path / "units"
    _install(sb, unit_dir)
    return sb, unit_dir


def _check(sb: Sandbox, unit_dir: Path, **kwargs: Any) -> list[DriftFinding]:
    kwargs.setdefault("runner", FakeSystemctl())
    return check_host(sb.checkout, base=sb.base, unit_dir=unit_dir, **kwargs)


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------


def test_clean_host_has_no_findings(host: tuple[Sandbox, Path]) -> None:
    sb, unit_dir = host
    runner = FakeSystemctl()
    assert _check(sb, unit_dir, runner=runner) == []
    # Only the units with [Install] are asked about; job.service is its timer's job.
    assert sorted(runner.shown) == ["job.timer", "server.service"]


def test_uninstalled_unit_is_missing(host: tuple[Sandbox, Path]) -> None:
    """Incident 1: the digest timer was never copied in."""
    sb, unit_dir = host
    (unit_dir / "job.timer").unlink()
    findings = _check(sb, unit_dir)
    assert [f.kind for f in findings] == ["missing_unit"]
    assert "job.timer" in findings[0].message


def test_installed_but_not_enabled_is_missing(host: tuple[Sandbox, Path]) -> None:
    sb, unit_dir = host
    runner = FakeSystemctl(
        {"job.timer": ("disabled", "inactive"), "server.service": ("enabled", "failed")}
    )
    findings = _check(sb, unit_dir, runner=runner)
    assert [f.kind for f in findings] == ["missing_unit", "missing_unit"]
    assert "disabled/inactive" in findings[0].message
    assert "enable --now job.timer" in findings[0].message
    assert "enabled/failed" in findings[1].message
    assert "status server.service" in findings[1].message


def test_installed_copy_differing_from_repo_is_stale(
    host: tuple[Sandbox, Path],
) -> None:
    """Incident 3: the runner moved on, the installed unit did not."""
    sb, unit_dir = host
    (unit_dir / "job.service").write_text(SERVICE.replace("true", "false"))
    findings = _check(sb, unit_dir)
    assert [f.kind for f in findings] == ["stale_unit"]
    assert "job.service" in findings[0].message
    assert "daemon-reload" in findings[0].message


def test_runner_behind_origin_reports_count_and_age(
    host: tuple[Sandbox, Path],
) -> None:
    """Incident 2: commits on origin/<base> the runner does not have, as of the last
    fetch — and the check itself never fetches."""
    sb, unit_dir = host
    _git(sb.work, "pull", "origin", sb.base)
    sb.push("A.md", "a\n", "a")
    sb.push("B.md", "b\n", "b")
    _git(sb.checkout, "fetch", "origin")

    runner = FakeSystemctl()
    calls: list[list[str]] = []

    def recording(argv: list[str], **kwargs: Any) -> Any:
        calls.append(list(argv))
        return runner(argv, **kwargs)

    findings = _check(
        sb, unit_dir, runner=recording, now=datetime.now(UTC) + timedelta(hours=5)
    )
    assert [f.kind for f in findings] == ["runner_behind"]
    assert "2 commit(s) behind" in findings[0].message
    assert "oldest missing commit is 5h old" in findings[0].message
    assert not any("fetch" in argv for argv in calls)


def test_a_fetch_older_than_two_nights_is_reported(
    host: tuple[Sandbox, Path],
) -> None:
    """The tick-lock deferrals returned before self-update's fetch, so origin/<base>
    itself was stale and a behind count of 0 would have been a lie."""
    sb, unit_dir = host
    findings = _check(sb, unit_dir, now=datetime.now(UTC) + timedelta(days=3))
    assert [f.kind for f in findings] == ["runner_behind"]
    assert "last fetched 3d ago" in findings[0].message


def test_failed_probes_are_findings_not_exceptions(
    host: tuple[Sandbox, Path], tmp_path: Path
) -> None:
    sb, unit_dir = host
    for error in (
        FileNotFoundError("systemctl"),
        subprocess.TimeoutExpired("systemctl", 15),
    ):
        findings = _check(sb, unit_dir, runner=FakeSystemctl(raise_with=error))
        assert [f.kind for f in findings] == ["probe_failed", "probe_failed"]

    # Not a git checkout at all, and no deploy/systemd/ in it.
    findings = check_host(tmp_path / "nowhere", unit_dir=unit_dir)
    assert {f.kind for f in findings} == {"probe_failed"}


# ---------------------------------------------------------------------------
# surface 1: `herdr-routines host-check`
# ---------------------------------------------------------------------------


def _patch_cli_runner(
    monkeypatch: pytest.MonkeyPatch, runner: FakeSystemctl, **fixed: Any
) -> None:
    """Keep the CLI's own call to check_host, only swapping the probe seam."""
    monkeypatch.setattr(cli, "check_host", partial(check_host, runner=runner, **fixed))


def test_cli_host_check_exit_codes(
    host: tuple[Sandbox, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sb, unit_dir = host
    _patch_cli_runner(monkeypatch, FakeSystemctl())
    argv = [
        "host-check",
        "--path",
        str(sb.checkout),
        "--base",
        sb.base,
        "--unit-dir",
        str(unit_dir),
    ]

    assert cli.main(argv) == 0
    assert "ok: no host drift" in capsys.readouterr().out

    (unit_dir / "job.timer").unlink()
    assert cli.main(argv) == 1
    out = capsys.readouterr().out
    assert "- missing_unit: job.timer" in out


# ---------------------------------------------------------------------------
# surface 2: the self-update notification
# ---------------------------------------------------------------------------


def test_drift_is_notified_on_an_up_to_date_run(
    host: tuple[Sandbox, Path],
) -> None:
    """An up_to_date run is otherwise silent; drift must still get out."""
    sb, unit_dir = host
    (unit_dir / "job.timer").unlink()
    notify = Recorder()
    result = sb.run(
        notify=notify,
        host_check=partial(
            check_host,
            sb.checkout,
            base=sb.base,
            unit_dir=unit_dir,
            runner=FakeSystemctl(),
        ),
    )
    assert result.status == "up_to_date"
    assert len(notify.calls) == 1
    assert "host drift" in notify.titles
    assert "- missing_unit: job.timer" in notify.body

    # And a clean host stays quiet.
    _install(sb, unit_dir)
    notify = Recorder()
    sb.run(
        notify=notify,
        host_check=partial(
            check_host,
            sb.checkout,
            base=sb.base,
            unit_dir=unit_dir,
            runner=FakeSystemctl(),
        ),
    )
    assert notify.calls == []


def test_a_merged_unit_change_shows_as_stale_in_the_update_notification(
    host: tuple[Sandbox, Path],
) -> None:
    """Incident 3 end to end: self-update pulls a unit change, reports it as a deploy
    change, and — run after the fast-forward — the host check flags the installed
    copy as stale in that same notification."""
    sb, unit_dir = host
    _git(sb.work, "pull", "origin", sb.base)
    sb.push("deploy/systemd/job.timer", TIMER.replace("daily", "hourly"), "timer")

    notify = Recorder()
    result = sb.run(
        notify=notify,
        runner=FakeValidate(),
        host_check=partial(
            check_host,
            sb.checkout,
            base=sb.base,
            unit_dir=unit_dir,
            runner=FakeSystemctl(),
        ),
    )
    assert result.status == "updated"
    assert len(notify.calls) == 1
    assert "self-update" in notify.titles
    assert "deploy/systemd/job.timer" in notify.body
    assert "- stale_unit:" in notify.body
    assert "job.timer" in notify.body.split("host drift", 1)[1]


def test_host_check_is_skipped_on_dry_run_and_cannot_eat_a_refusal(
    host: tuple[Sandbox, Path],
) -> None:
    sb, _ = host
    calls: list[int] = []

    def counting() -> list[DriftFinding]:
        calls.append(1)
        return [DriftFinding("missing_unit", "x")]

    notify = Recorder()
    sb.run(notify=notify, dry_run=True, host_check=counting)
    assert calls == []
    assert notify.calls == []

    def crashing() -> list[DriftFinding]:
        raise RuntimeError("boom")

    # A crashing check is a finding, and the run itself is unaffected.
    notify = Recorder()
    assert sb.run(notify=notify, host_check=crashing).status == "up_to_date"
    assert "host check crashed: boom" in notify.body

    # A refusal is notified by the run itself, with no host check after it.
    (sb.checkout / "DIRTY.md").write_text("x\n")
    notify = Recorder()
    assert sb.run(notify=notify, host_check=counting).status == "refused"
    assert calls == []
    assert "self-update refused" in notify.titles


# ---------------------------------------------------------------------------
# surface 3: the daily digest
# ---------------------------------------------------------------------------


def test_digest_has_a_drift_section_only_when_there_is_drift(
    host: tuple[Sandbox, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sb, unit_dir = host
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state_dir))
    config_path = write_config(tmp_path, ["nightly"])
    _patch_cli_runner(monkeypatch, FakeSystemctl(), base=sb.base, unit_dir=unit_dir)
    argv = [
        "--config",
        str(config_path),
        "digest",
        "--timezone",
        "UTC",
        "--path",
        str(sb.checkout),
    ]

    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "nightly: never run" in out
    assert "host drift" not in out

    (unit_dir / "server.service").unlink()
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "nightly: never run" in out
    assert "host drift" in out
    assert "- missing_unit: server.service" in out
