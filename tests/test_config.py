from __future__ import annotations

from pathlib import Path

import pytest

from herdr_routines.config import ConfigError, load_config

VALID_MINIMAL = """
version: 1
jobs:
  - name: nightly-audit
    cron: "0 3 * * *"
    repo: /home/guido/projects/fitted
"""


def write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_valid_minimal_config_applies_defaults(tmp_config_path: Path) -> None:
    cfg = load_config(write(tmp_config_path, VALID_MINIMAL))
    assert len(cfg.jobs) == 1
    job = cfg.job("nightly-audit")
    assert job is not None
    assert job.enabled is True
    assert job.agent_kind == "claude"
    assert job.workspace == "worktree"
    assert job.timeout_ms == 1_800_000
    assert job.start_timeout_ms == 120_000
    assert job.catch_up_minutes == 120
    assert job.agent_name == "rt-nightly-audit"
    assert job.repo == Path("/home/guido/projects/fitted")


def test_defaults_block_is_merged_and_overridable(tmp_config_path: Path) -> None:
    text = """
version: 1
defaults:
  agent_kind: opencode
  catch_up_minutes: 60
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
  - name: b
    cron: "0 4 * * *"
    repo: /repo/b
    agent_kind: claude
"""
    cfg = load_config(write(tmp_config_path, text))
    a = cfg.job("a")
    b = cfg.job("b")
    assert a is not None and b is not None
    assert a.agent_kind == "opencode"
    assert a.catch_up_minutes == 60
    assert b.agent_kind == "claude"
    assert b.catch_up_minutes == 60


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.yaml")


def test_invalid_yaml_raises_config_error(tmp_config_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_config_path, "jobs: [this is not: valid: yaml"))


def test_non_mapping_document_raises(tmp_config_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_config_path, "- just\n- a\n- list\n"))


def test_unsupported_version_raises(tmp_config_path: Path) -> None:
    text = "version: 2\njobs: []\n"
    with pytest.raises(ConfigError, match="version"):
        load_config(write(tmp_config_path, text))


def test_unknown_top_level_key_raises(tmp_config_path: Path) -> None:
    text = VALID_MINIMAL + "\nbogus: true\n"
    with pytest.raises(ConfigError, match="unknown top-level"):
        load_config(write(tmp_config_path, text))


def test_unknown_defaults_key_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
defaults:
  bogus_key: 1
jobs: []
"""
    with pytest.raises(ConfigError, match="defaults"):
        load_config(write(tmp_config_path, text))


def test_unknown_job_key_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    bogus: 1
"""
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(write(tmp_config_path, text))


def test_missing_required_key_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
"""
    with pytest.raises(ConfigError, match="missing required"):
        load_config(write(tmp_config_path, text))


@pytest.mark.parametrize(
    "bad_name",
    [
        "Nightly-Audit",  # uppercase
        "1nightly",  # must start with a letter
        "nightly audit",  # space
        "a" * 25,  # too long: name is capped at 24 (rt- prefix + Herdr's 32-char cap)
        "",
    ],
)
def test_invalid_name_raises(tmp_config_path: Path, bad_name: str) -> None:
    text = f"""
version: 1
jobs:
  - name: "{bad_name}"
    cron: "0 3 * * *"
    repo: /repo/a
"""
    with pytest.raises(ConfigError, match="name"):
        load_config(write(tmp_config_path, text))


def test_name_at_exactly_24_chars_is_valid(tmp_config_path: Path) -> None:
    name = "a" * 24
    text = f"""
version: 1
jobs:
  - name: "{name}"
    cron: "0 3 * * *"
    repo: /repo/a
"""
    cfg = load_config(write(tmp_config_path, text))
    assert cfg.job(name) is not None


def test_duplicate_names_raise(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
  - name: a
    cron: "0 4 * * *"
    repo: /repo/b
"""
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write(tmp_config_path, text))


def test_invalid_cron_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "not a cron expression"
    repo: /repo/a
"""
    with pytest.raises(ConfigError, match="cron"):
        load_config(write(tmp_config_path, text))


def test_invalid_agent_kind_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    agent_kind: definitely-not-a-real-kind
"""
    with pytest.raises(ConfigError, match="agent_kind"):
        load_config(write(tmp_config_path, text))


def test_invalid_workspace_mode_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    workspace: not-a-mode
"""
    with pytest.raises(ConfigError, match="workspace"):
        load_config(write(tmp_config_path, text))


def test_negative_timeout_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    timeout_ms: -5
"""
    with pytest.raises(ConfigError, match="timeout_ms"):
        load_config(write(tmp_config_path, text))


def test_empty_jobs_list_is_valid(tmp_config_path: Path) -> None:
    cfg = load_config(write(tmp_config_path, "version: 1\njobs: []\n"))
    assert cfg.jobs == ()


@pytest.mark.parametrize(
    "agent_kind,model",
    [("claude", "opus"), ("opencode", "opencode/big-pickle")],
)
def test_model_is_accepted_for_supported_agent_kinds(
    tmp_config_path: Path, agent_kind: str, model: str
) -> None:
    text = f"""
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    agent_kind: {agent_kind}
    model: {model}
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("a")
    assert job is not None
    assert job.model == model


def test_model_raises_for_unsupported_agent_kind(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    agent_kind: codex
    model: some-model
"""
    with pytest.raises(ConfigError, match="model"):
        load_config(write(tmp_config_path, text))


def test_non_string_model_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    model: 123
"""
    with pytest.raises(ConfigError, match="model"):
        load_config(write(tmp_config_path, text))


def test_no_permission_mode_key_exists_in_schema(tmp_config_path: Path) -> None:
    """Deliberate: v1 has no escape hatch for unattended auto-approve (see docs/plan-v1.md §2)."""
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    permission_mode: bypassPermissions
"""
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(write(tmp_config_path, text))


# -- failure_markers (docs/failure-reaping.md §3.4) -------------------------------------------


def test_failure_markers_absent_means_none(tmp_config_path: Path) -> None:
    cfg = load_config(write(tmp_config_path, VALID_MINIMAL))
    job = cfg.job("nightly-audit")
    assert job is not None
    assert job.failure_markers is None


def test_failure_markers_from_defaults_are_inherited(tmp_config_path: Path) -> None:
    text = """
version: 1
defaults:
  failure_markers: ["Free usage exceeded"]
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("a")
    assert job is not None
    assert job.failure_markers == ("Free usage exceeded",)


def test_failure_markers_job_level_overrides_defaults(tmp_config_path: Path) -> None:
    text = """
version: 1
defaults:
  failure_markers: ["Free usage exceeded"]
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    failure_markers: ["Out of credits", "quota blown"]
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("a")
    assert job is not None
    assert job.failure_markers == ("Out of credits", "quota blown")


def test_explicit_empty_failure_markers_is_valid_and_means_empty_tuple(
    tmp_config_path: Path,
) -> None:
    """[] must survive validation as an empty tuple (scan disabled downstream) — not collapse
    into 'unset'."""
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    failure_markers: []
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("a")
    assert job is not None
    assert job.failure_markers == ()


@pytest.mark.parametrize(
    "raw",
    ["just-a-string", "[1, 2]", '["ok", ""]', '["ok", null]', "{}"],
)
def test_invalid_failure_markers_raise(tmp_config_path: Path, raw: str) -> None:
    text = f"""
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    failure_markers: {raw}
"""
    with pytest.raises(ConfigError, match="failure_markers"):
        load_config(write(tmp_config_path, text))


# -- auto_fix config --------------------------------------------------------


def test_auto_fix_defaults_applied(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: auto-fix-prs
    cron: "*/5 * * * *"
    repo: /repo/test
    auto_fix: {}
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("auto-fix-prs")
    assert job is not None
    assert job.auto_fix is not None
    assert job.auto_fix.branch_prefix == "auto/"
    assert job.auto_fix.max_prs_per_tick == 3
    assert job.auto_fix.max_attempts_per_pr == 3
    assert job.auto_fix.timeout_ms == 1_800_000
    assert job.auto_fix.agent_kind == "claude"
    assert job.auto_fix.model is None
    assert job.auto_fix.prompt == ""


def test_auto_fix_custom_values(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: auto-fix-prs
    cron: "*/5 * * * *"
    repo: /repo/test
    auto_fix:
      branch_prefix: "fix/"
      max_prs_per_tick: 5
      max_attempts_per_pr: 10
      timeout_ms: 600000
      agent_kind: opencode
      model: opencode/big-pickle
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("auto-fix-prs")
    assert job is not None
    assert job.auto_fix is not None
    assert job.auto_fix.branch_prefix == "fix/"
    assert job.auto_fix.max_prs_per_tick == 5
    assert job.auto_fix.max_attempts_per_pr == 10
    assert job.auto_fix.timeout_ms == 600_000
    assert job.auto_fix.agent_kind == "opencode"
    assert job.auto_fix.model == "opencode/big-pickle"


def test_auto_fix_empty_branch_prefix_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    auto_fix:
      branch_prefix: ""
"""
    with pytest.raises(ConfigError, match="branch_prefix"):
        load_config(write(tmp_config_path, text))


def test_auto_fix_negative_max_prs_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    auto_fix:
      max_prs_per_tick: -1
"""
    with pytest.raises(ConfigError, match="max_prs_per_tick"):
        load_config(write(tmp_config_path, text))


def test_auto_fix_negative_max_attempts_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    auto_fix:
      max_attempts_per_pr: -5
"""
    with pytest.raises(ConfigError, match="max_attempts_per_pr"):
        load_config(write(tmp_config_path, text))


def test_auto_fix_bad_agent_kind_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    auto_fix:
      agent_kind: not-a-real-kind
"""
    with pytest.raises(ConfigError, match="agent_kind"):
        load_config(write(tmp_config_path, text))


def test_auto_fix_model_without_flag_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    auto_fix:
      agent_kind: codex
      model: some-model
"""
    with pytest.raises(ConfigError, match="model"):
        load_config(write(tmp_config_path, text))


def test_auto_fix_unknown_key_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    auto_fix:
      bogus_key: true
"""
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(write(tmp_config_path, text))


def test_auto_fix_non_mapping_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    auto_fix: "not a mapping"
"""
    with pytest.raises(ConfigError, match="auto_fix"):
        load_config(write(tmp_config_path, text))


def test_job_without_auto_fix_has_none(tmp_config_path: Path) -> None:
    cfg = load_config(write(tmp_config_path, VALID_MINIMAL))
    job = cfg.job("nightly-audit")
    assert job is not None
    assert job.auto_fix is None
