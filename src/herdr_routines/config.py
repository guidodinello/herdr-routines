"""Config loading and validation: YAML -> Job dataclasses.

Pure: no filesystem access beyond reading YAML files, no subprocess, no clock reads
(the caller supplies `now` where it matters). This is what makes it fully unit-testable.

Supports two config layouts:
- Legacy single file: ``jobs.yaml`` (deprecated, emits a warning when used).
- Directory layout: ``jobs.d/`` with one ``<name>.yaml`` per job and an optional
  ``defaults.yaml`` for shared fields.  The loader picks directory over file when
  both exist.
"""

from __future__ import annotations

import os
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from logger import get_logger

import yaml
from croniter import croniter

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
# empirically against herdr 0.8.2 (see docs/plan-v1.md): only these two kinds have a pinned-down
# flag — a job's 'model' is rejected for any other agent_kind rather than guessing. Also consumed
# by herdr.py's `build_agent_start_args`, which is where the flag is actually applied.
AGENT_MODEL_FLAGS: dict[str, str] = {
    "claude": "--model",
    "opencode": "-m",
}

VALID_WORKSPACE_MODES = frozenset({"worktree", "root"})
VALID_ON_MISSED = frozenset({"log", "notify"})

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
    }
)

_JOB_REQUIRED_KEYS = frozenset({"name", "cron", "repo"})
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
    "prompt": "",
    "timeout_ms": 1_800_000,
    "start_timeout_ms": 120_000,
    "catch_up_minutes": 120,
    "timezone": "UTC",
    "on_missed": "log",
    "failure_markers": None,
    "checks": None,
    "target": None,
    "max_workers_per_tick": 3,
    "max_attempts_per_target": 3,
}


class ConfigError(ValueError):
    """Raised for any problem with jobs.yaml — unknown keys, bad cron, duplicate names, etc."""


@dataclass(frozen=True, slots=True)
class GateCheck:
    """A single check in the unified gate model."""

    kind: str  # "pr_health" | "command"
    command: str | None = None
    timeout_ms: int = 120_000


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
    # Screen markers scanned after a failed prompt wait (docs/failure-reaping.md §3.2).
    # None = runner.DEFAULT_FAILURE_MARKERS.
    failure_markers: tuple[str, ...] | None = None
    # Unified gate model: ordered checks that gate the fix agent (None = plain job).
    checks: tuple[GateCheck, ...] | None = None
    # Inferred from check kinds or explicit override: "pr" | "base".
    target: str | None = None
    # Dispatch cap per tick.
    max_workers_per_tick: int = 3
    # Retry budget keyed per target (per gate branch for base, per PR number for pr).
    max_attempts_per_target: int = 3

    @property
    def agent_name(self) -> str:
        return f"rt-{self.name}"


@dataclass(frozen=True, slots=True)
class RoutinesConfig:
    jobs: tuple[Job, ...] = field(default_factory=tuple)
    # Per-file errors from directory loader (empty for single-file or clean directory load).
    errors: tuple[str, ...] = field(default_factory=tuple)

    def job(self, name: str) -> Job | None:
        for j in self.jobs:
            if j.name == name:
                return j
        return None


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

    unknown_top = set(raw) - {"version", "defaults", "jobs"}
    if unknown_top:
        raise ConfigError(f"unknown top-level key(s): {sorted(unknown_top)}")

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

    return RoutinesConfig(jobs=tuple(jobs))


def load_config_dir(path: Path) -> RoutinesConfig:
    """Load config from a ``jobs.d/`` directory layout.

    Directory contract::

        <path>/
            defaults.yaml   # optional; shared fields merged under each job
            <name>.yaml     # one per job; filename stem is the canonical name

    Discovery is deterministic (``sorted()`` glob).  ``defaults.yaml`` is excluded
    from the job file list.

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
        raw_defaults = _load_yaml_or_error(defaults_path, is_defaults=True)
        _validate_defaults_keys(raw_defaults, defaults_path)

    # --- discover job files (sorted, exclude defaults.yaml) ----------------------
    job_files = sorted(p for p in path.glob("*.yaml") if p.name != "defaults.yaml")

    jobs: list[Job] = []
    seen_names: set[str] = set()
    errors: list[str] = []

    for job_file in job_files:
        try:
            raw_job = _load_yaml_or_error(job_file, is_defaults=False)
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
            job = _build_job(raw_job, raw_defaults, index=len(jobs), label_prefix=job_file.name)
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

    return RoutinesConfig(jobs=tuple(jobs), errors=tuple(errors))


def _load_yaml_or_error(path: Path, *, is_defaults: bool) -> dict:
    """Read and parse a single YAML file.  Raises :class:`ConfigError` with the file
    name embedded for clear diagnostics."""
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
        kind = "defaults" if is_defaults else "job"
        raise ConfigError(f"{path}: {kind} file must be a mapping, got {type(raw).__name__}")
    return raw


def _validate_defaults_keys(raw: dict, path: Path) -> None:
    """Reject unknown keys in ``defaults.yaml`` (same contract as the legacy top-level
    ``defaults:`` block)."""
    unknown = set(raw) - _DEFAULTS_ALLOWED_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown key(s): {sorted(unknown)}")


def _build_job(raw_job: dict, defaults: dict, *, index: int, label_prefix: str | None = None) -> Job:
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
        label = f"jobs[{index}] ({name!r})" if isinstance(name, str) else f"jobs[{index}]"

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

    if checks is not None and target is None:
        target = inferred_target

    if (
        target == "base"
        and checks is not None
        and (not isinstance(base, str) or not base)
    ):
        raise ConfigError(
            f"{label}: 'base' must be a non-empty string when target is 'base'"
        )

    for int_key in ("max_workers_per_tick", "max_attempts_per_target"):
        value = merged[int_key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"{label}: '{int_key}' must be a non-negative integer")

    return Job(
        name=name,
        enabled=enabled,
        cron=cron,
        repo=repo,
        workspace=workspace,
        base=base,
        agent_kind=agent_kind,
        model=model,
        prompt=prompt,
        timeout_ms=merged["timeout_ms"],
        start_timeout_ms=merged["start_timeout_ms"],
        catch_up_minutes=merged["catch_up_minutes"],
        timezone=timezone,
        on_missed=on_missed,
        failure_markers=failure_markers,
        checks=checks,
        target=target,
        max_workers_per_tick=merged["max_workers_per_tick"],
        max_attempts_per_target=merged["max_attempts_per_target"],
    )
