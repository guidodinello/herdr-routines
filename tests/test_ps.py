"""Tier-1/2 tests for `herdr-routines ps` (spec 20260825T070012Z): the running table must
handle zero live agents gracefully, cross-reference in-progress pipeline state.json runs into
stage indicators instead of bare names, and stay strictly read-only. Mirrors the fake-client /
fake-CommandRunner patterns of test_tick.py / test_herdr.py."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from herdr_routines import cli
from herdr_routines.herdr import HerdrCliError
from herdr_routines.ps import build_ps_rows, scan_pipeline_runs

RUN_ID = "20260825T070012Z"


class FakeRunner:
    """Records argvs and returns canned responses (same shape as test_herdr.py's)."""

    def __init__(self, responses: list[tuple[int, str, str]]) -> None:
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], *, timeout_s: float | None
    ) -> tuple[int, str, str]:
        self.calls.append(argv)
        return self._responses.pop(0)


def ok(body: dict) -> tuple[int, str, str]:
    return 0, json.dumps(body), ""


class FakeStatusClient:
    def __init__(
        self, statuses: dict[str, str] | None = None, *, raise_error: bool = False
    ):
        self._statuses = statuses or {}
        self._raise_error = raise_error

    def agent_statuses(self) -> dict[str, str]:
        if self._raise_error:
            raise HerdrCliError("server unreachable", exit_code=1)
        return self._statuses


def setup_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HERDR_PLUGIN_STATE_DIR (hence default_reports_dir/default_history_path) at tmp."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state_dir))
    return state_dir


def write_pipeline_state(
    reports_dir: Path,
    *,
    run_id: str = RUN_ID,
    current_stage: int = 4,
    with_report: bool = False,
) -> Path:
    run_dir = reports_dir / f"pipeline-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "current_stage": current_stage,
                "branch": f"auto/pipeline-{run_id}",
                "shared_workspace": "w1G",
            }
        )
    )
    if with_report:
        (reports_dir / f"pipeline-{run_id}.md").write_text("# report\n")
    return state_path


def install_fake_client(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    monkeypatch.setattr(cli, "HerdrClient", lambda: client)


# --- criterion 1 ---------------------------------------------------------------------------


def test_status_running_table_handles_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Zero live agents must print an empty table plus a warning and exit 0 — not crash."""
    setup_state_dir(tmp_path, monkeypatch)
    install_fake_client(monkeypatch, FakeStatusClient({}))

    assert cli.main(["ps"]) == 0

    captured = capsys.readouterr()
    assert "AGENT" in captured.out  # header-only table
    assert "pl-" not in captured.out and "rt-" not in captured.out
    assert "warning" in captured.err.lower()


def test_status_running_table_survives_unreachable_herdr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Same graceful path when the Herdr server is down (HerdrCliError), per spec v2."""
    setup_state_dir(tmp_path, monkeypatch)
    install_fake_client(monkeypatch, FakeStatusClient(raise_error=True))

    assert cli.main(["ps"]) == 0

    captured = capsys.readouterr()
    assert "AGENT" in captured.out
    assert "unavailable" in captured.err


# --- criterion 2 ---------------------------------------------------------------------------


def test_status_running_table_shows_pipeline_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A working pl-<N>-<run_id> agent must be enriched to 'pipeline run … stage N/6'
    using the matching in-progress state.json under the reports dir — and settled agents
    (idle/done) must not be listed as running at all."""
    state_dir = setup_state_dir(tmp_path, monkeypatch)
    write_pipeline_state(state_dir / "reports", current_stage=4)
    install_fake_client(
        monkeypatch,
        FakeStatusClient({f"pl-4-{RUN_ID}": "working", "rt-a": "idle"}),
    )

    assert cli.main(["ps"]) == 0

    out = capsys.readouterr().out
    assert "stage 4/6" in out
    assert f"pipeline run {RUN_ID[:10]}" in out
    # idle agents stay registered until their pane closes but are not "running".
    assert "rt-a" not in out


def test_scan_and_enrich_round_trip(tmp_path: Path) -> None:
    reports_dir = tmp_path / "reports"
    state_path = write_pipeline_state(reports_dir, current_stage=4)

    runs = scan_pipeline_runs([reports_dir])
    assert set(runs) == {RUN_ID}
    assert runs[RUN_ID].current_stage == 4
    assert runs[RUN_ID].state_path == state_path

    rows = build_ps_rows({f"pl-4-{RUN_ID}": "working"}, runs)
    assert len(rows) == 1
    assert rows[0].detail == f"pipeline run {RUN_ID[:10]}… stage 4/{6}"

    # Herdr lowercases agent names; matching must survive that (observed live).
    lowered_rows = build_ps_rows({f"pl-4-{RUN_ID.lower()}": "working"}, runs)
    assert lowered_rows[0].detail == rows[0].detail

    # With the final report written, the run is complete and no longer enriches.
    (reports_dir / f"pipeline-{RUN_ID}.md").write_text("# done\n")
    runs_done = scan_pipeline_runs([reports_dir])
    rows_done = build_ps_rows({f"pl-4-{RUN_ID}": "working"}, runs_done)
    assert rows_done[0].detail == ""

    # Unparseable state.json is skipped, not raised.
    junk = reports_dir / "other" / "state.json"
    junk.parent.mkdir(parents=True)
    junk.write_text("{not json")
    assert scan_pipeline_runs([reports_dir]).keys() == {RUN_ID}


# --- criterion 5 (ps half; full both-commands version below) --------------------------------


def test_status_commands_are_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Neither `ps` nor `scheduled` may write jobs.yaml, history.jsonl, or any state.json
    (spec criterion 5). Snapshot content digests + mtimes around both commands."""
    state_dir = setup_state_dir(tmp_path, monkeypatch)
    reports_dir = state_dir / "reports"
    state_file = write_pipeline_state(reports_dir, current_stage=2)
    history_file = state_dir / "history.jsonl"
    history_file.write_text(
        '{"ts": "2026-08-24T03:00:05Z", "job": "nightly", "state": "done"}\n'
    )
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    config_path = tmp_path / "jobs.yaml"
    config_path.write_text(
        "version: 1\n"
        "jobs:\n"
        "  - name: nightly\n"
        "    cron: '0 3 * * *'\n"
        f"    repo: {repo}\n"
    )

    install_fake_client(monkeypatch, FakeStatusClient({}))

    def snapshot() -> dict[Path, tuple[str, int]]:
        out = {}
        for p in (config_path, history_file, state_file):
            data = p.read_bytes()
            out[p] = (
                hashlib.sha256(data).hexdigest(),
                p.stat().st_mtime_ns,
            )
        return out

    before = snapshot()
    try:
        assert cli.main(["ps"]) == 0
        # --config is a top-level option, so it precedes the subcommand.
        assert cli.main(["--config", str(config_path), "scheduled"]) == 0
    finally:
        capsys.readouterr()  # keep test output clean either way

    assert snapshot() == before
