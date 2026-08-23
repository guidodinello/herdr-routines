"""Config loading and validation: YAML -> Job dataclasses.

Pure: no filesystem access beyond reading the one YAML file, no subprocess, no clock reads
(the caller supplies `now` where it matters). This is what makes it fully unit-testable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from croniter import croniter

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
    | frozenset({"enabled", "base", "model", "prompt"})
)

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
}


class ConfigError(ValueError):
    """Raised for any problem with jobs.yaml — unknown keys, bad cron, duplicate names, etc."""


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

    @property
    def agent_name(self) -> str:
        return f"rt-{self.name}"


@dataclass(frozen=True, slots=True)
class RoutinesConfig:
    jobs: tuple[Job, ...] = field(default_factory=tuple)

    def job(self, name: str) -> Job | None:
        for j in self.jobs:
            if j.name == name:
                return j
        return None


def default_config_path() -> Path:
    """--config > $HERDR_PLUGIN_CONFIG_DIR/jobs.yaml > ~/.config/herdr-routines/jobs.yaml.

    The middle entry is forethought for the optional plugin manifest described in
    docs/plan-v1.md §8.4 — it costs nothing now and keeps that door open later.
    """
    plugin_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if plugin_dir:
        return Path(plugin_dir) / "jobs.yaml"
    return Path.home() / ".config" / "herdr-routines" / "jobs.yaml"


def load_config(path: Path) -> RoutinesConfig:
    """Load and fully validate jobs.yaml. Raises ConfigError on any problem."""
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


def _build_job(raw_job: dict, defaults: dict, *, index: int) -> Job:
    unknown = set(raw_job) - _JOB_ALLOWED_KEYS
    if unknown:
        raise ConfigError(f"jobs[{index}]: unknown key(s): {sorted(unknown)}")

    missing = _JOB_REQUIRED_KEYS - set(raw_job)
    if missing:
        raise ConfigError(f"jobs[{index}]: missing required key(s): {sorted(missing)}")

    merged = {**_JOB_DEFAULTS, **defaults, **raw_job}
    name = merged["name"]
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
    )
