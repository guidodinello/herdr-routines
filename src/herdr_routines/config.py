"""Config loading and validation: YAML -> Job dataclasses.

Pure: no filesystem access beyond reading YAML files, no subprocess, no clock reads
(the caller supplies `now` where it matters). This is what makes it fully unit-testable.

Supports two config layouts:
- Legacy single file: ``jobs.yaml`` (deprecated, emits a warning when used).
- Directory layout: ``jobs.d/`` with one ``<name>.yaml`` per job, an optional
  ``defaults.yaml`` for shared fields, and an optional ``retention.yaml`` for the
  state-dir retention policy (issue 021).  The loader picks directory over file
  when both exist.
"""

from __future__ import annotations

import os
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from croniter import croniter
from logger import get_logger

log = get_logger(__name__)

# Kinds documented by `herdr agent` --help on herdr 0.8.2 (see docs/plan-v1.md).
VALID_AGENT_KINDS = frozenset(
    {
        "pi",
        "claude",
        "codex",
        "gemini",
        "cursor",
        "devin",
        "agy",
        "cline",
        "omp",
        "mastracode",
        "opencode",
        "copilot",
        "kimi",
        "kiro",
        "droid",
        "amp",
        "grok",
        "hermes",
        "kilo",
        "qodercli",
        "qwen",
        "maki",
    }
)

# Native model-selection flag per agent kind, passed as a native arg after `--`. Confirmed
# empirically against herdr 0.8.2 (see docs/plan-v1.md): only these kinds have a pinned-down
# flag — a job's 'model' is rejected for any other agent_kind rather than guessing. Also consumed
# by herdr.py's `build_agent_start_args`, which is where the flag is actually applied.
# codex 0.52.0 --help: --model <model> (confirmed 2026-09-14)
AGENT_MODEL_FLAGS: dict[str, str] = {
    "claude": "--model",
    "opencode": "-m",
    "codex": "--model",
}

VALID_WORKSPACE_MODES = frozenset({"worktree", "root"})
VALID_ON_MISSED = frozenset({"log", "notify"})

# Per-job notification policy (issue 009). Governs the terminal-state `_notify()` calls
# in tick.py — not `on_missed`, which is its own pre-existing opt-in and stays orthogonal
# (see tick.py's `_notify_gate` docstring for the full four-tier semantics). "terminal"
# is the default: it reproduces pre-issue-009 behavior exactly (one notification per
# run's terminal outcome — done or failed — never a mid-run progress ping), which is
# what "defaulting to a single terminal-state notification" in the issue's acceptance
# criteria requires. "always" > "terminal" > "on-finding" > "on-failure" is a strict
# hierarchy, each a subset of the last.
VALID_NOTIFY_POLICIES = frozenset({"always", "terminal", "on-finding", "on-failure"})

# Single dispatch key (issue 049). "routine" (default) = plain unconditional execute_run;
# "gated" = gate checks + fix dispatch (_process_gated_job); "pipeline" = detached
# systemd-run launch (_process_pipeline_job); "audit" = report-diff gate job (issue 057):
# schedule an audit skill, parse its findings manifest, diff against the ledger, and
# dispatch fixes only for new/regressed findings. Gate mode is no longer an orthogonal
# axis:
# (the old `checks is not None` discriminator); kind is the exhaustive SSOT.
VALID_JOB_KINDS = frozenset({"routine", "gated", "pipeline", "audit"})

# Closed set of known terminal RunOutcome.reason values eligible for retry.
# Derived from runner.py + tick.py failure sites. Unknown strings in retry_on
# raise ConfigError to catch typos and drift as new reasons are added.
VALID_RETRY_REASONS = frozenset(
    {
        "agent_start_failed",
        "agent_not_interactive",
        "pane_creation_failed",
        "clone_failed",
        "repo_sync_failed",
        "agent_prompt_failed",
        "tmp_full",
        "report_dir_creation_failed",
        "blocked",
        "no_report",
        "quota_exhausted",
        # Note: `interrupted_unknown` and `unsettled_status_unknown` are excluded —
        # they are emitted with state="interrupted_unknown", not "failed", so
        # _retry_eligible() (which requires state == "failed") can never match them.
    }
)

# A pipeline job's fixed catch_up_minutes. NOT 0: `schedule.decide()`'s grace window is a
# strict `late <= grace`, and tick only samples every 5 minutes and evaluates jobs
# sequentially under one lock — a job listed after a slow gated job (e.g. babysit-prs
# dispatching PR-fix workers) can easily be evaluated 30-60+ minutes after its cron
# instant on an ordinary night. `catch_up_minutes: 0` would then report MISSED on
# essentially every run, not just genuine multi-hour outages (confirmed empirically:
# test_schedule.py's own test_catch_up_zero_means_no_backfill_at_all shows even 30s late
# with grace 0 -> MISSED). This value only needs to comfortably exceed realistic
# same-night tick delay while staying far short of "into the workday" — see the issue's
# log for the incident this corrects (an earlier revision required exactly 0, audited but
# never run against a real multi-job tick).
PIPELINE_CATCH_UP_MINUTES = 60

# Job name feeds the live agent name as f"rt-{name}", and Herdr caps agent names at 32 chars
# matching [a-z][a-z0-9_-]{0,31}. "rt-" costs 3, so the job name gets 24.
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")

_DEFAULTS_ALLOWED_KEYS = frozenset(
    {
        "agent_kind",
        "workspace",
        "timeout_ms",
        "start_timeout_ms",
        "catch_up_minutes",
        "timezone",
        "on_missed",
        "failure_markers",
        "fallback_model",
        "tmp_hygiene",
        "notify_policy",
    }
)

_JOB_REQUIRED_KEYS = frozenset({"name", "cron"})
_RETENTION_ALLOWED_KEYS = frozenset(
    {"history_max_bytes", "history_max_age_days", "reports_max_age_days"}
)
# Recognized non-job files in a jobs.d/ directory. `defaults.yaml` was already special-cased
# in load_config_dir; `retention.yaml` is its issue-021 twin (the directory-layout spelling
# of the top-level `retention:` block).
_RETENTION_FILENAME = "retention.yaml"
_NON_JOB_FILENAMES = frozenset({"defaults.yaml", _RETENTION_FILENAME})
_JOB_ALLOWED_KEYS = (
    _JOB_REQUIRED_KEYS
    | _DEFAULTS_ALLOWED_KEYS
    | frozenset(
        {
            "enabled",
            "base",
            "model",
            "prompt",
            "checks",
            "target",
            "max_workers_per_tick",
            "max_attempts_per_target",
            "repo",
            "repository",
            "kind",
            "prompt_file",
            "deadline_ms",
            "tmp_hygiene",
            "retry_attempts",
            "retry_on",
            # kind: audit (issue 057). Deliberately NOT in _DEFAULTS_ALLOWED_KEYS:
            # an audit's identity (which skill/command) and its budget are per-job.
            "audit",
            "max_findings_per_dispatch",
            "ledger_retention_days",
            "adopt_baseline",
            "fix_prompt",
        }
    )
)

VALID_CHECK_KINDS = frozenset({"pr_health", "command"})
VALID_TARGETS = frozenset({"pr", "base"})

_JOB_DEFAULTS = {
    "enabled": True,
    "agent_kind": "claude",
    "workspace": "worktree",
    "base": "main",
    "model": None,
    "repo": None,
    "prompt": "",
    "timeout_ms": 1_800_000,
    "start_timeout_ms": 120_000,
    "catch_up_minutes": 120,
    "timezone": "UTC",
    "on_missed": "log",
    "notify_policy": "terminal",
    "failure_markers": None,
    "fallback_model": None,
    "checks": None,
    "target": None,
    "max_workers_per_tick": 3,
    "max_attempts_per_target": 3,
    "repository": None,
    "kind": "routine",
    "prompt_file": None,
    "deadline_ms": None,
    "tmp_hygiene": None,
    "retry_attempts": 0,
    "retry_on": None,
    # kind: audit (issue 057). None for every other kind.
    "audit": None,
    "max_findings_per_dispatch": 10,
    "ledger_retention_days": 90,
    "adopt_baseline": True,
    "fix_prompt": "",
}


class ConfigError(ValueError):
    """Raised for any problem with jobs.yaml — unknown keys, bad cron, duplicate names, etc."""


DEFAULT_TMP_DIR = "/tmp"
DEFAULT_MAX_AGE_S = 3600

# Retention defaults (issue 021). Rotation is OFF by default: the mechanism ships so an
# operator can turn it on when the day comes, not so it runs on day one. The prune window
# is generous on purpose — it only ever applies to an explicit `prune reports --yes`.
DEFAULT_REPORTS_MAX_AGE_DAYS = 90

# `history_max_bytes` under this would rotate on essentially every tick (a 5-minute tick
# appending a handful of lines per run crosses 1 MiB in well under a day). A soft
# `validate` warning, not a load error — same posture as the $ROUTINE_REPORT checks.
TINY_HISTORY_MAX_BYTES = 1_048_576  # 1 MiB


@dataclass(frozen=True, slots=True)
class TmphgieneConfig:
    """Configuration for /tmp age-based cleanup (issue 027)."""

    enabled: bool = True
    max_age_s: int = DEFAULT_MAX_AGE_S
    tmp_dir: str = DEFAULT_TMP_DIR


@dataclass(frozen=True, slots=True)
class Retention:
    """State-dir retention policy (issue 021).

    A top-level block, deliberately NOT a per-job key and NOT a `defaults:` entry: rotation
    is a property of *one* file, so merging it under every job would be semantically wrong.

      history_max_bytes:    roll history.jsonl past this size. None = off.
      history_max_age_days: roll it once its oldest record is this old. None = off.
      reports_max_age_days: default window for `prune reports`.
    """

    history_max_bytes: int | None = None
    history_max_age_days: int | None = None
    reports_max_age_days: int = DEFAULT_REPORTS_MAX_AGE_DAYS


@dataclass(frozen=True, slots=True)
class GateCheck:
    """A single check in the unified gate model."""

    kind: str  # "pr_health" | "command"
    command: str | None = None
    timeout_ms: int = 120_000


@dataclass(frozen=True, slots=True)
class AuditSpec:
    """The audit half of a kind: audit job (issue 057 Phase A).

    Exactly one of ``skill``/``command`` is set (validated at load):
    - ``skill`` names a Claude/agent skill the run should execute (e.g. ``type-health``).
    - ``command`` is a shell command whose stdout is the audit.

    Both write the report to ``$ROUTINE_REPORT`` and the findings manifest to
    ``$ROUTINE_FINDINGS``. ``timeout_ms`` bounds the audit run itself, distinct from
    the fix worker's ``Job.timeout_ms``."""

    skill: str | None = None
    command: str | None = None
    timeout_ms: int = 1_800_000


@dataclass(frozen=True, slots=True)
class Job:
    name: str
    enabled: bool
    cron: str
    repo: Path
    workspace: str  # "worktree" | "root"
    base: str
    agent_kind: str
    model: str | None
    prompt: str
    timeout_ms: int
    start_timeout_ms: int
    catch_up_minutes: int
    timezone: str
    on_missed: str  # "log" | "notify"
    # Notification policy (issue 009): "always" | "terminal" (default) | "on-finding" |
    # "on-failure". Governs tick.py's terminal-state `_notify()` calls; `on_missed` is
    # unaffected — see tick._notify_gate.
    notify_policy: str = "terminal"
    # Optional git URL for managed clone lifecycle.
    repository: str | None = None
    # Screen markers scanned after a failed prompt wait (docs/failure-reaping.md §3.2).
    # None = runner.DEFAULT_FAILURE_MARKERS.
    failure_markers: tuple[str, ...] | None = None
    # Model retried once, under a fresh run_id, when the primary run fails with reason
    # "quota_exhausted" (tick._process_job). None = no fallback attempt. Usually set under
    # `defaults:` so every job sharing a provider's free-tier pool shares one fallback provider.
    fallback_model: str | None = None
    # Unified gate model: ordered checks that gate the fix agent (None = plain job).
    checks: tuple[GateCheck, ...] | None = None
    # Inferred from check kinds or explicit override: "pr" | "base".
    target: str | None = None
    # Dispatch cap per tick.
    max_workers_per_tick: int = 3
    # Retry budget keyed per target (per gate branch for base, per PR number for pr).
    max_attempts_per_target: int = 3
    # Scheduling-only mode discriminator (issue 026, unified in 049):
    # "routine" | "gated" | "pipeline" | "audit". kind is the exhaustive single source
    # of truth for how a job is dispatched; it never serializes into history.jsonl
    # (records store job name / state / run_id / outcome extras; status/scheduled join
    # live config + history by job name only — see cli.py:432-434 / cli.py:481).
    kind: Literal["routine", "gated", "pipeline", "audit"] = "routine"
    # Prompt source file, read by the pipeline launcher script (not by `load_config`).
    # Required for kind: pipeline; a plain routine may also use it as an I/O convenience.
    prompt_file: str | None = None
    # Orchestrator wall-clock budget in ms, feeding the generated systemd unit's
    # RuntimeMaxSec and the reconcile-staleness bound in tick._process_pipeline_job.
    # Required for kind: pipeline; unused for kind: routine (job.timeout_ms applies instead).
    deadline_ms: int | None = None
    # Optional /tmp hygiene config (issue 027). None = use global defaults.
    tmp_hygiene: TmphgieneConfig | None = None
    # Per-job retry for transient failures (issue 008): total extra attempts after
    # the first failure. 0 = no retries (default). NOT inheritable via defaults.yaml.
    retry_attempts: int = 0
    # Whitelist of RunOutcome.reason values eligible for retry. None = no eligible
    # reasons (so retry_attempts is inert). NOT inheritable via defaults.yaml.
    retry_on: tuple[str, ...] | None = None
    # -- kind: audit (issue 057) ---------------------------------------------------
    # The audit command/skill and its own timeout. Required for kind: audit, None
    # otherwise. NOT inheritable via defaults.yaml (per-job identity).
    audit: AuditSpec | None = None
    # Max findings dispatched for a fix in a single cycle (the rest wait, most severe
    # first). Bounds the PR blast radius of one audit night.
    max_findings_per_dispatch: int = 10
    # Resolved-ledger entries older than this are pruned. Open entries are never pruned.
    ledger_retention_days: int = 90
    # On a cold start (no ledger): adopt the current findings as the baseline and
    # dispatch nothing. False = treat the first report as the first dispatch set.
    adopt_baseline: bool = True
    # The fix worker's prompt template, seeded with $ROUTINE_FINDINGS. Empty = the
    # built-in default. NOT inheritable via defaults.yaml.
    fix_prompt: str = ""

    @property
    def agent_name(self) -> str:
        return f"rt-{self.name}"


@dataclass(frozen=True, slots=True)
class RoutinesConfig:
    jobs: tuple[Job, ...] = field(default_factory=tuple)
    # Per-file errors from directory loader (empty for single-file or clean directory load).
    errors: tuple[str, ...] = field(default_factory=tuple)
    # Rotation / prune policy (issue 021). Defaults mean "rotate nothing, prune at 90 days
    # if explicitly asked" — a config that never mentions `retention:` behaves exactly as
    # it did before this block existed.
    retention: Retention = field(default_factory=Retention)

    def job(self, name: str) -> Job | None:
        for j in self.jobs:
            if j.name == name:
                return j
        return None


def default_repos_dir() -> Path:
    """Resolve the managed repos base dir, following the same
    ``HERDR_PLUGIN_STATE_DIR`` pattern as ``history.default_history_path``."""
    plugin_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    base = (
        Path(plugin_dir)
        if plugin_dir
        else Path.home() / ".local" / "state" / "herdr-routines"
    )
    return base / "repos"


def default_config_path() -> Path:
    """Resolve the config base path: ``--config``, ``$HERDR_PLUGIN_CONFIG_DIR/jobs.d``,
    or ``~/.config/herdr-routines/jobs.d``.

    The returned path may be a *directory* (``jobs.d/`` layout) or a *file* (legacy
    ``jobs.yaml``).  Callers should pass it straight to :func:`load_config` which
    auto-detects the shape.

    The middle entry is forethought for the optional plugin manifest described in
    docs/plan-v1.md §8.4 — it costs nothing now and keeps that door open later.
    """
    plugin_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if plugin_dir:
        return Path(plugin_dir) / "jobs.d"
    return Path.home() / ".config" / "herdr-routines" / "jobs.d"


def load_config(path: Path) -> RoutinesConfig:
    """Load and fully validate config from *path*.

    *path* may point to a **directory** (``jobs.d/`` layout) or a **file** (legacy
    ``jobs.yaml``).  When it is a directory, :func:`load_config_dir` is used.  When it
    is a file, the legacy single-file loader runs with a deprecation warning.

    Raises :class:`ConfigError` on any problem.
    """
    if path.is_dir():
        return load_config_dir(path)

    # Legacy single-file path — still supported but deprecated.
    warnings.warn(
        f"Loading config from a single file ({path}) is deprecated. "
        "Migrate to a jobs.d/ directory layout.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _load_config_file(path)


def _load_config_file(path: Path) -> RoutinesConfig:
    """Legacy single-file loader (``jobs.yaml``).  Raises :class:`ConfigError` on any problem."""
    try:
        raw_text = path.read_text()
    except OSError as e:
        raise ConfigError(f"cannot read config file {path}: {e}") from e

    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top-level document must be a mapping")

    version = raw.get("version", 1)
    if version != 1:
        raise ConfigError(
            f"unsupported config version: {version!r} (only 1 is supported)"
        )

    unknown_top = set(raw) - {"version", "defaults", "jobs", "retention"}
    if unknown_top:
        raise ConfigError(f"unknown top-level key(s): {sorted(unknown_top)}")

    raw_retention = raw.get("retention") or {}
    if not isinstance(raw_retention, dict):
        raise ConfigError("'retention' must be a mapping")
    retention = _build_retention(raw_retention, "retention")

    raw_defaults = raw.get("defaults") or {}
    if not isinstance(raw_defaults, dict):
        raise ConfigError("'defaults' must be a mapping")
    unknown_defaults = set(raw_defaults) - _DEFAULTS_ALLOWED_KEYS
    if unknown_defaults:
        raise ConfigError(
            f"unknown key(s) under 'defaults': {sorted(unknown_defaults)}"
        )

    raw_jobs = raw.get("jobs") or []
    if not isinstance(raw_jobs, list):
        raise ConfigError("'jobs' must be a list")

    jobs: list[Job] = []
    seen_names: set[str] = set()
    for i, raw_job in enumerate(raw_jobs):
        if not isinstance(raw_job, dict):
            raise ConfigError(f"jobs[{i}] must be a mapping")
        job = _build_job(raw_job, raw_defaults, index=i)
        if job.name in seen_names:
            raise ConfigError(f"duplicate job name: {job.name!r}")
        seen_names.add(job.name)
        jobs.append(job)

    return RoutinesConfig(jobs=tuple(jobs), retention=retention)


def load_config_dir(path: Path) -> RoutinesConfig:
    """Load config from a ``jobs.d/`` directory layout.

    Directory contract::

        <path>/
            defaults.yaml    # optional; shared fields merged under each job
            retention.yaml   # optional; rotation / prune policy (issue 021)
            <name>.yaml      # one per job; filename stem is the canonical name

    Discovery is deterministic (``sorted()`` glob).  ``defaults.yaml`` and
    ``retention.yaml`` are excluded from the job file list.

    Per-file YAML syntax errors surface the file name and do **not** prevent other
    jobs from loading (for diagnostics).  Unknown keys or bad values in one file also
    skip that file and continue.

    Raises :class:`ConfigError` if the directory does not exist.
    """
    if not path.is_dir():
        raise ConfigError(f"config directory does not exist: {path}")

    # --- load defaults.yaml (optional) -------------------------------------------
    defaults_path = path / "defaults.yaml"
    raw_defaults: dict = {}
    if defaults_path.exists():
        raw_defaults = _load_yaml_or_error(defaults_path, kind="defaults")
        _validate_defaults_keys(raw_defaults, defaults_path)

    # --- load retention.yaml (optional, issue 021) -------------------------------
    retention_path = path / _RETENTION_FILENAME
    retention = Retention()
    if retention_path.exists():
        raw_retention = _load_yaml_or_error(retention_path, kind="retention")
        retention = _build_retention(raw_retention, str(retention_path))

    # --- discover job files (sorted, exclude the two non-job files) ---------------
    job_files = sorted(
        p for p in path.glob("*.yaml") if p.name not in _NON_JOB_FILENAMES
    )

    jobs: list[Job] = []
    seen_names: set[str] = set()
    errors: list[str] = []

    for job_file in job_files:
        try:
            raw_job = _load_yaml_or_error(job_file, kind="job")
        except ConfigError as e:
            errors.append(str(e))
            continue

        if not isinstance(raw_job, dict):
            errors.append(f"{job_file}: job file must be a mapping")
            continue

        # --- filename / name contract -------------------------------------------
        stem = job_file.stem
        if not NAME_RE.match(stem):
            errors.append(
                f"{job_file}: filename stem {stem!r} does not match {NAME_RE.pattern}"
            )
            continue

        name_in_file = raw_job.get("name")
        if name_in_file is not None:
            if not isinstance(name_in_file, str):
                errors.append(f"{job_file}: 'name' must be a string")
                continue
            if name_in_file != stem:
                errors.append(
                    f"{job_file}: 'name' key {name_in_file!r} does not match "
                    f"filename stem {stem!r}"
                )
                continue
        else:
            # No 'name' key — filename stem is the canonical name.
            raw_job["name"] = stem

        try:
            job = _build_job(
                raw_job, raw_defaults, index=len(jobs), label_prefix=job_file.name
            )
        except ConfigError as e:
            errors.append(str(e))
            continue

        # Defensive: the filename=name contract (stem == name) makes duplicates
        # impossible across different files, but this guard stays as a safety net
        # in case the contract is relaxed in the future.
        if job.name in seen_names:
            errors.append(f"duplicate job name: {job.name!r} (from {job_file})")
            continue
        seen_names.add(job.name)
        jobs.append(job)

    if errors:
        for err in errors:
            log.warning("config: %s", err)

    return RoutinesConfig(jobs=tuple(jobs), errors=tuple(errors), retention=retention)


def _load_yaml_or_error(path: Path, *, kind: str) -> dict:
    """Read and parse a single YAML file.  Raises :class:`ConfigError` with the file
    name embedded for clear diagnostics.  *kind* ("defaults" | "retention" | "job") only
    shapes the error message."""
    try:
        text = path.read_text()
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: YAML syntax error: {e}") from e
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path}: {kind} file must be a mapping, got {type(raw).__name__}"
        )
    return raw


def _validate_defaults_keys(raw: dict, path: Path) -> None:
    """Reject unknown keys in ``defaults.yaml`` (same contract as the legacy top-level
    ``defaults:`` block)."""
    unknown = set(raw) - _DEFAULTS_ALLOWED_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown key(s): {sorted(unknown)}")


def _validate_retention_keys(raw: dict, label: str) -> None:
    """Reject unknown keys under `retention` (same contract for the top-level block and for
    ``retention.yaml``). *label* is the file path, or "retention" for the inline block."""
    unknown = set(raw) - _RETENTION_ALLOWED_KEYS
    if unknown:
        raise ConfigError(f"{label}: unknown key(s): {sorted(unknown)}")


def _build_retention(raw: dict, label: str) -> Retention:
    """Build a :class:`Retention` from a parsed retention mapping. Validates the unknown key
    set first, so a typo is reported as a typo rather than as a type error on `None`."""
    _validate_retention_keys(raw, label)

    def _optional_positive_int(key: str) -> int | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"{label}: '{key}' must be a positive integer or null")
        return value

    reports_max_age_days = raw.get("reports_max_age_days", DEFAULT_REPORTS_MAX_AGE_DAYS)
    if (
        not isinstance(reports_max_age_days, int)
        or isinstance(reports_max_age_days, bool)
        or reports_max_age_days <= 0
    ):
        raise ConfigError(f"{label}: 'reports_max_age_days' must be a positive integer")
    return Retention(
        history_max_bytes=_optional_positive_int("history_max_bytes"),
        history_max_age_days=_optional_positive_int("history_max_age_days"),
        reports_max_age_days=reports_max_age_days,
    )


_REPO_URL_PREFIXES = ("https://", "ssh://", "git@", "git://")


def _is_valid_repo_url(value: str) -> bool:
    """Check if value looks like a git URL (not a bare path)."""
    return any(value.startswith(p) for p in _REPO_URL_PREFIXES)


def _build_job(
    raw_job: dict, defaults: dict, *, index: int, label_prefix: str | None = None
) -> Job:
    unknown = set(raw_job) - _JOB_ALLOWED_KEYS
    if unknown:
        prefix = label_prefix or f"jobs[{index}]"
        raise ConfigError(f"{prefix}: unknown key(s): {sorted(unknown)}")

    missing = _JOB_REQUIRED_KEYS - set(raw_job)
    if missing:
        prefix = label_prefix or f"jobs[{index}]"
        raise ConfigError(f"{prefix}: missing required key(s): {sorted(missing)}")

    merged = {**_JOB_DEFAULTS, **defaults, **raw_job}
    name = merged["name"]
    if label_prefix:
        label = f"{label_prefix} ({name!r})" if isinstance(name, str) else label_prefix
    else:
        label = (
            f"jobs[{index}] ({name!r})" if isinstance(name, str) else f"jobs[{index}]"
        )

    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ConfigError(
            f"{label}: 'name' must match {NAME_RE.pattern} (max 24 chars, "
            "since the live agent name is 'rt-<name>' and Herdr caps names at 32)"
        )

    cron = merged["cron"]
    if not isinstance(cron, str):
        raise ConfigError(f"{label}: 'cron' must be a string")
    try:
        croniter(cron)
    except (ValueError, KeyError) as e:
        raise ConfigError(f"{label}: invalid cron expression {cron!r}: {e}") from e

    repo_raw = merged["repo"]
    repository = merged["repository"]

    # URL-shape validation for repository
    if repository is not None:
        if not isinstance(repository, str) or not repository:
            raise ConfigError(f"{label}: 'repository' must be a non-empty string")
        if not _is_valid_repo_url(repository):
            raise ConfigError(
                f"{label}: 'repository' must be a git URL "
                "(https://, ssh://, git@host:, or git://) — bare paths rejected"
            )

    # Conditional repo requirement
    if repo_raw is None and repository is None:
        raise ConfigError(f"{label}: missing 'repo' or 'repository'")

    if repo_raw is None:
        # repository present, repo absent → derive deterministic path
        from herdr_routines.repos import default_repos_dir

        repo = default_repos_dir() / name
    else:
        if not isinstance(repo_raw, str) or not repo_raw:
            raise ConfigError(f"{label}: 'repo' must be a non-empty string path")
        repo = Path(repo_raw).expanduser()

    workspace = merged["workspace"]
    if workspace not in VALID_WORKSPACE_MODES:
        raise ConfigError(
            f"{label}: 'workspace' must be one of {sorted(VALID_WORKSPACE_MODES)}"
        )

    agent_kind = merged["agent_kind"]
    if agent_kind not in VALID_AGENT_KINDS:
        raise ConfigError(
            f"{label}: 'agent_kind' must be one of {sorted(VALID_AGENT_KINDS)}"
        )

    on_missed = merged["on_missed"]
    if on_missed not in VALID_ON_MISSED:
        raise ConfigError(
            f"{label}: 'on_missed' must be one of {sorted(VALID_ON_MISSED)}"
        )

    notify_policy = merged["notify_policy"]
    if notify_policy not in VALID_NOTIFY_POLICIES:
        raise ConfigError(
            f"{label}: 'notify_policy' must be one of {sorted(VALID_NOTIFY_POLICIES)}"
        )

    for int_key in ("timeout_ms", "start_timeout_ms", "catch_up_minutes"):
        value = merged[int_key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"{label}: '{int_key}' must be a non-negative integer")

    enabled = merged["enabled"]
    if not isinstance(enabled, bool):
        raise ConfigError(f"{label}: 'enabled' must be a boolean")

    model = merged["model"]
    if model is not None and not isinstance(model, str):
        raise ConfigError(f"{label}: 'model' must be a string or null")
    if model is not None and agent_kind not in AGENT_MODEL_FLAGS:
        raise ConfigError(
            f"{label}: 'model' is not supported for agent_kind {agent_kind!r} "
            f"(supported: {sorted(AGENT_MODEL_FLAGS)})"
        )

    fallback_model = merged["fallback_model"]
    if fallback_model is not None and not isinstance(fallback_model, str):
        raise ConfigError(f"{label}: 'fallback_model' must be a string or null")
    if fallback_model is not None and agent_kind not in AGENT_MODEL_FLAGS:
        if "fallback_model" in raw_job:
            # The job itself asked for this — an explicit mistake, not something a shared
            # default should paper over.
            raise ConfigError(
                f"{label}: 'fallback_model' is not supported for agent_kind {agent_kind!r} "
                f"(supported: {sorted(AGENT_MODEL_FLAGS)})"
            )
        # Inherited from defaults.yaml, which is deliberately shared across every job
        # (unlike `model`) so one entry can cover a whole provider's job set. A job whose
        # agent_kind doesn't support model selection at all shouldn't fail to load just
        # because the default doesn't apply to it — it's simply inert here.
        fallback_model = None

    prompt = merged["prompt"]
    if not isinstance(prompt, str):
        raise ConfigError(f"{label}: 'prompt' must be a string")

    timezone = merged["timezone"]
    if not isinstance(timezone, str):
        raise ConfigError(f"{label}: 'timezone' must be a string")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as e:
        raise ConfigError(
            f"{label}: 'timezone' is not a valid IANA zone: {timezone!r}"
        ) from e

    base = merged["base"]
    if not isinstance(base, str):
        raise ConfigError(f"{label}: 'base' must be a string")

    failure_markers_raw = merged["failure_markers"]
    failure_markers: tuple[str, ...] | None = None
    if failure_markers_raw is not None:
        if not isinstance(failure_markers_raw, list) or not all(
            isinstance(m, str) and m for m in failure_markers_raw
        ):
            raise ConfigError(
                f"{label}: 'failure_markers' must be null or a list of non-empty strings"
            )
        failure_markers = tuple(failure_markers_raw)

    # -- Unified gate model: checks / target / max_workers / max_attempts -----------

    checks_raw = merged.get("checks")
    checks: tuple[GateCheck, ...] | None = None
    inferred_target: str | None = None

    if checks_raw is not None:
        if not isinstance(checks_raw, list):
            raise ConfigError(f"{label}: 'checks' must be a list or null")
        if len(checks_raw) == 0:
            checks = None
        else:
            parsed_checks: list[GateCheck] = []
            has_pr_health = False
            has_command = False
            for ci, c in enumerate(checks_raw):
                if not isinstance(c, dict):
                    raise ConfigError(f"{label}: 'checks[{ci}]' must be a mapping")
                unknown_ck = set(c) - {"pr_health", "command", "timeout_ms"}
                if unknown_ck:
                    raise ConfigError(
                        f"{label}: 'checks[{ci}]' has unknown key(s): {sorted(unknown_ck)}"
                    )
                if "pr_health" in c and "command" in c:
                    raise ConfigError(
                        f"{label}: 'checks[{ci}]' cannot have both 'pr_health' and 'command'"
                    )
                if "pr_health" not in c and "command" not in c:
                    raise ConfigError(
                        f"{label}: 'checks[{ci}]' must have either 'pr_health' or 'command'"
                    )
                if "pr_health" in c:
                    has_pr_health = True
                    parsed_checks.append(GateCheck(kind="pr_health"))
                else:
                    has_command = True
                    cmd = c["command"]
                    if not isinstance(cmd, str) or not cmd:
                        raise ConfigError(
                            f"{label}: 'checks[{ci}].command' must be a non-empty string"
                        )
                    ct = c.get("timeout_ms", 120_000)
                    if not isinstance(ct, int) or isinstance(ct, bool) or ct <= 0:
                        raise ConfigError(
                            f"{label}: 'checks[{ci}].timeout_ms' must be a positive integer"
                        )
                    parsed_checks.append(
                        GateCheck(kind="command", command=cmd, timeout_ms=ct)
                    )

            if has_pr_health and has_command:
                raise ConfigError(
                    f"{label}: 'checks' cannot mix 'pr_health' and 'command' kinds"
                )

            checks = tuple(parsed_checks)
            inferred_target = "pr" if has_pr_health else "base"

    for int_key in ("max_workers_per_tick", "max_attempts_per_target"):
        value = merged[int_key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"{label}: '{int_key}' must be a non-negative integer")

    # -- kind (issue 049: kind is the exhaustive single dispatch key) ----------------
    #
    # Rules key off `raw_job`, never the merged value: a shared `defaults.yaml` (e.g. the
    # live Pi's `workspace: worktree` + `catch_up_minutes: 120`) must not retroactively
    # invalidate every pipeline job — only an *explicit* per-job setting is rejected.

    kind = merged["kind"]
    if kind not in VALID_JOB_KINDS:
        raise ConfigError(f"{label}: 'kind' must be one of {sorted(VALID_JOB_KINDS)}")

    # -- kind: gated requires non-empty checks -------------------------------------------
    if kind == "gated" and (checks is None or len(checks) == 0):
        raise ConfigError(f"{label}: 'kind: gated' requires a non-empty 'checks' list")

    # -- checks present but kind is not gated → must migrate -----------------------------
    if checks is not None and kind != "gated":
        raise ConfigError(
            f"{label}: 'checks' requires kind: gated "
            "(a job with checks is a gated workflow; add kind: gated)"
        )

    # -- kind: audit (issue 057 Phase A) ------------------------------------------------
    #
    # An audit is a report-diff gate: it schedules an audit skill/command, parses the
    # resulting findings manifest, and diffs against a ledger. Unlike a gated job its
    # "gate" is the diff, not a checks list, and it always targets base.
    audit: AuditSpec | None = None
    fix_prompt = merged.get("fix_prompt", "")
    if not isinstance(fix_prompt, str):
        raise ConfigError(f"{label}: 'fix_prompt' must be a string")

    if kind == "audit":
        if merged["workspace"] != "worktree":
            raise ConfigError(
                f"{label}: 'workspace' must be 'worktree' for kind: audit "
                "(the fix worker edits source; an audit never runs in a live checkout)"
            )
        target_raw_for_audit = merged.get("target")
        if target_raw_for_audit is not None and target_raw_for_audit != "base":
            raise ConfigError(f"{label}: 'target' must be 'base' for kind: audit")
        if "max_workers_per_tick" in raw_job:
            raise ConfigError(
                f"{label}: 'max_workers_per_tick' is not applicable to kind: audit "
                "(use 'max_findings_per_dispatch')"
            )

        audit_raw = merged["audit"]
        if audit_raw is None:
            raise ConfigError(f"{label}: kind: audit requires an 'audit' mapping")
        if not isinstance(audit_raw, dict):
            raise ConfigError(f"{label}: 'audit' must be a mapping")
        unknown_audit = set(audit_raw) - {"skill", "command", "timeout_ms"}
        if unknown_audit:
            raise ConfigError(
                f"{label}: 'audit' has unknown key(s): {sorted(unknown_audit)}"
            )
        audit_skill = audit_raw.get("skill")
        audit_command = audit_raw.get("command")
        if (audit_skill is None) == (audit_command is None):
            raise ConfigError(
                f"{label}: 'audit' requires exactly one of 'skill'/'command'"
            )
        if audit_skill is not None and (
            not isinstance(audit_skill, str) or not audit_skill
        ):
            raise ConfigError(f"{label}: 'audit.skill' must be a non-empty string")
        if audit_command is not None and (
            not isinstance(audit_command, str) or not audit_command
        ):
            raise ConfigError(f"{label}: 'audit.command' must be a non-empty string")
        audit_timeout = audit_raw.get("timeout_ms", 1_800_000)
        if (
            not isinstance(audit_timeout, int)
            or isinstance(audit_timeout, bool)
            or audit_timeout <= 0
        ):
            raise ConfigError(f"{label}: 'audit.timeout_ms' must be a positive integer")
        audit = AuditSpec(
            skill=audit_skill, command=audit_command, timeout_ms=audit_timeout
        )

        for int_key in (
            "max_findings_per_dispatch",
            "max_attempts_per_target",
            "ledger_retention_days",
        ):
            value = merged[int_key]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigError(f"{label}: '{int_key}' must be a positive integer")

        if not isinstance(merged["adopt_baseline"], bool):
            raise ConfigError(f"{label}: 'adopt_baseline' must be a boolean")
    elif "audit" in raw_job:
        raise ConfigError(f"{label}: 'audit' requires kind: audit")

    if kind == "pipeline":
        if checks is not None:
            raise ConfigError(
                f"{label}: 'checks' is not applicable to kind: pipeline "
                "(gate mode and pipeline mode are mutually exclusive dispatch paths)"
            )
        if "workspace" in raw_job:
            raise ConfigError(
                f"{label}: 'workspace' is not applicable to kind: pipeline "
                "(the orchestrator owns its own worktree, created from the parent clone)"
            )
        if "catch_up_minutes" in raw_job:
            raise ConfigError(
                f"{label}: 'catch_up_minutes' is not applicable to kind: pipeline "
                f"(fixed at PIPELINE_CATCH_UP_MINUTES={PIPELINE_CATCH_UP_MINUTES} — "
                "enough to absorb ordinary same-night tick delay, not to fire late into "
                "the workday; remove the key)"
            )
        # Fixed per-kind value, independent of defaults.yaml's shared value.
        catch_up_minutes = PIPELINE_CATCH_UP_MINUTES

        prompt_file = merged["prompt_file"]
        if not isinstance(prompt_file, str) or not prompt_file:
            raise ConfigError(f"{label}: 'prompt_file' is required for kind: pipeline")

        deadline_ms = merged["deadline_ms"]
        if (
            not isinstance(deadline_ms, int)
            or isinstance(deadline_ms, bool)
            or deadline_ms <= 0
        ):
            raise ConfigError(
                f"{label}: 'deadline_ms' must be a positive integer for kind: pipeline"
            )
    else:
        catch_up_minutes = merged["catch_up_minutes"]

        deadline_ms = merged["deadline_ms"]
        if deadline_ms is not None and (
            not isinstance(deadline_ms, int)
            or isinstance(deadline_ms, bool)
            or deadline_ms <= 0
        ):
            raise ConfigError(
                f"{label}: 'deadline_ms' must be a positive integer or null"
            )

        prompt_file = merged["prompt_file"]
        if prompt_file is not None and (
            not isinstance(prompt_file, str) or not prompt_file
        ):
            raise ConfigError(
                f"{label}: 'prompt_file' must be a non-empty string or null"
            )

    # -- target inference (after kind is resolved) ------------------------------------
    target_raw = merged.get("target")
    target: str | None = None
    if target_raw is not None:
        if not isinstance(target_raw, str) or target_raw not in VALID_TARGETS:
            raise ConfigError(f"{label}: 'target' must be 'pr' or 'base' or null")
        target = target_raw
        if inferred_target is not None and target != inferred_target:
            raise ConfigError(
                f"{label}: explicit 'target: {target}' does not match inferred "
                f"'target: {inferred_target}' from checks (not yet supported)"
            )

    if kind == "gated" and target is None:
        target = inferred_target

    # An audit always targets base: the fixes it dispatches are ordinary base-target
    # gated work, and the audit itself has no PR to gate on.
    if kind == "audit":
        target = "base"

    if target == "base" and kind == "gated" and (not isinstance(base, str) or not base):
        raise ConfigError(
            f"{label}: 'base' must be a non-empty string when target is 'base'"
        )

    # -- tmp_hygiene (issue 027) ---------------------------------------------------
    tmp_hygiene_raw = merged.get("tmp_hygiene")
    tmp_hygiene: TmphgieneConfig | None = None
    if tmp_hygiene_raw is not None:
        if not isinstance(tmp_hygiene_raw, dict):
            raise ConfigError(f"{label}: 'tmp_hygiene' must be a mapping or null")
        unknown_th = set(tmp_hygiene_raw) - {"enabled", "max_age_s", "tmp_dir"}
        if unknown_th:
            raise ConfigError(
                f"{label}: 'tmp_hygiene' has unknown key(s): {sorted(unknown_th)}"
            )
        th_enabled = tmp_hygiene_raw.get("enabled", True)
        if not isinstance(th_enabled, bool):
            raise ConfigError(f"{label}: 'tmp_hygiene.enabled' must be a boolean")
        th_max_age_s = tmp_hygiene_raw.get("max_age_s", DEFAULT_MAX_AGE_S)
        if (
            not isinstance(th_max_age_s, int)
            or isinstance(th_max_age_s, bool)
            or th_max_age_s <= 0
        ):
            raise ConfigError(
                f"{label}: 'tmp_hygiene.max_age_s' must be a positive integer"
            )
        th_tmp_dir = tmp_hygiene_raw.get("tmp_dir", DEFAULT_TMP_DIR)
        if not isinstance(th_tmp_dir, str) or not th_tmp_dir:
            raise ConfigError(
                f"{label}: 'tmp_hygiene.tmp_dir' must be a non-empty string"
            )
        tmp_hygiene = TmphgieneConfig(
            enabled=th_enabled, max_age_s=th_max_age_s, tmp_dir=th_tmp_dir
        )

    # -- retry_attempts / retry_on (issue 008) ----------------------------------
    #
    # Per-job only — NOT inheritable via defaults.yaml (same precedent as kind/checks
    # in issue 049). retry_attempts is bounded 0..3 to prevent tick-hogging.

    retry_attempts = merged["retry_attempts"]
    if not isinstance(retry_attempts, int) or isinstance(retry_attempts, bool):
        raise ConfigError(f"{label}: 'retry_attempts' must be an integer")
    if retry_attempts < 0 or retry_attempts > 3:
        raise ConfigError(
            f"{label}: 'retry_attempts' must be between 0 and 3 (inclusive)"
        )

    retry_on_raw = merged.get("retry_on")
    retry_on: tuple[str, ...] | None = None
    if retry_on_raw is not None:
        if not isinstance(retry_on_raw, list):
            raise ConfigError(f"{label}: 'retry_on' must be null or a list of strings")
        for ri, reason in enumerate(retry_on_raw):
            if not isinstance(reason, str) or not reason:
                raise ConfigError(
                    f"{label}: 'retry_on[{ri}]' must be a non-empty string"
                )
            if reason not in VALID_RETRY_REASONS:
                raise ConfigError(
                    f"{label}: 'retry_on' contains unknown reason {reason!r} — "
                    f"valid reasons: {sorted(VALID_RETRY_REASONS)}"
                )
        retry_on = tuple(retry_on_raw) if retry_on_raw else None

    # retry_attempts > 0 with empty/null retry_on is a hard error
    if retry_attempts > 0 and not retry_on:
        raise ConfigError(
            f"{label}: 'retry_attempts' requires non-empty 'retry_on' — "
            "declare which reasons are retry-eligible"
        )
    # retry_on non-empty with retry_attempts == 0 is allowed but inert
    if retry_on and retry_attempts == 0:
        warnings.warn(
            f"{label}: 'retry_on' is set but 'retry_attempts' is 0 — "
            "retry_on is inert (no retries will occur)",
            stacklevel=2,
        )
    # workspace: root with retries is a soft warning
    if retry_attempts > 0 and workspace == "root":
        warnings.warn(
            f"{label}: 'retry_attempts' > 0 on workspace: root — "
            "retries are only safe for idempotent jobs",
            stacklevel=2,
        )

    return Job(
        name=name,
        enabled=enabled,
        cron=cron,
        repo=repo,
        workspace=workspace,
        base=base,
        agent_kind=agent_kind,
        model=model,
        fallback_model=fallback_model,
        prompt=prompt,
        timeout_ms=merged["timeout_ms"],
        start_timeout_ms=merged["start_timeout_ms"],
        catch_up_minutes=catch_up_minutes,
        timezone=timezone,
        on_missed=on_missed,
        notify_policy=notify_policy,
        repository=repository,
        failure_markers=failure_markers,
        checks=checks,
        target=target,
        max_workers_per_tick=merged["max_workers_per_tick"],
        max_attempts_per_target=merged["max_attempts_per_target"],
        kind=kind,
        prompt_file=prompt_file,
        deadline_ms=deadline_ms,
        tmp_hygiene=tmp_hygiene,
        retry_attempts=retry_attempts,
        retry_on=retry_on,
        audit=audit,
        max_findings_per_dispatch=merged["max_findings_per_dispatch"],
        ledger_retention_days=merged["ledger_retention_days"],
        adopt_baseline=merged["adopt_baseline"],
        fix_prompt=fix_prompt,
    )
