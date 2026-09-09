"""Doc-contract tests for issue 050 — the orchestrator must invoke `herdr-routines`
via `uv run`, and the opencode permission allowlist must live in one tracked file.

Same spirit as test_pipeline_pane_lifecycle.py: grep the authority docs and parse
the committed config. The bug these guard against killed the 20260909 nightly run
(a bare `herdr-routines` call is command-not-found on the pipeline hosts, and the
orchestrator then wedged probing `~/.local/bin`).
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT = REPO_ROOT / "docs" / "pipeline" / "orchestrator-prompt.md"
SETUP = REPO_ROOT / "docs" / "pipeline" / "setup.md"
PERMISSION_FILE = REPO_ROOT / "deploy" / "opencode.pipeline.json"

# Subcommands the prompt tells the orchestrator to run.
HERDR_ROUTINES_SUBCOMMANDS = ("sync-repo", "pick-feature", "gate")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_prompt_invokes_herdr_routines_via_uv_run() -> None:
    """Criterion 1: every `herdr-routines <subcommand>` in the prompt is `uv run herdr-routines <subcommand>`."""
    text = _read(PROMPT)
    for sub in HERDR_ROUTINES_SUBCOMMANDS:
        # Count command invocations of this subcommand, then confirm each one is
        # preceded by `uv run `. `(?<!uv run )` is a fixed-width lookbehind.
        bare = re.findall(rf"(?<!uv run )herdr-routines {sub}\b", text)
        assert not bare, (
            f"bare `herdr-routines {sub}` in orchestrator-prompt.md: {bare}"
        )
        assert f"uv run herdr-routines {sub}" in text, (
            f"expected `uv run herdr-routines {sub}` in orchestrator-prompt.md"
        )


def test_prompt_notes_herdr_routines_not_a_binary() -> None:
    """Criterion 2: the prompt explains why `uv run` — herdr-routines is not on PATH on pipeline hosts."""
    text = " ".join(_read(PROMPT).split())
    assert "uv run herdr-routines" in text
    assert "not installed as a standalone binary" in text
    assert "issue 050" in text


def test_opencode_pipeline_permission_file() -> None:
    """Criterion 3: deploy/opencode.pipeline.json is valid JSON and allowlists what the pipeline needs."""
    config = json.loads(_read(PERMISSION_FILE))
    allow = config["permission"]["external_directory"]
    required = {
        "/tmp/**",
        "~/.config/opencode/**",
        "~/.config/herdr/**",
        "~/.herdr/worktrees/**",
        "~/.local/state/herdr/**",
        "~/.local/state/herdr-routines/**",
        "~/.cache/uv/**",
    }
    assert required <= set(allow), f"missing allowlist entries: {required - set(allow)}"
    assert all(v == "allow" for v in allow.values())
    assert config["permission"]["tool"] == "allow"
    # ~/.local/bin is deliberately NOT allowlisted (issue 050 fix rationale).
    assert not any("/.local/bin" in k for k in allow)


def test_setup_references_tracked_opencode_file() -> None:
    """Criterion 4: setup.md points at the tracked permission file, not an inline block, and warns off ~/.local/bin."""
    text = _read(SETUP)
    assert "deploy/opencode.pipeline.json" in text
    # Inline JSON allowlist block is gone — one tracked source, not a copy that drifts.
    assert '"~/.local/state/herdr/**": "allow"' not in text
    # Operators are warned off ~/.local/bin rather than told to add it.
    assert "not** add" in text and "`~/.local/bin/**`" in text
    # The stale-launcher cleanup step is documented.
    assert "pipeline-launch-nightly.sh" in text
