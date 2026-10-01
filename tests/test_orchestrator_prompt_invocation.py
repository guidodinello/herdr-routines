"""Doc-contract tests for issue 050 — whatever invokes `herdr-routines` must invoke it
via `uv run`, and the opencode permission allowlist must live in one tracked file.

Same spirit as test_pipeline_pane_lifecycle.py: grep the authority docs and parse
the committed config. The bug these guard against killed the 20260909 nightly run
(a bare `herdr-routines` call is command-not-found on the pipeline hosts, and the
caller then wedged probing `~/.local/bin`).

Issue 056 phase B changed *who* invokes it: the orchestrator prompt no longer runs at
all, and the launcher is now the only caller — so these retargeted from the prompt to
`scripts/pipeline-launch.sh`. The bug class is unchanged; only the file that carries it.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT = REPO_ROOT / "docs" / "pipeline" / "orchestrator-prompt.md"
LAUNCHER = REPO_ROOT / "scripts" / "pipeline-launch.sh"
SETUP = REPO_ROOT / "docs" / "pipeline" / "setup.md"
PERMISSION_FILE = REPO_ROOT / "deploy" / "opencode.pipeline.json"
ISSUE_REFINEMENT_JOB = REPO_ROOT / "deploy" / "jobs.d" / "issue-refinement.yaml"

# Subcommands the launcher runs. `sync-repo` and `pick-feature` used to be here; issue
# 054 phase A moved both into `pipeline-prepare`, and issue 056 phase B moved the stage
# loop into `pipeline-run` — so both are now the launcher's own calls rather than a
# model's. `gate` is what `pipeline_run` shells out to between stages; it is checked
# here only because the launcher is the file that sets the `uv run` precedent.
HERDR_ROUTINES_SUBCOMMANDS = ("pipeline-prepare", "pipeline-run")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_launcher_invokes_herdr_routines_via_uv_run() -> None:
    """Criterion 1: every `herdr-routines <subcommand>` in the launcher is `uv run herdr-routines <subcommand>`."""
    text = _read(LAUNCHER)
    for sub in HERDR_ROUTINES_SUBCOMMANDS:
        # Count command invocations of this subcommand, then confirm each one is
        # preceded by `uv run `. `(?<!uv run )` is a fixed-width lookbehind.
        bare = re.findall(rf"(?<!uv run )herdr-routines {sub}\b", text)
        assert not bare, f"bare `herdr-routines {sub}` in pipeline-launch.sh: {bare}"
        assert f"uv run herdr-routines {sub}" in text, (
            f"expected `uv run herdr-routines {sub}` in pipeline-launch.sh"
        )


def test_launcher_notes_herdr_routines_not_a_binary() -> None:
    """Criterion 2: the file that now makes the call explains why `uv run` —
    herdr-routines is not on PATH on the pipeline hosts."""
    text = " ".join(_read(LAUNCHER).split())
    assert "uv run herdr-routines" in text
    assert "not a standalone binary" in text
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


def test_retired_prompt_instructs_nothing() -> None:
    """Phase A moved sync, pick, worktree, workspace and `state.json` into
    `herdr-routines pipeline-prepare` (issue 054); phase B moved the stage loop into
    `herdr-routines pipeline-run` (issue 056). A prompt that still *instructs* any of it
    is not merely redundant — it is the fabrication risk issue 054 exists to remove. The
    2026-09-28 fallback model wrote a deadline a year in the past and the watchdog
    believed it.

    The file is kept (design docs and audits cite it by line number), so it has to be
    unambiguous that it is a historical document: it says it is retired, and it contains
    no runnable step for anything to follow.
    """
    text = _read(PROMPT)
    flat = " ".join(text.split())

    # Retirement is stated up front, not in a footnote.
    assert "no longer executed" in flat
    assert "retired" in text.lower()

    # No runnable instruction survives: every command the old prompt taught a model to
    # run is gone, and nothing it asks for is phrased as a step to take.
    for gone in (
        "herdr-routines sync-repo",
        "herdr-routines pick-feature",
        "herdr worktree create",
        "Before you start, confirm",
        "Pick the feature",
        "Create the shared worktree",
    ):
        assert gone not in flat, (
            f"retired prompt still contains a runnable step: {gone}"
        )

    # And it points at what replaced it, so a reader arriving from a stale reference
    # lands somewhere that tells them where the behaviour lives now.
    for target in ("docs/pipeline/stages/", "pipeline_stages", "pipeline_run"):
        assert target in text, f"retired prompt does not point at {target}"
