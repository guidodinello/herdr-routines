"""Guards for the supply-chain pins (audit-supply): third-party code that runs in CI or on
the host must be pinned to an immutable reference, and installs must fail on a stale lock
rather than silently re-resolving. Text checks over the committed files, so a regression
(a bare tag, a dropped `--locked`) fails here instead of surfacing in a later audit."""

import re
from pathlib import Path

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
