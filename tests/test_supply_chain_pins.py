"""Guards for the supply-chain pins (audit-supply): third-party code that runs in CI or on
the host must be pinned to an immutable reference, and installs must fail on a stale lock
rather than silently re-resolving. Text checks over the committed files, so a regression
(a bare tag, a dropped `--locked`) fails here instead of surfacing in a later audit."""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))

# `uses: owner/repo@<40 hex> # v1.2.3` — the trailing tag comment is what Dependabot's
# github-actions ecosystem reads to keep bumping the SHA.
PINNED_USES = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(\.\d+)*$")


def _uses_refs(text: str) -> list[str]:
    return re.findall(r"^\s*(?:-\s+)?uses:\s*(.+?)\s*$", text, flags=re.MULTILINE)


def test_workflows_exist() -> None:
    assert WORKFLOWS, "no workflows found; the guards below would pass vacuously"


def test_every_action_is_pinned_to_a_commit_sha() -> None:
    unpinned = [
        f"{wf.name}: {ref}"
        for wf in WORKFLOWS
        for ref in _uses_refs(wf.read_text(encoding="utf-8"))
        if not PINNED_USES.match(ref)
    ]
    assert unpinned == []


def test_checkouts_do_not_persist_credentials() -> None:
    """None of the CI jobs pushes, so the token has no reason to stay in `.git/config`."""
    for wf in WORKFLOWS:
        text = wf.read_text(encoding="utf-8")
        checkouts = text.count("actions/checkout@")
        assert text.count("persist-credentials: false") == checkouts, wf.name


def test_logger_dependency_is_pinned_to_a_commit() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'"logger @ git\+\S+\.git@[0-9a-f]{40}"', pyproject)


def test_ci_installs_from_a_locked_lock() -> None:
    for wf in WORKFLOWS:
        for line in wf.read_text(encoding="utf-8").splitlines():
            if "uv sync" in line and not line.lstrip().startswith("#"):
                assert "--locked" in line, f"{wf.name}: {line.strip()}"


def test_systemd_units_run_uv_locked() -> None:
    execs = [
        (unit.name, line)
        for unit in sorted((REPO_ROOT / "deploy" / "systemd").glob("*.service"))
        for line in unit.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStart=") and "uv run" in line
    ]
    assert execs, "no `uv run` ExecStart found; the guard would pass vacuously"
    assert [e for e in execs if "uv run --locked " not in e[1]] == []


def test_dependabot_has_cooldown_and_tracks_uv_lock() -> None:
    config = yaml.safe_load(
        (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    )
    updates = config["updates"]
    assert all("cooldown" in u for u in updates)
    ecosystems = {u["package-ecosystem"] for u in updates}
    # `pip` bumps edit only pyproject.toml, which `uv sync --locked` then rejects.
    assert "uv" in ecosystems
    assert "pip" not in ecosystems
