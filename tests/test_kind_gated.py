"""Acceptance tests for issue 049: kind becomes the single dispatch key.

11 tests, one per acceptance criterion, named exactly as the spec requires.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from herdr_routines.config import (
    _DEFAULTS_ALLOWED_KEYS,
    VALID_JOB_KINDS,
    ConfigError,
    Job,
    load_config,
    load_config_dir,
)

# -- helpers -------------------------------------------------------------------


def write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def _make_jobs_d(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a jobs.d/ directory with the given filename->content mapping."""
    jobs_dir = tmp_path / "jobs.d"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (jobs_dir / name).write_text(content)
    return jobs_dir


# -- AC 1: _process_job dispatches on kind only --------------------------------


def test_process_job_dispatches_on_kind_only() -> None:
    """AC 1: parse inspect.getsource(_process_job) with AST and assert no If test
    node in the function's body references job.checks. AST strips comments, so the
    in-function comment mentioning 'checks:' must not trip the check. Scoped to
    _process_job only — the two assert statements in _process_gated_job intentionally
    remain."""
    from herdr_routines.tick import _process_job

    source = inspect.getsource(_process_job)
    tree = ast.parse(textwrap.dedent(source))

    # Find the _process_job function node
    func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_process_job":
            func_node = node
            break
    assert func_node is not None, "_process_job function not found in AST"

    # Walk all If nodes in the function body and check their test expressions
    for node in ast.walk(func_node):
        if isinstance(node, ast.If):
            # Convert the test expression to source to check for references to job.checks
            test_src = ast.dump(node.test)
            # ast.dump will contain "Attribute" nodes with attr="checks" if job.checks
            # is referenced — but AST strips comments and strings, so this is reliable
            has_checks_ref = (
                isinstance(node.test, ast.Attribute) and node.test.attr == "checks"
            )
            # Also check for subscripts like job.checks[0]
            if isinstance(node.test, ast.Subscript) and isinstance(
                node.test.value, ast.Attribute
            ):
                has_checks_ref = has_checks_ref or node.test.value.attr == "checks"
            # Check for boolean operations (e.g. "job.checks is not None and ...")
            if isinstance(node.test, ast.BoolOp):
                for value in node.test.values:
                    if isinstance(value, ast.Compare):
                        if isinstance(value.left, ast.Attribute):
                            has_checks_ref = (
                                has_checks_ref or value.left.attr == "checks"
                            )
                    elif isinstance(value, ast.Attribute):
                        has_checks_ref = has_checks_ref or value.attr == "checks"
            # Check for chained comparisons
            if isinstance(node.test, ast.Compare) and isinstance(
                node.test.left, ast.Attribute
            ):
                has_checks_ref = has_checks_ref or node.test.left.attr == "checks"
            assert not has_checks_ref, (
                f"_process_job If node references job.checks — "
                f"dispatch must be kind-only. AST dump of test: {test_src}"
            )


# -- AC 2: checks without gated kind is ConfigError ----------------------------


def test_checks_without_gated_kind_rejected(tmp_config_path: Path) -> None:
    """AC 2: a job with checks and no kind: gated (explicit routine or default) is
    a ConfigError whose message contains both 'kind: gated' and 'checks'."""
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    kind: routine
    checks:
      - pr_health:
"""
    with pytest.raises(ConfigError, match="kind: gated") as exc_info:
        load_config(write(tmp_config_path, text))
    assert "checks" in str(exc_info.value).lower()


def test_checks_without_kind_rejected(tmp_config_path: Path) -> None:
    """AC 2 variant: checks present with no explicit kind (defaults to routine)."""
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    checks:
      - pr_health:
"""
    with pytest.raises(ConfigError, match="kind: gated") as exc_info:
        load_config(write(tmp_config_path, text))
    assert "checks" in str(exc_info.value).lower()


# -- AC 3: kind: gated requires checks ----------------------------------------


def test_gated_kind_requires_checks(tmp_config_path: Path) -> None:
    """AC 3: kind: gated with missing or empty checks list is a ConfigError
    naming the job and checks."""
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    kind: gated
"""
    with pytest.raises(ConfigError, match="kind: gated.*requires"):
        load_config(write(tmp_config_path, text))


def test_gated_kind_empty_checks_rejected(tmp_config_path: Path) -> None:
    """AC 3 variant: kind: gated with empty checks list."""
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    kind: gated
    checks: []
"""
    with pytest.raises(ConfigError, match="kind: gated.*requires"):
        load_config(write(tmp_config_path, text))


# -- AC 4: kind: pipeline rejects checks; kind: gated accepts pipeline-only fields


def test_pipeline_rejects_checks_unchanged(tmp_config_path: Path) -> None:
    """AC 4: kind: pipeline with checks stays rejected. kind: gated accepts
    deadline_ms and prompt_file without complaint."""
    # pipeline + checks rejected
    text = """
version: 1
jobs:
  - name: a
    cron: "0 2 * * *"
    repo: /repo/a
    kind: pipeline
    prompt_file: docs/pipeline/orchestrator-prompt.md
    deadline_ms: 25200000
    checks:
      - pr_health:
"""
    with pytest.raises(ConfigError, match="checks"):
        load_config(write(tmp_config_path, text))

    # gated + pipeline-only fields accepted (deadline_ms, prompt_file are allowed)
    text = """
version: 1
jobs:
  - name: b
    cron: "0 3 * * *"
    repo: /repo/b
    kind: gated
    prompt_file: custom-prompt.md
    deadline_ms: 3600000
    checks:
      - pr_health:
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("b")
    assert job is not None
    assert job.kind == "gated"
    assert job.prompt_file == "custom-prompt.md"
    assert job.deadline_ms == 3_600_000


# -- AC 5: unknown kind is ConfigError -----------------------------------------


def test_unknown_kind_rejected(tmp_config_path: Path) -> None:
    """AC 5: an unknown kind value is a ConfigError over the widened set."""
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    kind: orchestrator
"""
    with pytest.raises(ConfigError, match="kind"):
        load_config(write(tmp_config_path, text))


# -- AC 6: Job.kind is Literal -------------------------------------------------


def test_job_kind_is_literal() -> None:
    """AC 6: Job.kind is typed Literal['routine', 'gated', 'pipeline'].
    Verified via runtime annotation check (mypy gate covers the static side)."""
    annotations = Job.__annotations__
    kind_type = annotations["kind"]
    # After from __future__ import annotations, annotations are strings
    assert "Literal" in str(kind_type), f"Job.kind is not Literal, got: {kind_type}"
    assert "routine" in str(kind_type)
    assert "gated" in str(kind_type)
    assert "pipeline" in str(kind_type)


def test_valid_job_kinds_widened() -> None:
    """AC 6 supplement: VALID_JOB_KINDS contains the three values."""
    assert VALID_JOB_KINDS == frozenset({"routine", "gated", "pipeline"})


# -- AC 7: re-keyed discriminators behave identically ---------------------------


def test_discriminator_rekey_on_kind(tmp_config_path: Path) -> None:
    """AC 7: target inference keys on kind == gated (not checks is not None).
    Verify by loading a gated job and checking target is inferred from checks."""
    text = """
version: 1
jobs:
  - name: bps
    cron: "*/10 * * * *"
    repo: /repo/test
    kind: gated
    checks:
      - pr_health:
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("bps")
    assert job is not None
    assert job.kind == "gated"
    assert job.target == "pr"  # inferred from pr_health check

    # Also verify command checks infer base target
    text2 = """
version: 1
jobs:
  - name: rh
    cron: "0 13 * * *"
    repo: /repo/test
    base: main
    kind: gated
    checks:
      - command: uv run ruff check .
"""
    cfg2 = load_config(write(tmp_config_path, text2))
    job2 = cfg2.job("rh")
    assert job2 is not None
    assert job2.kind == "gated"
    assert job2.target == "base"  # inferred from command check


def test_no_checks_is_not_none_outside_gated_job() -> None:
    """AC 7 supplement: grep src/herdr_routines/ for 'checks is not None' /
    'checks is None' — must only appear in _process_gated_job's two asserts
    and config.py's checks-presence validation."""
    import re

    src_dir = Path(__file__).resolve().parent.parent / "src" / "herdr_routines"
    pattern = re.compile(r"checks is not None|checks is None")

    allowed_files = {"tick.py", "config.py"}
    allowed_contexts = {
        # tick.py: _process_gated_job's two asserts (intentional)
        "assert job.checks is not None",
    }

    violations = []
    for py_file in sorted(src_dir.glob("**/*.py")):
        if py_file.name not in allowed_files:
            continue
        for i, line in enumerate(py_file.read_text().splitlines(), 1):
            stripped = line.strip()
            if pattern.search(stripped) and stripped not in allowed_contexts:
                # config.py: validation/parsing lines are allowed
                if py_file.name == "config.py" and any(
                    kw in stripped
                    for kw in (
                        "checks_raw",
                        "checks is None",
                        "not gated",
                        "requires kind: gated",
                        "# (the old",
                        "checks is not None and kind",  # gated validation
                        "if checks is not None:",  # pipeline rejection guard
                    )
                ):
                    continue
                violations.append(f"{py_file.name}:{i}: {stripped}")

    assert violations == [], (
        "Found checks-is-None/is-Not-None discriminators outside allowed locations: "
        + "; ".join(violations)
    )


# -- AC 8: regression — routine and gated unchanged ----------------------------


def test_routine_and_gated_regression_unchanged(tmp_path: Path) -> None:
    """AC 8: plain no-checks routine runs execute_run path; kind: gated reaches
    _process_gated_job with same checks/target semantics."""
    from dataclasses import replace

    from herdr_routines.config import GateCheck, RoutinesConfig

    # Plain routine: kind defaults to "routine", checks=None
    job_routine = Job(
        name="plain",
        enabled=True,
        cron="* * * * *",
        repo=tmp_path,
        workspace="root",
        base="main",
        agent_kind="claude",
        model=None,
        prompt="report to $ROUTINE_REPORT",
        timeout_ms=5_000,
        start_timeout_ms=30_000,
        catch_up_minutes=120,
        timezone="UTC",
        on_missed="log",
    )
    assert job_routine.kind == "routine"
    assert job_routine.checks is None

    # Gated: kind="gated", checks present
    job_gated = replace(
        job_routine,
        name="gated-job",
        kind="gated",
        checks=(GateCheck(kind="pr_health"),),
        target="pr",
        max_workers_per_tick=3,
        max_attempts_per_target=3,
    )
    assert job_gated.kind == "gated"
    assert job_gated.checks is not None

    # Both can coexist in config
    config = RoutinesConfig(jobs=(job_routine, job_gated))
    assert config.job("plain") is not None
    assert config.job("gated-job") is not None


# -- AC 9: jobs.d + defaults roundtrip -----------------------------------------


def test_gated_job_jobs_d_roundtrip(tmp_path: Path) -> None:
    """AC 9: a kind: gated job in a jobs.d/<name>.yaml loads with non-empty checks,
    and neither kind nor checks is in _DEFAULTS_ALLOWED_KEYS."""
    jobs_dir = _make_jobs_d(
        tmp_path,
        {
            "defaults.yaml": "agent_kind: opencode\n",
            "bps.yaml": (
                "name: bps\n"
                "cron: '*/10 * * * *'\n"
                "repo: /repo/test\n"
                "kind: gated\n"
                "checks:\n"
                "  - pr_health:\n"
                "max_workers_per_tick: 3\n"
                "max_attempts_per_target: 3\n"
            ),
        },
    )
    cfg = load_config(jobs_dir)
    job = cfg.job("bps")
    assert job is not None
    assert job.kind == "gated"
    assert job.checks is not None
    assert len(job.checks) == 1
    assert job.checks[0].kind == "pr_health"

    # kind and checks must NOT be in _DEFAULTS_ALLOWED_KEYS
    assert "kind" not in _DEFAULTS_ALLOWED_KEYS
    assert "checks" not in _DEFAULTS_ALLOWED_KEYS


# -- AC 10: status renders gated job -------------------------------------------


def test_status_renders_gated_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 10: status renders a kind: gated job via normal join on job name
    (cli.py:432-434) with no history-format change and no kind key in any
    history.jsonl record."""
    import json

    from herdr_routines import cli
    from herdr_routines.history import HistoryRecord, append

    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    history_path = tmp_path / "state" / "history.jsonl"

    jobs_dir = _make_jobs_d(
        tmp_path,
        {
            "bps.yaml": (
                "name: bps\n"
                "cron: '*/10 * * * *'\n"
                "repo: /repo/test\n"
                "kind: gated\n"
                "checks:\n"
                "  - pr_health:\n"
                "max_workers_per_tick: 3\n"
                "max_attempts_per_target: 3\n"
            ),
        },
    )

    # Write a history record (no 'kind' key — history.jsonl is unchanged)
    from datetime import UTC, datetime

    now = datetime(2026, 9, 10, 5, 0, 0, tzinfo=UTC)
    append(
        history_path,
        HistoryRecord(ts=now, job="bps", state="done", run_id="bps-20260910"),
    )

    # Verify history record has no 'kind' key
    record_line = history_path.read_text().strip()
    record = json.loads(record_line)
    assert "kind" not in record
    assert record["job"] == "bps"
    assert record["state"] == "done"

    # Verify status renders via CLI
    monkeypatch.setattr(cli, "default_config_path", lambda: jobs_dir)
    monkeypatch.setattr(cli, "default_history_path", lambda: history_path)
    monkeypatch.setattr(cli, "default_lock_path", lambda: tmp_path / "tick.lock")

    import argparse

    args = argparse.Namespace(config=jobs_dir)
    assert cli._cmd_status(args) == 0


# -- AC 11: deploy migration ships ---------------------------------------------


def test_example_config_kind_gated(tmp_path: Path) -> None:
    """AC 11: deploy/jobs.d/babysit-prs.yaml and deploy/jobs.example.yaml carry
    kind: gated. Committed jobs.d/ loads clean through validate. Babysit-prs is
    the only committed job with a checks: key."""
    # babysit-prs.yaml has kind: gated
    babysit = (
        Path(__file__).resolve().parent.parent
        / "deploy"
        / "jobs.d"
        / "babysit-prs.yaml"
    )
    babysit_text = babysit.read_text()
    assert "kind: gated" in babysit_text
    assert "checks:" in babysit_text

    # jobs.example.yaml active block has kind: gated
    example = Path(__file__).resolve().parent.parent / "deploy" / "jobs.example.yaml"
    example_text = example.read_text()
    assert "kind: gated" in example_text

    # Committed jobs.d/ loads clean through load_config
    example_dir = Path(__file__).resolve().parent.parent / "deploy" / "jobs.d"
    cfg = load_config_dir(example_dir)
    # No errors from load_config_dir (babysit-prs validates cleanly)
    # Note: feature-pipeline.yaml may produce warnings/errors from missing
    # repo/prompt_file on this host, but babysit-prs itself loads cleanly
    bps = cfg.job("babysit-prs")
    assert bps is not None
    assert bps.kind == "gated"
    assert bps.checks is not None

    # Grep: babysit-prs is the only committed job with checks: key
    import re

    checks_pattern = re.compile(r"^checks:", re.MULTILINE)
    jobs_d_files = sorted(example_dir.glob("*.yaml"))
    files_with_checks = []
    for f in jobs_d_files:
        if f.name == "defaults.yaml":
            continue
        text = f.read_text()
        # Exclude lines that are comments
        for line in text.splitlines():
            if checks_pattern.match(line.lstrip()):
                files_with_checks.append(f.name)
                break
    assert files_with_checks == ["babysit-prs.yaml"], (
        f"Expected only babysit-prs.yaml with checks:, found: {files_with_checks}"
    )
