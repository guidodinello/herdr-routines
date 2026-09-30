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
ISSUE_REFINEMENT_JOB = REPO_ROOT / "deploy" / "jobs.d" / "issue-refinement.yaml"

# Subcommands the prompt tells the orchestrator to run. `sync-repo` and `pick-feature`
# used to be here; issue 054 phase A moved both into the launcher's `pipeline-prepare`
# call, so the orchestrator no longer runs them. `pipeline-prepare` is deliberately
# absent too — the *launcher* runs it, not the model, which is the whole point of the
# phase (test_prompt_no_longer_asks_the_orchestrator_to_do_setup).
HERDR_ROUTINES_SUBCOMMANDS = ("gate",)


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


def test_issue_refinement_job_invokes_herdr_routines_via_uv_run() -> None:
    """The issue-refinement job prompt (`refine-issue`) has the same not-a-binary bug — same fix."""
    text = _read(ISSUE_REFINEMENT_JOB)
    bare = re.findall(r"(?<!uv run )herdr-routines refine-issue\b", text)
    assert not bare, (
        f"bare `herdr-routines refine-issue` in issue-refinement.yaml: {bare}"
    )
    assert "uv run herdr-routines refine-issue" in text


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


# -- issue 054 phase A: the orchestrator no longer does the mechanical setup -------------


def test_prompt_no_longer_asks_the_orchestrator_to_do_setup() -> None:
    """Phase A moved sync, pick, worktree, workspace and `state.json` into
    `herdr-routines pipeline-prepare`, which the launcher runs before the agent exists.
    A prompt that still asks a model to do them is not just redundant: it is the
    fabrication risk issue 054 exists to remove — the 2026-09-28 fallback model wrote a
    deadline a year in the past and the watchdog believed it.

    So the prompt must not *instruct* any of those steps. Asserting on the commands and
    on the prose, not just the absence of a heading, so a reworded checklist still fails.
    """
    text = _read(PROMPT)
    flat = " ".join(text.split())

    # The commands themselves are gone from the prompt: the launcher runs them.
    for gone in (
        "herdr-routines sync-repo",
        "herdr-routines pick-feature",
        "herdr worktree create",
    ):
        assert gone not in text, f"prompt still asks the orchestrator to run `{gone}`"

    # Nor the checklist prose that told it to go do them.
    for gone in (
        "Before you start, confirm",
        "Pick the feature",
        "Create the shared worktree",
    ):
        assert gone not in flat, f"prompt still has the phase-A setup checklist: {gone}"

    # ...and it says so, so the orchestrator knows the values are already resolved
    # rather than silently missing its old instructions.
    assert "pipeline-prepare" in flat
    assert "pre-flight" in flat.lower()
    # The header values the launcher appends are named, so the model knows to copy them.
    for key in ("FEATURE_IDEA", "FEATURE_SOURCE", "ISSUE_ID", "STATE_JSON"):
        assert key in text, f"prompt no longer mentions the {key} it is handed"
