"""Host drift check: does this host's deployment still match the runner checkout?

Three things went wrong on the production Pi, all found on 2026-10-23, and none of
them made a sound:

- `herdr-routines-digest.timer` was never installed, although deploy/README.md says
  to enable it. Nothing noticed for weeks.
- self-update deferred three nights running on a held tick lock (fixed in #219), and
  the runner sat 9 commits behind origin/main with exit 0 and no alert.
- After #219 merged and the runner updated, `~/.config/systemd/user/` still held the
  *old* `herdr-routines-update.{service,timer}`. The host installs units by copying
  `deploy/systemd/*` by hand, and self-update only *reports* `deploy/` changes, once,
  in that night's notification (`self_update.deploy_changes`) — so a merged unit fix
  silently does not take effect.

`check_host` is the read-only answer to "has any of that happened here". It never
installs, copies, enables or reloads anything — the same report-only posture as
self-update's deploy diff, for the same reason: host config has its own install
steps (deploy/README.md), and applying them from a timer could take the scheduler
down. It never fetches either, so it stays fast and safe offline; `runner_behind`
reads `origin/<base>` as self-update's nightly fetch last left it, and says how old
that fetch is.

Every external call (`git`, `systemctl --user`) goes through the injectable `runner=`
seam, bounded by `PROBE_TIMEOUT_S`, and a probe that fails or times out becomes its
own `probe_failed` finding instead of an exception: the check runs at the tail of
self-update and inside the digest, and must never take either of them down.

Surfaced three ways: `herdr-routines host-check` (exit 1 on drift, for the runbook),
the self-update notification (the path that still runs when the digest timer is the
thing missing), and a section in the daily digest.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

# Same escape hatch as self_update.Runner: subprocess.run is overloaded.
Runner = Callable[..., Any]

DriftKind = Literal["missing_unit", "stale_unit", "runner_behind", "probe_failed"]

# Each probe is a local `git` or `systemctl --user` read; seconds is generous, and a
# hung user bus must not stall the self-update unit or the digest behind it.
PROBE_TIMEOUT_S = 15
# self-update fetches once a night, so a fetch older than two nights means at least
# one night's update did not get as far as `git fetch` — the tick-lock deferrals of
# 2026-10-21..23 returned before it — and the behind count read off origin/<base> is
# understated by however much main moved since.
STALE_FETCH_S = 48 * 3600


@dataclass(frozen=True, slots=True)
class DriftFinding:
    kind: DriftKind
    message: str


def default_unit_dir() -> Path:
    """Where the host's user units are installed: deploy/README.md copies them here."""
    return Path.home() / ".config" / "systemd" / "user"


def format_findings(findings: Sequence[DriftFinding]) -> list[str]:
    """One `- kind: message` line per finding — the shape all three surfaces print."""
    return [f"- {f.kind}: {f.message}" for f in findings]


def _probe(runner: Runner, argv: list[str], cwd: Path | None = None) -> str | None:
    """stdout of one bounded probe, or None when it failed, timed out or could not
    start (a missing `systemctl` is a FileNotFoundError, not a non-zero exit)."""
    try:
        proc = runner(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return str(proc.stdout or "")


def _age(seconds: float) -> str:
    hours = int(seconds // 3600)
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


# ---------------------------------------------------------------------------
# units
# ---------------------------------------------------------------------------


def _must_be_enabled(unit_text: str) -> bool:
    """A unit is enable-able exactly when it has an `[Install]` section. In
    deploy/systemd/ that is every `.timer` and `herdr-server.service`; the timer-driven
    `.service`s have none, because it is their timer that must be enabled."""
    return any(line.strip() == "[Install]" for line in unit_text.splitlines())


def _unit_findings(
    unit_src: Path, unit_dir: Path, runner: Runner
) -> list[DriftFinding]:
    name = unit_src.name
    installed = unit_dir / name
    if not installed.is_file():
        return [
            DriftFinding(
                "missing_unit", f"{name} is in deploy/systemd/ but not in {unit_dir}"
            )
        ]

    try:
        repo_bytes = unit_src.read_bytes()
        installed_bytes = installed.read_bytes()
    except OSError as e:
        return [DriftFinding("probe_failed", f"cannot read {name}: {e}")]

    findings: list[DriftFinding] = []
    # Byte compare: the host copies units verbatim and systemd expands `%h`/`%u` at
    # load time. If install ever starts templating (sed-ing paths in), this has to
    # compare against the rendered template instead or it will flag every unit.
    if installed_bytes != repo_bytes:
        findings.append(
            DriftFinding(
                "stale_unit",
                f"{installed} differs from deploy/systemd/{name} — copy it and "
                "`systemctl --user daemon-reload`",
            )
        )

    if _must_be_enabled(repo_bytes.decode(errors="replace")):
        out = _probe(
            runner,
            [
                "systemctl",
                "--user",
                "show",
                "--property=UnitFileState,ActiveState",
                name,
            ],
        )
        if out is None:
            findings.append(
                DriftFinding("probe_failed", f"systemctl --user show {name} failed")
            )
            return findings
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        enabled = props.get("UnitFileState", "")
        active = props.get("ActiveState", "")
        if enabled != "enabled" or active != "active":
            # An enabled unit that is not active has stopped or crashed (a failed
            # herdr-server.service): enabling it again is not the fix.
            remedy = (
                f"systemctl --user status {name}"
                if enabled == "enabled"
                else f"systemctl --user enable --now {name}"
            )
            findings.append(
                DriftFinding(
                    "missing_unit",
                    f"{name} is installed but {enabled or 'unknown'}/"
                    f"{active or 'unknown'} — `{remedy}`",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# runner checkout
# ---------------------------------------------------------------------------


def _runner_findings(
    checkout: Path, base: str, runner: Runner, now: datetime
) -> list[DriftFinding]:
    findings: list[DriftFinding] = []
    ref = f"origin/{base}"

    # The age of the last fetch, from FETCH_HEAD's mtime. `--git-path` rather than
    # `.git/FETCH_HEAD` so a worktree or a `.git` file still resolves; it comes back
    # relative to the checkout.
    git_path = _probe(
        runner, ["git", "-C", str(checkout), "rev-parse", "--git-path", "FETCH_HEAD"]
    )
    if git_path is None:
        return [DriftFinding("probe_failed", f"git rev-parse failed in {checkout}")]
    fetch_head = Path(git_path.strip())
    if not fetch_head.is_absolute():
        fetch_head = checkout / fetch_head
    try:
        fetched_at = fetch_head.stat().st_mtime
    except OSError:
        fetched_at = None
    if fetched_at is None:
        findings.append(
            DriftFinding(
                "runner_behind",
                f"{checkout} has never fetched — {ref} is unknown and self-update "
                "has not run here",
            )
        )
    elif now.timestamp() - fetched_at > STALE_FETCH_S:
        findings.append(
            DriftFinding(
                "runner_behind",
                f"{checkout} last fetched {_age(now.timestamp() - fetched_at)} ago — "
                f"self-update has not got as far as its fetch since, so {ref} (and "
                "any behind count below) is that old",
            )
        )

    count = _probe(
        runner, ["git", "-C", str(checkout), "rev-list", "--count", f"HEAD..{ref}"]
    )
    if count is None:
        findings.append(
            DriftFinding(
                "probe_failed", f"git rev-list HEAD..{ref} failed in {checkout}"
            )
        )
        return findings
    if not count.strip().isdigit():
        findings.append(
            DriftFinding("probe_failed", f"git rev-list printed {count.strip()!r}")
        )
        return findings
    behind = int(count.strip())
    if behind == 0:
        return findings

    stamps = _probe(
        runner, ["git", "-C", str(checkout), "log", "--format=%ct", f"HEAD..{ref}"]
    )
    times = [int(s) for s in (stamps or "").split() if s.isdigit()]
    oldest = (
        f"; oldest missing commit is {_age(now.timestamp() - min(times))} old"
        if times
        else ""
    )
    findings.append(
        DriftFinding(
            "runner_behind",
            f"{checkout} is {behind} commit(s) behind {ref}{oldest} "
            "(as of the last fetch; this check never fetches)",
        )
    )
    return findings


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------


def check_host(
    checkout: Path,
    *,
    base: str = "main",
    unit_dir: Path | None = None,
    runner: Runner = subprocess.run,
    now: datetime | None = None,
) -> list[DriftFinding]:
    """Every drift finding for the runner *checkout* on this host; empty when clean.

    Units come from the checkout's own `deploy/systemd/`, so after self-update moves
    the runner forward, a unit fix that merged with it shows up here as `stale_unit`
    until someone copies it — which is incident 3 above."""
    units_src = checkout / "deploy" / "systemd"
    target = unit_dir or default_unit_dir()
    findings: list[DriftFinding] = []
    units = sorted(p for p in units_src.glob("*") if p.suffix in (".service", ".timer"))
    if not units:
        findings.append(DriftFinding("probe_failed", f"no units found in {units_src}"))
    for unit in units:
        findings += _unit_findings(unit, target, runner)
    findings += _runner_findings(checkout, base, runner, now or datetime.now(UTC))
    return findings
