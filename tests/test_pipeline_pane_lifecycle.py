"""Doc-contract tests for pane-lifecycle v2 close-and-resume (20260825T021919Z).

Mostly doc-level, in the spirit of test_plugin_manifest.py: grep the two pipeline
authority docs and the proposal for required content.

Issue 056 phase B promoted the feature from convention to code. The two criteria that
used to be satisfied by the orchestrator prompt's prose — per-stage pane close on
gate-pass, and stage 6's close-then-resume — are now `pipeline_run`'s behaviour, so those
two tests read `src/herdr_routines/pipeline_run.py` instead. The rest stay doc-level:
design.md is still where the contract is written down.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DESIGN = REPO_ROOT / "docs" / "pipeline" / "design.md"
PROMPT = REPO_ROOT / "docs" / "pipeline" / "orchestrator-prompt.md"
PIPELINE_RUN = REPO_ROOT / "src" / "herdr_routines" / "pipeline_run.py"
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


def test_pipeline_run_closes_each_stage_pane_after_its_gate() -> None:
    """Criterion 3: a stage's pane is closed once that stage's gate passes, not only at
    end of run. Was prose in the prompt's worker-spawn template; now `pipeline_run`'s
    per-stage finally-block."""
    text = _read(PIPELINE_RUN)
    # The close is scoped to one stage's pane, through the same client that started it.
    assert "_close_pane(client, pane_id)" in text
    # ...and it happens inside the per-stage function on the gate path, right after the
    # verdict rather than in a run-wide sweep at the end. Position is the assertion:
    # the close follows the gate call and is guarded on `pane_id`, so a stage with no
    # agent (stage 4) closes nothing and a failed gate closes before reporting.
    gate_idx = text.index("verdict = _gate_for_stage")
    close_idx = text.index("if pane_id is not None:", gate_idx)
    assert gate_idx < close_idx


def test_pipeline_run_stage6_resumes_the_stage3_session() -> None:
    """Criterion 4: stage 6 resumes stage 3's real session id with
    `agent_start(..., session_id=...)` against a fresh pane, rather than prompting a pane
    held open since stage 3."""
    text = _read(PIPELINE_RUN)
    # The resume is a real parameter on the start call, not a string in a prompt.
    assert "session_id=resume_session," in text
    # ...and that value is read out of the session the run recorded for the reused
    # stage, so it is the stage-3 id and not a fresh one. A missing id aborts rather
    # than silently starting a blank session that would pass the layout check anyway.
    assert "resume_session" in text
    assert "resume_session_missing" in text
    assert "reuses_stage" in text
    # The layout is the reason it works: stage 6 repeats stage 3's session id.
    assert "reuses_stage" in _read(
        REPO_ROOT / "src" / "herdr_routines" / "pipeline_stages.py"
    )


def test_proposal_doc_marked_implemented() -> None:
    """Criterion 5: proposal doc is updated to Status: implemented with PR pointer."""
    text = _read(PROPOSAL)
    assert "Status: implemented" in text
    assert "auto/pipeline-20260825T021919Z" in text
    # Should still reference the evidence but not be marked as proposal
    assert "PR" in text


# ---------------------------------------------------------------------------
# Issue 011: pane/session retention — retention policy documented
# ---------------------------------------------------------------------------

RETENTION_DOC = REPO_ROOT / "docs" / "process" / "pane-retention.md"


def test_retention_policy_documented() -> None:
    """AC 8: Retention policy is documented in docs/process/pane-retention.md
    (and referenced from docs/pipeline/design.md) stating: panes close immediately
    after capture (exceptions: blocked stays open, root never closes), artifacts
    retained 14 days, pruning explicit/opt-in only, session_id retained indefinitely."""
    assert RETENTION_DOC.exists(), "docs/process/pane-retention.md must exist"
    text = _read(RETENTION_DOC)

    # Pane cleanup policy
    assert "immediately after tail capture" in text.lower()
    assert "blocked" in text
    assert "stays open" in text.lower()
    assert "root" in text
    assert "never closes" in text.lower()

    # Artifact retention window
    assert "14 days" in text

    # Pruning is explicit, not automatic
    assert "opt-in" in text.lower() or "explicit" in text.lower()

    # Session id retained indefinitely
    assert "session_id" in text
    assert "indefinite" in text.lower()

    # Ordering invariant documented
    assert "capture" in text.lower()
    assert "session_id" in text
    assert "pane_close" in text or "pane close" in text.lower()

    # Reference from design.md
    design_text = _read(DESIGN)
    assert "pane-retention.md" in design_text
