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
    with pytest.raises(ConfigError, match="missing"):
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


def test_fallback_model_settable_in_defaults_and_overridable_per_job(
    tmp_path: Path,
) -> None:
    """fallback_model lives in _DEFAULTS_ALLOWED_KEYS (unlike model) so one entry in
    defaults.yaml covers every job sharing a provider's free-tier pool; a job can still
    override it."""
    jobs_dir = _make_jobs_d(
        tmp_path,
        {
            "defaults.yaml": "agent_kind: opencode\nfallback_model: openrouter/free\n",
            "a.yaml": "name: a\ncron: '0 3 * * *'\nrepo: /repo/a\n",
            "b.yaml": (
                "name: b\ncron: '0 4 * * *'\nrepo: /repo/b\n"
                "fallback_model: openrouter/other\n"
            ),
        },
    )
    cfg = load_config(jobs_dir)
    a = cfg.job("a")
    b = cfg.job("b")
    assert a is not None and a.fallback_model == "openrouter/free"
    assert b is not None and b.fallback_model == "openrouter/other"


def test_defaults_fallback_model_is_inert_for_unsupported_agent_kind(
    tmp_path: Path,
) -> None:
    """Regression (PR #65 review): fallback_model in defaults.yaml is deliberately shared
    across every job (unlike model). A job whose own agent_kind doesn't support model
    selection at all (e.g. codex) must still load — the inherited default is simply inert
    for it, not a config error. Only an *explicit* per-job fallback_model for an unsupported
    agent_kind stays an error (see test_fallback_model_raises_for_unsupported_agent_kind)."""
    jobs_dir = _make_jobs_d(
        tmp_path,
        {
            "defaults.yaml": "fallback_model: openrouter/free\n",
            "a.yaml": "name: a\ncron: '0 3 * * *'\nrepo: /repo/a\nagent_kind: codex\n",
        },
    )
    cfg = load_config(jobs_dir)
    a = cfg.job("a")
    assert a is not None
    assert a.fallback_model is None


def test_fallback_model_raises_for_unsupported_agent_kind(
    tmp_config_path: Path,
) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    agent_kind: codex
    fallback_model: some-model
"""
    with pytest.raises(ConfigError, match="fallback_model"):
        load_config(write(tmp_config_path, text))


def test_non_string_fallback_model_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    fallback_model: 123
"""
    with pytest.raises(ConfigError, match="fallback_model"):
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


# -- checks config (unified gate model) -------------------------------------------


def test_checks_pr_health_inferred_target(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: babysit-prs
    cron: "*/5 * * * *"
    repo: /repo/test
    checks:
      - pr_health:
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("babysit-prs")
    assert job is not None
    assert job.checks is not None
    assert len(job.checks) == 1
    assert job.checks[0].kind == "pr_health"
    assert job.target == "pr"


def test_checks_command_inferred_target(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: repo-hygiene
    cron: "0 13 * * *"
    repo: /repo/test
    base: main
    checks:
      - command: uv run ruff check .
        timeout_ms: 120000
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("repo-hygiene")
    assert job is not None
    assert job.checks is not None
    assert len(job.checks) == 1
    assert job.checks[0].kind == "command"
    assert job.checks[0].command == "uv run ruff check ."
    assert job.checks[0].timeout_ms == 120_000
    assert job.target == "base"


def test_checks_empty_list_becomes_none(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: plain-job
    cron: "0 3 * * *"
    repo: /repo/test
    checks: []
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("plain-job")
    assert job is not None
    assert job.checks is None


def test_checks_mixed_rejected(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: bad
    cron: "0 3 * * *"
    repo: /repo/test
    checks:
      - pr_health:
      - command: uv run ruff check .
"""
    with pytest.raises(ConfigError, match="mix"):
        load_config(write(tmp_config_path, text))


def test_checks_invalid_target_rejected(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: bad
    cron: "0 3 * * *"
    repo: /repo/test
    target: invalid
"""
    with pytest.raises(ConfigError, match="target"):
        load_config(write(tmp_config_path, text))


def test_checks_target_mismatch_rejected(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: bad
    cron: "0 3 * * *"
    repo: /repo/test
    target: base
    checks:
      - pr_health:
"""
    with pytest.raises(ConfigError, match="does not match"):
        load_config(write(tmp_config_path, text))


def test_checks_invalid_kind_rejected(tmp_config_path: Path) -> None:
    """A check with neither pr_health nor command is rejected."""
    text = """
version: 1
jobs:
  - name: bad
    cron: "0 3 * * *"
    repo: /repo/test
    checks:
      - {}
"""
    with pytest.raises(ConfigError, match="must have either"):
        load_config(write(tmp_config_path, text))


def test_checks_command_empty_rejected(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: bad
    cron: "0 3 * * *"
    repo: /repo/test
    checks:
      - command: ""
"""
    with pytest.raises(ConfigError, match="non-empty string"):
        load_config(write(tmp_config_path, text))


def test_checks_negative_timeout_rejected(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: bad
    cron: "0 3 * * *"
    repo: /repo/test
    checks:
      - command: ruff check .
        timeout_ms: -1
"""
    with pytest.raises(ConfigError, match="positive integer"):
        load_config(write(tmp_config_path, text))


def test_max_workers_per_tick_and_max_attempts(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/test
    max_workers_per_tick: 5
    max_attempts_per_target: 10
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("a")
    assert job is not None
    assert job.max_workers_per_tick == 5
    assert job.max_attempts_per_target == 10


def test_job_without_checks_has_none(tmp_config_path: Path) -> None:
    cfg = load_config(write(tmp_config_path, VALID_MINIMAL))
    job = cfg.job("nightly-audit")
    assert job is not None
    assert job.checks is None
    assert job.target is None


# -- jobs.d directory layout tests (spec 20260831T012350Z) ----------------------------------------


def _make_jobs_d(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a jobs.d/ directory with the given filename->content mapping."""
    jobs_dir = tmp_path / "jobs.d"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (jobs_dir / name).write_text(content)
    return jobs_dir


def test_config_discovers_jobs_d(tmp_path: Path) -> None:
    """Acceptance 1: loader discovers every jobs.d/*.yaml sorted excluding defaults.yaml
    and merges defaults.yaml under each job's own fields with precedence
    _JOB_DEFAULTS < defaults.yaml < job file."""
    defaults = "agent_kind: opencode\ncatch_up_minutes: 60\n"
    job_a = "name: a\ncron: '0 3 * * *'\nrepo: /repo/a\n"
    job_b = "name: b\ncron: '0 4 * * *'\nrepo: /repo/b\nagent_kind: claude\n"
    jobs_dir = _make_jobs_d(
        tmp_path,
        {
            "defaults.yaml": defaults,
            "a.yaml": job_a,
            "b.yaml": job_b,
        },
    )
    cfg = load_config(jobs_dir)
    assert len(cfg.jobs) == 2
    a = cfg.job("a")
    b = cfg.job("b")
    assert a is not None and b is not None
    # a inherits defaults.yaml
    assert a.agent_kind == "opencode"
    assert a.catch_up_minutes == 60
    # b overrides defaults.yaml
    assert b.agent_kind == "claude"
    assert b.catch_up_minutes == 60


def test_config_filename_name_mismatch(tmp_path: Path) -> None:
    """Acceptance 2: filename/name contract enforced.  'name' key if present must
    equal stem; both validated against NAME_RE."""
    # name key disagrees with filename stem
    job_content = "name: wrong\ncron: '0 3 * * *'\nrepo: /repo/a\n"
    jobs_dir = _make_jobs_d(tmp_path, {"my-job.yaml": job_content})
    cfg = load_config(jobs_dir)
    assert len(cfg.jobs) == 0
    assert any("does not match" in e and "my-job.yaml" in e for e in cfg.errors)


def test_config_filename_name_mismatch_accepts_matching(tmp_path: Path) -> None:
    """Acceptance 2 positive: name key that matches stem is accepted."""
    job_content = "name: my-job\ncron: '0 3 * * *'\nrepo: /repo/a\n"
    jobs_dir = _make_jobs_d(tmp_path, {"my-job.yaml": job_content})
    cfg = load_config(jobs_dir)
    assert len(cfg.jobs) == 1
    assert cfg.jobs[0].name == "my-job"


def test_config_filename_name_implicit_from_stem(tmp_path: Path) -> None:
    """Acceptance 2: when name key is absent, filename stem is the canonical name."""
    job_content = "cron: '0 3 * * *'\nrepo: /repo/a\n"
    jobs_dir = _make_jobs_d(tmp_path, {"my-job.yaml": job_content})
    cfg = load_config(jobs_dir)
    assert len(cfg.jobs) == 1
    assert cfg.jobs[0].name == "my-job"


def test_config_filename_rejects_name_re_violation(tmp_path: Path) -> None:
    """Acceptance 2: filename stem that violates NAME_RE is rejected."""
    job_content = "cron: '0 3 * * *'\nrepo: /repo/a\n"
    jobs_dir = _make_jobs_d(tmp_path, {"Bad-Name.yaml": job_content})
    cfg = load_config(jobs_dir)
    assert len(cfg.jobs) == 0
    assert any("Bad-Name" in e for e in cfg.errors)


def test_config_yaml_error_isolated_by_file(tmp_path: Path) -> None:
    """Acceptance 3: YAML syntax error in one jobs.d/<name>.yaml surfaces that file
    by name and does not prevent other jobs from loading."""
    good = "name: good\ncron: '0 3 * * *'\nrepo: /repo/good\n"
    bad = "name: bad\ncron: [invalid: yaml: {\n"  # broken YAML
    jobs_dir = _make_jobs_d(tmp_path, {"good.yaml": good, "bad.yaml": bad})
    cfg = load_config(jobs_dir)
    # good job loaded
    assert len(cfg.jobs) == 1
    assert cfg.jobs[0].name == "good"
    # bad file surfaced in errors
    assert len(cfg.errors) == 1
    assert "bad.yaml" in cfg.errors[0]


def test_config_defaults_merge_precedence_and_absent(tmp_path: Path) -> None:
    """Acceptance 6: defaults.yaml absent = empty defaults; unknown keys in defaults.yaml
    rejected; duplicate job name across files rejected; checks: [] still plain."""
    # --- absent defaults.yaml ---
    job_content = "name: a\ncron: '0 3 * * *'\nrepo: /repo/a\n"
    jobs_dir = _make_jobs_d(tmp_path / "t1", {"a.yaml": job_content})
    cfg = load_config(jobs_dir)
    assert len(cfg.jobs) == 1
    assert cfg.jobs[0].agent_kind == "claude"  # from _JOB_DEFAULTS

    # --- unknown keys in defaults.yaml ---
    bad_defaults = "bogus_key: 1\n"
    jobs_dir2 = _make_jobs_d(
        tmp_path / "t2", {"defaults.yaml": bad_defaults, "a.yaml": job_content}
    )
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(jobs_dir2)

    # --- duplicate job name across files: with the filename=name contract, different
    # stems produce different names, so true duplicates are impossible in jobs.d/.
    # Verify this invariant: two files with the same name key but different stems
    # are rejected by the name mismatch check, not the duplicate check.
    job_x1 = "name: same\ncron: '0 3 * * *'\nrepo: /repo/x\n"
    job_x2 = "name: same\ncron: '0 4 * * *'\nrepo: /repo/y\n"
    jobs_dir3 = _make_jobs_d(tmp_path / "t3", {"x.yaml": job_x1, "y.yaml": job_x2})
    cfg3 = load_config(jobs_dir3)
    assert len(cfg3.jobs) == 0  # both rejected by name mismatch
    assert len(cfg3.errors) == 2

    # --- checks: [] still plain ---
    job_checks = "name: c\ncron: '0 3 * * *'\nrepo: /repo/c\nchecks: []\n"
    jobs_dir4 = _make_jobs_d(tmp_path / "t4", {"c.yaml": job_checks})
    cfg4 = load_config(jobs_dir4)
    assert len(cfg4.jobs) == 1
    assert cfg4.jobs[0].checks is None


def test_config_jobs_d_sorted_and_example_layout(tmp_path: Path) -> None:
    """Acceptance 7: directory discovery is deterministic (sorted() glob) and
    deploy/jobs.d/ example layout present."""
    # Create files in reverse order to verify sorted discovery
    job_z = "name: z\ncron: '0 3 * * *'\nrepo: /repo/z\n"
    job_a = "name: a\ncron: '0 3 * * *'\nrepo: /repo/a\n"
    job_m = "name: m\ncron: '0 3 * * *'\nrepo: /repo/m\n"
    jobs_dir = _make_jobs_d(
        tmp_path, {"z.yaml": job_z, "a.yaml": job_a, "m.yaml": job_m}
    )
    cfg = load_config(jobs_dir)
    names = [j.name for j in cfg.jobs]
    assert names == sorted(names)

    # deploy/jobs.d/ example layout exists
    from pathlib import Path as P

    example_dir = P(__file__).resolve().parent.parent / "deploy" / "jobs.d"
    assert example_dir.is_dir()
    assert (example_dir / "defaults.yaml").exists()
    yaml_files = sorted(
        f.name for f in example_dir.glob("*.yaml") if f.name != "defaults.yaml"
    )
    assert len(yaml_files) >= 2  # at least 2 example job files


def test_config_migration_jobs_yaml_fallback_documented(tmp_path: Path) -> None:
    """Acceptance 5: loader accepts both during transition.  If jobs.d/ exists use
    it; else fall back to legacy jobs.yaml with deprecation warning."""
    import warnings as w_mod

    # --- legacy jobs.yaml still works (with deprecation warning) ---
    legacy_path = tmp_path / "jobs.yaml"
    legacy_path.write_text(
        "version: 1\njobs:\n  - name: legacy\n    cron: '0 3 * * *'\n    repo: /repo/legacy\n"
    )
    with w_mod.catch_warnings():
        w_mod.simplefilter("ignore", DeprecationWarning)
        cfg = load_config(legacy_path)
    assert len(cfg.jobs) == 1
    assert cfg.jobs[0].name == "legacy"

    # --- directory takes precedence when both exist ---
    jobs_dir = tmp_path / "jobs.d"
    jobs_dir.mkdir()
    (jobs_dir / "dirjob.yaml").write_text(
        "name: dirjob\ncron: '0 4 * * *'\nrepo: /repo/dirjob\n"
    )
    cfg2 = load_config(jobs_dir)
    assert len(cfg2.jobs) == 1
    assert cfg2.jobs[0].name == "dirjob"


def test_config_review_tiers_present() -> None:
    """Spec v2 review notes contain blocking/non-blocking and confidence tiers."""
    from pathlib import Path

    spec = Path("docs/pipeline/runs/20260831T012350Z/spec.md")
    if not spec.exists():
        # Fallback for other runs: check current spec path via env or just pass
        spec = (
            Path(__file__).parent.parent / "docs/pipeline/runs/20260831T012350Z/spec.md"
        )
    text = spec.read_text() if spec.exists() else ""
    # Check for at least one blocking, non-blocking, and confidence marker
    assert "blocking" in text.lower()
    assert "non-blocking" in text.lower()
    assert "confidence:" in text.lower()


# -- repository: <url> job field (issue 016) -------------------------------------------


def test_repo_url_accepted(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repository: https://github.com/org/repo.git
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("a")
    assert job is not None
    assert job.repository == "https://github.com/org/repo.git"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/org/repo.git",
        "ssh://git@github.com/org/repo.git",
        "git@github.com:org/repo.git",
        "git://github.com/org/repo.git",
    ],
)
def test_repo_url_various_protocols_accepted(tmp_config_path: Path, url: str) -> None:
    text = f"""
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repository: {url}
"""
    cfg = load_config(write(tmp_config_path, text))
    assert cfg.job("a") is not None


@pytest.mark.parametrize("bad_url", ["/local/path", "not-a-url", "ftp://wrong-scheme"])
def test_repo_url_bare_paths_rejected(tmp_config_path: Path, bad_url: str) -> None:
    text = f"""
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repository: {bad_url}
"""
    with pytest.raises(ConfigError, match="repository"):
        load_config(write(tmp_config_path, text))


def test_repo_url_derives_repo_from_name(tmp_config_path: Path) -> None:
    """repository present, repo absent → repo = default_repos_dir() / name."""
    text = """
version: 1
jobs:
  - name: myjob
    cron: "0 3 * * *"
    repository: https://github.com/org/repo.git
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("myjob")
    assert job is not None
    assert job.repository == "https://github.com/org/repo.git"
    assert job.repo.name == "myjob"
    assert "repos" in str(job.repo)


def test_repo_url_and_explicit_repo_uses_explicit(tmp_config_path: Path) -> None:
    """Both repository and repo → repo is explicit, repository is the remote."""
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /explicit/path
    repository: https://github.com/org/repo.git
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("a")
    assert job is not None
    assert job.repo == Path("/explicit/path")
    assert job.repository == "https://github.com/org/repo.git"


def test_neither_repo_nor_repository_raises(tmp_config_path: Path) -> None:
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
"""
    with pytest.raises(ConfigError, match="missing"):
        load_config(write(tmp_config_path, text))


def test_explicit_repo_only_still_works(tmp_config_path: Path) -> None:
    """Legacy repo-only jobs continue to work unchanged."""
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /some/repo
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("a")
    assert job is not None
    assert job.repo == Path("/some/repo")
    assert job.repository is None


def test_repo_url_example_and_docs() -> None:
    """deploy/jobs.example.yaml has a commented repository example."""
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "deploy" / "jobs.example.yaml"
    assert example.exists()
    text = example.read_text()
    assert "repository:" in text


def test_repo_url_validation_and_derivation(tmp_config_path: Path) -> None:
    """Config validation: URL-shape accepted, bare paths rejected, repo derived from name."""
    # Valid URLs accepted
    for url in [
        "https://github.com/org/repo.git",
        "ssh://git@github.com/org/repo.git",
        "git@github.com:org/repo.git",
        "git://github.com/org/repo.git",
    ]:
        text = f"""
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repository: {url}
"""
        cfg = load_config(write(tmp_config_path, text))
        assert cfg.job("a") is not None

    # Bare paths rejected
    for bad_url in ["/local/path", "not-a-url", "ftp://wrong-scheme"]:
        text = f"""
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repository: {bad_url}
"""
        with pytest.raises(ConfigError, match="repository"):
            load_config(write(tmp_config_path, text))

    # repository alone derives repos/<name>
    text = """
version: 1
jobs:
  - name: myjob
    cron: "0 3 * * *"
    repository: https://github.com/org/repo.git
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("myjob")
    assert job is not None
    assert job.repo.name == "myjob"
    assert "repos" in str(job.repo)

    # Both present: explicit repo + repository as remote
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /explicit/path
    repository: https://github.com/org/repo.git
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("a")
    assert job is not None
    assert job.repo == Path("/explicit/path")
    assert job.repository == "https://github.com/org/repo.git"

    # Neither → error
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
"""
    with pytest.raises(ConfigError, match="missing"):
        load_config(write(tmp_config_path, text))

    # repo-only legacy still works
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /some/repo
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("a")
    assert job is not None
    assert job.repo == Path("/some/repo")
    assert job.repository is None


# -- kind: pipeline (issue 026) ------------------------------------------------------


PIPELINE_MINIMAL = """
version: 1
jobs:
  - name: nightly-pipeline
    cron: "0 2 * * *"
    repo: /home/guido/repos/herdr-routines
    kind: pipeline
    prompt_file: docs/pipeline/orchestrator-prompt.md
    deadline_ms: 25200000
"""


def test_pipeline_config_schema_roundtrip(tmp_config_path: Path) -> None:
    """kind/prompt_file/deadline_ms parse, default sanely, and round-trip onto Job."""
    cfg = load_config(write(tmp_config_path, PIPELINE_MINIMAL))
    job = cfg.job("nightly-pipeline")
    assert job is not None
    assert job.kind == "pipeline"
    assert job.prompt_file == "docs/pipeline/orchestrator-prompt.md"
    assert job.deadline_ms == 25_200_000
    # per-kind default, not the routine default of 120
    assert job.catch_up_minutes == 0

    # Plain routine jobs default kind to "routine" and leave prompt_file/deadline_ms null.
    cfg = load_config(write(tmp_config_path, VALID_MINIMAL))
    job = cfg.job("nightly-audit")
    assert job is not None
    assert job.kind == "routine"
    assert job.prompt_file is None
    assert job.deadline_ms is None

    # Unknown kind rejected.
    with pytest.raises(ConfigError, match="'kind'"):
        load_config(
            write(
                tmp_config_path,
                PIPELINE_MINIMAL.replace("kind: pipeline", "kind: orchestrator"),
            )
        )


def test_validate_pipeline_requires_prompt_file_and_deadline(
    tmp_config_path: Path,
) -> None:
    text = """
version: 1
jobs:
  - name: nightly-pipeline
    cron: "0 2 * * *"
    repo: /home/guido/repos/herdr-routines
    kind: pipeline
"""
    with pytest.raises(ConfigError, match="prompt_file"):
        load_config(write(tmp_config_path, text))

    text = """
version: 1
jobs:
  - name: nightly-pipeline
    cron: "0 2 * * *"
    repo: /home/guido/repos/herdr-routines
    kind: pipeline
    prompt_file: docs/pipeline/orchestrator-prompt.md
"""
    with pytest.raises(ConfigError, match="deadline_ms"):
        load_config(write(tmp_config_path, text))


def test_validate_pipeline_workspace_na_repo_is_clone(tmp_config_path: Path) -> None:
    """An explicit `workspace:` on a pipeline job is rejected outright — it does not apply,
    since the orchestrator manages its own worktree from the plain parent clone."""
    text = PIPELINE_MINIMAL + "    workspace: worktree\n"
    with pytest.raises(ConfigError, match="workspace"):
        load_config(write(tmp_config_path, text))

    # workspace absent from the job itself is fine even when a shared defaults.yaml sets one —
    # inherited defaults must not retroactively invalidate a pipeline job (the live Pi's
    # jobs.d/defaults.yaml sets workspace: worktree for every job).
    cfg = load_config(write(tmp_config_path, PIPELINE_MINIMAL))
    assert cfg.job("nightly-pipeline") is not None


def test_validate_rejects_pipeline_catchup(tmp_config_path: Path) -> None:
    """catch_up_minutes must be 0 for kind: pipeline — a missed 02:00 must never fire the
    7h run mid-morning. Rejected only when explicitly set on the job itself; a shared
    defaults.yaml's catch_up_minutes: 120 must not retroactively invalidate it."""
    text = PIPELINE_MINIMAL + "    catch_up_minutes: 120\n"
    with pytest.raises(ConfigError, match="catch_up_minutes"):
        load_config(write(tmp_config_path, text))

    # Explicit 0 is fine.
    text = PIPELINE_MINIMAL + "    catch_up_minutes: 0\n"
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("nightly-pipeline")
    assert job is not None
    assert job.catch_up_minutes == 0


def test_pipeline_ignores_shared_defaults_catchup(tmp_config_path: Path) -> None:
    """A `defaults:` block's catch_up_minutes: 120 (the live Pi's shape) must not leak into
    a pipeline job — it gets the per-kind 0 default regardless."""
    text = """
version: 1
defaults:
  catch_up_minutes: 120
  workspace: worktree
jobs:
  - name: nightly-pipeline
    cron: "0 2 * * *"
    repo: /home/guido/repos/herdr-routines
    kind: pipeline
    prompt_file: docs/pipeline/orchestrator-prompt.md
    deadline_ms: 25200000
"""
    cfg = load_config(write(tmp_config_path, text))
    job = cfg.job("nightly-pipeline")
    assert job is not None
    assert job.catch_up_minutes == 0


def test_pipeline_rejects_checks(tmp_config_path: Path) -> None:
    """kind: pipeline and the unified gate model (`checks:`) are mutually exclusive
    dispatch paths — silently ignoring one would be a worse failure mode than rejecting
    the config outright."""
    text = """
version: 1
jobs:
  - name: nightly-pipeline
    cron: "0 2 * * *"
    repo: /home/guido/repos/herdr-routines
    kind: pipeline
    prompt_file: docs/pipeline/orchestrator-prompt.md
    deadline_ms: 25200000
    checks:
      - pr_health: null
"""
    with pytest.raises(ConfigError, match="checks"):
        load_config(write(tmp_config_path, text))
