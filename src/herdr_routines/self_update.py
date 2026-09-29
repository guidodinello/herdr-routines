"""Keep the runner checkout current (issue 053).

The Pi runs herdr-routines from two checkouts and only one of them maintained itself:
`~/.local/state/herdr-routines/repos/herdr-routines` (the feature-pipeline job's
`repo:`, fast-forwarded by `ensure_repo` on every run) and
`~/projects/herdr-routines` — the **runner**, the one `herdr-routines.service`,
`-watchdog.service` and `-digest.service` actually execute. The runner was updated
by hand, on the runbook's two guards (green CI, config migrations), for 17 days
straight; the 2026-09-27 incident was a merged fix (#125) that never took effect on
the host it was written for because the tick that should have used it was still on
2026-09-10 code.

`self-update` is the same sequence as a command, driven by its own daily timer
(`herdr-routines-update.timer`) before the night's runs. Three properties shape every
decision in here, and all three come from that incident:

- **Never report an update that did not happen.** `git merge --ff-only` is a no-op
  whenever HEAD already contains `origin/<base>` — *including* when HEAD is strictly
  ahead of it, which is the state a hand-hotfixed host is left in. So the post-ff
  HEAD re-read is authoritative and is the only thing that decides "updated" vs
  "up to date" (spec finding F3).
- **Never re-attempt a commit already rejected.** A bad commit would otherwise be
  re-fetched, re-validated, re-rolled-back and re-notified every night forever, so
  the operator cannot tell a broken timer from a broken commit — and mutes it. The
  rejected SHA is persisted to `$HERDR_PLUGIN_STATE_DIR/self-update.json` and
  deferred on, which clears itself for free when main moves (spec finding F2).
- **Fail closed, exit 0.** Anything unverifiable — a tick lock held, an open
  pipeline run, a red or pending or unqueryable CI state, a missing `uv` — defers
  quietly and leaves the host exactly as it was.

The command runs from the *already imported* old code and exercises the new code out
of process, so a commit that does not even import is data to roll back rather than a
crash. Everything here goes through an injectable seam (`runner=`, `gh`, `notify`,
`uv_resolver`), so the whole flow is testable without a host; git itself is not one
of those seams, and is exercised for real in `tests/test_self_update.py`.

Sequence (spec 20260929T050000Z, `### Sequence`):

    1. tick_lock                       not acquired -> deferred,  rc 0
    2. pipeline_run_open               open run     -> deferred,  rc 0
    3. repo_state                      dirty / not <base> / detached -> refused, rc 1
    4. resolve_uv                      missing      -> refused,  rc 1   (before any git op)
    5. fetch + CI gate on the new SHA  not green    -> deferred,  rc 0
                                         already rejected -> deferred, rc 0
    6. baseline validate (best effort) failure -> no baseline, count check skipped
    7. _fetch_and_fast_forward         diverged     -> refused,  rc 1
    8. re-read HEAD                    unchanged    -> up_to_date (+ warn if unpushed)
    9. post validate                   failed / fewer jobs -> reset --keep, rc 1
   10. deploy changes are reported, never applied
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol

from logger import get_logger

from herdr_routines.auto_fix import GhClient
from herdr_routines.config import RoutinesConfig
from herdr_routines.gates import (
    GateVerdict,
    evaluate_commit_checks,
    remote_owner_and_repo,
)
from herdr_routines.repos import _fetch_and_fast_forward
from herdr_routines.tick import _open_pipeline_run, tick_lock

log = get_logger(__name__)

# Every git call in this module is bounded the same way repos._fetch_and_fast_forward
# bounds its own; a degraded network link is exactly the 2026-09-27 incident's
# "5 GHz link degraded", and an unbounded git is how a host wedges overnight.
GIT_TIMEOUT_S = 120
# The unit's TimeoutStartSec is sized so systemd cannot land a SIGTERM inside the
# git merge window: two `uv run` invocations at up to VALIDATE_TIMEOUT_S each, plus
# up to five bounded git calls, plus one `gh` call, plus slack.
VALIDATE_TIMEOUT_S = 300


class Notify(Protocol):
    """The notification seam. A Protocol rather than a bare `Callable[[str, str | None],
    None]` so `body` keeps its name: the real implementation (cli._self_update_notify)
    and every test double declare `body`, and a positional-only Callable would reject
    them. A failed notification must never fail the update, so implementations log and
    swallow HerdrCliError rather than raising."""

    def __call__(self, title: str, body: str | None = None) -> None: ...


# Not typed as Callable[..., CompletedProcess[str]]: subprocess.run is overloaded and
# some of its overloads return CompletedProcess[bytes], which a narrower annotation
# would reject. gates' `runner=` seam has the same escape hatch.
Runner = Callable[..., Any]

SelfUpdateStatus = Literal[
    "up_to_date", "updated", "deferred", "refused", "rolled_back"
]

# `validate`'s only pass line (cli._cmd_validate, on stdout — warnings go to stderr
# and do not change the exit code, so a run full of pre-existing $ROUTINE_REPORT
# warnings still parses). There is no flag that emits job *names*, so the count is
# the only signal available for the "silently dropped job" hazard (spec finding F4).
_JOB_COUNT_RE = re.compile(r"^ok:\s+(\d+)\s+job\(s\)\s+valid", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class SelfUpdateResult:
    """What one `self_update` run did. `status` maps to the process exit code via
    `exit_code`: `up_to_date` / `updated` / `deferred` are all 0 — a deferral is a
    correct outcome, not an error — while `refused` and `rolled_back` are 1."""

    status: SelfUpdateStatus
    old: str | None = None
    new: str | None = None
    reason: str | None = None
    deploy_changes: tuple[str, ...] = ()
    validate_output: str | None = None

    @property
    def exit_code(self) -> int:
        return 0 if self.status in ("up_to_date", "updated", "deferred") else 1


class RepoState(NamedTuple):
    old: str
    branch: str
    dirty: bool


class ValidateOutcome(NamedTuple):
    ok: bool
    job_count: int | None
    output: str


def default_state_path() -> Path:
    """$HERDR_PLUGIN_STATE_DIR/self-update.json, else
    ~/.local/state/herdr-routines/self-update.json — the same convention as
    `tick.default_lock_path` and `history.default_history_path`."""
    plugin_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    base = (
        Path(plugin_dir)
        if plugin_dir
        else Path.home() / ".local" / "state" / "herdr-routines"
    )
    return base / "self-update.json"


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """One bounded `git -C <checkout> ...`. Every git call in this module goes
    through here so the flow is auditable in one place (and so a test can record
    the argv without mocking git's behaviour)."""
    return subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
        check=False,
    )


def repo_state(checkout: Path) -> RepoState:
    """(old_sha, branch, dirty) for the checkout. A detached HEAD is reported by
    `rev-parse --abbrev-ref` as the literal string `HEAD`, which is why the
    detached case is a comparison against the branch name rather than a special
    check with a nicer name — `_fetch_and_fast_forward` short-circuits on detached
    HEAD and returns having merged nothing, so without a refusal this command would
    report a successful "update" of a checkout it never moved."""
    head = _git(checkout, "rev-parse", "HEAD")
    branch = _git(checkout, "rev-parse", "--abbrev-ref", "HEAD")
    porcelain = _git(checkout, "status", "--porcelain")
    return RepoState(
        old=head.stdout.strip(),
        branch=branch.stdout.strip(),
        dirty=bool(porcelain.stdout.strip()),
    )


def reset_keep(checkout: Path, sha: str) -> tuple[int, str, str]:
    """`git reset --keep <sha>` — never `--hard`, never force, anywhere.

    `--keep` rewinds HEAD, index and worktree over a clean fast-forward, and
    *refuses* (rc 128) rather than discarding when a local edit would be
    overwritten. That refusal is the one reachable path where the checkout is not
    back at `old`, so the caller reports the SHA it is actually stuck on.
    """
    proc = _git(checkout, "reset", "--keep", sha)
    return proc.returncode, proc.stdout, proc.stderr


def deploy_changes(checkout: Path, old: str, new: str) -> list[str]:
    """`deploy/` paths touched by old..new. Reported, never applied: systemd units,
    `opencode.pipeline.json` and the example `jobs.d/` are host config with their own
    install steps, and applying them from a timer is the one thing here that could
    take the scheduler down."""
    proc = _git(checkout, "diff", "--name-only", f"{old}..{new}", "--", "deploy/")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _commit_count(checkout: Path, old: str, new: str) -> int:
    proc = _git(checkout, "rev-list", "--count", f"{old}..{new}")
    return _as_int(proc.stdout)


def _unpushed_count(checkout: Path, base: str) -> int:
    """Commits on HEAD that are not on origin/<base> — the signature of a host that
    was hot-fixed by hand and committed."""
    proc = _git(checkout, "rev-list", "--count", f"origin/{base}..HEAD")
    return _as_int(proc.stdout)


def _as_int(stdout: str) -> int:
    try:
        return int(stdout.strip())
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------


def resolve_uv() -> str | None:
    """Locate `uv`, or None. Resolved *before* any git operation: `uv` is not on
    PATH in non-interactive ssh (docs/process/pi-update-runbook.md), and finding that
    out after the fast-forward would mean rolling back for nothing."""
    found = shutil.which("uv")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "uv"
    return str(fallback) if fallback.exists() else None


def validate_subprocess(
    checkout: Path,
    uv: str,
    *,
    runner: Runner = subprocess.run,
    timeout_s: float = VALIDATE_TIMEOUT_S,
) -> ValidateOutcome:
    """`uv run --frozen herdr-routines validate` in *checkout*, out of process.

    Out of process is the whole point: the parent already has the old code imported,
    so an import error in the new code is data, not a crash — and the new import
    graph, config schema and `uv.lock` are all exercised against the host's live
    `jobs.d/`. `--frozen` still syncs `.venv` from the committed lock (so the new
    lock really is exercised) while making it impossible for `uv` to rewrite a
    *tracked* `uv.lock` between the fast-forward and a rollback.

    A `TimeoutExpired` is a failure, never a pass: an unbounded child is exactly the
    wedge `TimeoutStartSec` exists to prevent.
    """
    argv = [str(uv), "run", "--frozen", "herdr-routines", "validate"]
    try:
        proc = runner(
            argv,
            cwd=checkout,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ValidateOutcome(
            ok=False,
            job_count=None,
            output=f"validate timed out after {timeout_s:.0f}s",
        )
    output = "\n".join(
        part
        for part in ((proc.stdout or "").strip(), (proc.stderr or "").strip())
        if part
    )
    if proc.returncode != 0:
        return ValidateOutcome(ok=False, job_count=None, output=output)
    match = _JOB_COUNT_RE.search(proc.stdout or "")
    if match is None:
        return ValidateOutcome(ok=False, job_count=None, output=output or "(no output)")
    return ValidateOutcome(ok=True, job_count=int(match.group(1)), output=output)


def pipeline_run_open(config: RoutinesConfig, history_path: Path) -> str | None:
    """Name of a `kind: pipeline` job with a run in flight, else None.

    Deliberately `tick._open_pipeline_run` and not `is_currently_running`: the
    question here is "is there a run in flight", with no staleness clock folded in,
    and those are two separate predicates by design. This guard is not redundant
    with the tick lock — a pipeline run's real work happens in a detached
    `systemd-run --user` unit that holds no lock for its (hours-long) life, so the
    lock protects the launch/reconcile boundary and this protects the run itself.
    """
    for job in config.jobs:
        if job.kind != "pipeline":
            continue
        if _open_pipeline_run(history_path, job.name) is not None:
            return job.name
    return None


def commit_ci_state(gh: GhClient, *, owner: str, repo: str, sha: str) -> GateVerdict:
    """The green-CI gate on the commit we are about to deploy — the runbook's step 0,
    automated. Any `gh` failure is "unverifiable", which is a deferral, never a
    pass."""
    try:
        checks = gh.commit_check_runs(owner=owner, repo=repo, sha=sha)
    except Exception as e:  # noqa: BLE001 - any gh failure means "cannot confirm green"
        return GateVerdict(passed=False, reason=f"check runs unqueryable: {e}")
    return evaluate_commit_checks([c for c in checks if isinstance(c, dict)])


# ---------------------------------------------------------------------------
# the rejected-SHA state file
# ---------------------------------------------------------------------------


def read_rejected(state_path: Path) -> tuple[str | None, str | None]:
    """(last_rejected_sha, reason), or (None, None) when there is no usable state."""
    try:
        payload = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    sha = payload.get("last_rejected_sha")
    reason = payload.get("reason")
    return (
        sha if isinstance(sha, str) else None,
        reason if isinstance(reason, str) else None,
    )


def record_rejected(
    state_path: Path, sha: str, reason: str, *, now: datetime | None = None
) -> None:
    """Remember a commit we rolled back, so the next run defers on it instead of
    re-testing it. Keyed on the SHA, so a later move of main clears it for free."""
    stamp = now or datetime.now(UTC)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "last_rejected_sha": sha,
                "reason": reason,
                "ts": stamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            },
            indent=2,
        )
        + "\n"
    )


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


def _deferred(reason: str, checkout: Path) -> SelfUpdateResult:
    log.warning("self-update: deferred (%s) on %s", reason, checkout)
    return SelfUpdateResult(status="deferred", reason=reason)


def _refused(
    reason: str, checkout: Path, *, notify: Notify, old: str | None = None
) -> SelfUpdateResult:
    """Refusals are loud: a dirty / hand-hotfixed / diverged checkout is a host a
    human has to look at, and a host that silently stops updating is the exact
    failure this command exists to prevent (R3)."""
    log.error("self-update: refused (%s) on %s", reason, checkout)
    notify(
        f"herdr-routines: self-update refused ({checkout})",
        body=f"{reason}\ncheckout: {checkout}",
    )
    return SelfUpdateResult(status="refused", old=old, reason=reason)


def _rolled_back(
    checkout: Path,
    *,
    notify: Notify,
    old: str,
    new: str,
    reason: str,
    validate_output: str,
    state_path: Path,
) -> SelfUpdateResult:
    """Undo the fast-forward, remember the rejected SHA, and report the SHA the
    checkout is *actually* on — which is not `old` when `reset --keep` refused."""
    rc, _out, stderr = reset_keep(checkout, old)
    record_rejected(state_path, new, reason)
    head_now = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    porcelain = _git(checkout, "status", "--porcelain").stdout.strip()
    stuck = head_now != old
    body = [
        reason,
        f"checkout: {checkout}",
        f"rolled back to: {old}",
        f"HEAD is now: {head_now or '(unknown)'}",
    ]
    if stuck:
        body.append(
            "git reset --keep refused — the checkout is stuck on a commit that did "
            "not validate; fix it by hand (no reset --hard was attempted)"
        )
    body += [
        f"git status --porcelain: {porcelain or '(clean)'}",
        f"validate output: {validate_output or '(none)'}",
    ]
    if rc != 0:
        body.append(f"git reset --keep stderr: {stderr.strip() or '(empty)'}")
    log.error("self-update: rolled back %s on %s: %s", old, checkout, reason)
    notify(
        f"herdr-routines: self-update rolled back ({checkout})", body="\n".join(body)
    )
    return SelfUpdateResult(
        status="rolled_back",
        old=old,
        new=new,
        reason=reason,
        validate_output=validate_output,
    )


def _validate_failure_reason(post: ValidateOutcome, baseline_count: int | None) -> str:
    if not post.ok:
        first = (post.output.strip().splitlines() or [""])[0]
        return f"validate failed: {first}" if first else "validate failed"
    if baseline_count is not None and post.job_count is not None:
        return (
            f"validate reports {post.job_count} job(s), down from {baseline_count} — "
            "a config migration that silently dropped a job"
        )
    return "validate failed"


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------


def run_self_update(
    *,
    checkout: Path,
    lock_path: Path,
    history_path: Path,
    config: RoutinesConfig,
    gh: GhClient,
    notify: Notify,
    state_path: Path,
    base: str = "main",
    dry_run: bool = False,
    runner: Runner = subprocess.run,
    uv_resolver: Callable[[], str | None] = resolve_uv,
) -> SelfUpdateResult:
    """Fast-forward *checkout* to origin/<base> after proving the new commit is
    green and that the new code accepts the host's live config; roll back if it does
    not. See the module docstring for the numbered sequence and `SelfUpdateResult`
    for the exit-code mapping.

    `dry_run` performs steps 1-5 — including the fetch and the CI query, since both
    are needed for a truthful `old..new` and deploy diff — prints what it would do,
    and stops before the fast-forward: no ff, no validate, no state write, no
    notification. It reports `deferred` (exit 0) rather than inventing a sixth
    status, and a `--dry-run` under a held tick lock reports `deferred` too, which is
    correct and is said in `--help` so the manual runbook path is not confusing.
    """
    with tick_lock(lock_path) as acquired:
        if not acquired:
            return _deferred("tick lock held", checkout)

        open_job = pipeline_run_open(config, history_path)
        if open_job is not None:
            return _deferred(f"pipeline run open for {open_job}", checkout)

        state = repo_state(checkout)
        if state.dirty:
            return _refused(
                "working tree is dirty", checkout, notify=notify, old=state.old
            )
        if state.branch == "HEAD":
            return _refused("detached HEAD", checkout, notify=notify, old=state.old)
        if state.branch != base:
            return _refused(
                f"not on {base} (HEAD is on {state.branch})",
                checkout,
                notify=notify,
                old=state.old,
            )

        uv = uv_resolver()
        if uv is None:
            return _refused(
                "uv not found on PATH or at ~/.local/bin/uv",
                checkout,
                notify=notify,
                old=state.old,
            )

        fetch = _git(checkout, "fetch", "--prune", "origin")
        if fetch.returncode != 0:
            return _refused(
                f"git fetch failed: {fetch.stderr.strip() or 'unknown error'}",
                checkout,
                notify=notify,
                old=state.old,
            )
        old = state.old
        new = _git(checkout, "rev-parse", f"origin/{base}").stdout.strip()
        if not new:
            return _refused(
                f"cannot resolve origin/{base}", checkout, notify=notify, old=old
            )

        # Cheap short-circuit only: `merge --ff-only` is also a no-op when HEAD is
        # strictly ahead of origin/<base>, and step 8 is what actually decides.
        if new == old:
            log.info("self-update: %s already at %s on %s", checkout, old, base)
            return SelfUpdateResult(status="up_to_date", old=old, new=old)

        rejected_sha, rejected_reason = read_rejected(state_path)
        if new == rejected_sha:
            return _deferred(
                f"already rolled back: {new} — {rejected_reason or 'unknown reason'}",
                checkout,
            )

        try:
            owner, name = remote_owner_and_repo(checkout)
        except (RuntimeError, ValueError) as e:
            return _deferred(f"cannot resolve owner/repo: {e}", checkout)
        verdict = commit_ci_state(gh, owner=owner, repo=name, sha=new)
        if not verdict.passed:
            return _deferred(verdict.reason or "commit CI is not green", checkout)

        if dry_run:
            changes = deploy_changes(checkout, old, new)
            log.info(
                "self-update: dry run: %s..%s (%d commits) on %s; deploy changes: %s",
                old,
                new,
                _commit_count(checkout, old, new),
                checkout,
                ", ".join(changes) or "none",
            )
            return SelfUpdateResult(
                status="deferred",
                old=old,
                new=new,
                reason=f"dry run: would fast-forward {old}..{new}",
                deploy_changes=tuple(changes),
            )

        # Best effort: a host that was already broken must not stay frozen forever
        # because its *pre-update* validate failed. The exit-code check still
        # applies; only the count comparison is skipped.
        baseline = validate_subprocess(checkout, uv, runner=runner)
        baseline_count = baseline.job_count if baseline.ok else None
        if baseline_count is None:
            log.warning(
                "self-update: no pre-update validate baseline on %s; the job-count "
                "check will be skipped (exit code still enforced)",
                checkout,
            )

        try:
            _fetch_and_fast_forward(checkout, base=base)
        except RuntimeError as e:
            return _refused(
                f"fast-forward failed: {e}", checkout, notify=notify, old=old
            )

        # Authoritative: what is on disk after the fast-forward is what happened,
        # not what origin/<base> said before it (spec finding F3).
        head_after = _git(checkout, "rev-parse", "HEAD").stdout.strip()
        if head_after == old:
            unpushed = _unpushed_count(checkout, base)
            if unpushed:
                log.warning(
                    "self-update: up to date; %d unpushed commit(s) on %s "
                    "(hand-hotfixed host?)",
                    unpushed,
                    checkout,
                )
            log.info("self-update: %s already up to date at %s", checkout, old)
            return SelfUpdateResult(status="up_to_date", old=old, new=old)

        post = validate_subprocess(checkout, uv, runner=runner)
        job_count_drop = (
            post.ok
            and post.job_count is not None
            and baseline_count is not None
            and post.job_count < baseline_count
        )
        if not post.ok or job_count_drop:
            return _rolled_back(
                checkout,
                notify=notify,
                old=old,
                new=head_after,
                reason=_validate_failure_reason(post, baseline_count),
                validate_output=post.output,
                state_path=state_path,
            )

        changes = deploy_changes(checkout, old, head_after)
        n = _commit_count(checkout, old, head_after)
        log.info("self-update: %s..%s (%d commits) on %s", old, head_after, n, checkout)
        body = [f"{old}..{head_after} ({n} commits)", f"checkout: {checkout}"]
        if changes:
            body.append("deploy/ changes (reported, not applied):")
            body += [f"  {path}" for path in changes]
        notify(
            f"herdr-routines: self-update {old[:7]}..{head_after[:7]} ({n} commits)",
            body="\n".join(body),
        )
        return SelfUpdateResult(
            status="updated",
            old=old,
            new=head_after,
            deploy_changes=tuple(changes),
        )
