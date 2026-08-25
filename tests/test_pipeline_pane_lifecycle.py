"""Doc-contract tests for pane-lifecycle v2 close-and-resume (20260825T021919Z).

Same spirit as test_plugin_manifest.py: grep the two pipeline authority docs
and the proposal for required content. Criteria are intentionally doc-level —
the feature is an orchestrator-prompt convention, not src behavior (Option C
deferred, design.md).
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DESIGN = REPO_ROOT / "docs" / "pipeline" / "design.md"
PROMPT = REPO_ROOT / "docs" / "pipeline" / "orchestrator-prompt.md"
PROPOSAL = REPO_ROOT / "docs" / "pipeline" / "pane-lifecycle-v2-proposal.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_design_doc_documents_per_stage_pane_close() -> None:
    """Criterion 1: design.md's cleanup section documents per-stage pane close on gate-pass (not only end-of-run)."""
    text = _read(DESIGN)
    # Exact phrases from the acceptance criterion / design G-16 wording
    assert "per-stage pane close on gate-pass" in text.lower()
    assert "not only end-of-run" in text.lower()
    # Must describe closing once gate passes, not only final sweep
    assert "close this worker's pane once its gate passes" in text.lower()
    assert (
        "close a worker's pane as soon as its stage's gate has passed" in text.lower()
    )


def test_design_doc_documents_session_resume_mechanism() -> None:
    """Criterion 2: design.md documents the close-then-resume-by-session-id mechanism, including -s <session_id>."""
    text = _read(DESIGN)
    assert "close-then-resume-by-session-id" in text
    assert "-s <session_id>" in text
    assert "agent_session.value" in text
    # Empirically verified flag must be mentioned
    assert "2026-08-25" in text
    # Must mention the concrete command shape for pl-6 resume
    assert "herdr agent start pl-6-" in text


def test_orchestrator_prompt_includes_pane_close_step() -> None:
    """Criterion 3: orchestrator-prompt.md's worker spawn template includes a close-pane step."""
    text = _read(PROMPT)
    assert "close this worker's pane once its gate passes" in text.lower()
    # Must be in the spawn template section, not only in cleanup
    assert "Worker spawn template" in text
    # The close should be tied to gate-pass + handoff confirmed on disk
    assert "gate passes" in text
    # Ensure the step mentions herdr pane/tab close
    assert "herdr pane close" in text


def test_orchestrator_prompt_stage6_uses_session_resume() -> None:
    """Criterion 4: orchestrator-prompt stage 6 uses herdr agent start ... -s <session_id> against fresh pane."""
    text = _read(PROMPT)
    # Stage 6 header must exist
    assert "Stage 6" in text
    # Must use the new resume form, not the old prompt-against-held-pane
    assert "herdr agent start pl-6-" in text
    assert "-s <session_id>" in text
    assert "fresh pane" in text
    # Must explicitly forbid the old hold-open form
    assert (
        "herdr agent prompt pl-3-" not in text or "Do not" in text
    )  # allow mention only if negated
    # Strong check: stage 6 section should contain -s and fresh pane together
    # Find stage 6 slice
    idx = text.index("Stage 6")
    slice6 = text[idx : idx + 3000]
    assert "-s <session_id>" in slice6
    assert "fresh pane" in slice6


def test_proposal_doc_marked_implemented() -> None:
    """Criterion 5: proposal doc is updated to Status: implemented with PR pointer."""
    text = _read(PROPOSAL)
    assert "Status: implemented" in text
    assert "auto/pipeline-20260825T021919Z" in text
    # Should still reference the evidence but not be marked as proposal
    assert "PR" in text
