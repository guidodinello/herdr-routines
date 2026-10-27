"""Pin for issue 068's docs change (Part 5): the places that used to promise the old
kill-and-report-only watchdog must now describe the single automatic resume attempt and
the `resume_count` cap instead.

These read the files at their real paths on purpose — a docstring or design paragraph
whose wording drifts back to "never resumes" is exactly the failure the acceptance
criterion exists to catch, and no fixture can stand in for the wording."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_resume_once_is_documented() -> None:
    """The watchdog module docstring, design.md's checkpointing/resume paragraph and
    issue 031's Log all say what the code now does: one resume attempt, capped by
    `resume_count` — not "kill + report only, never resumes"."""
    watchdog = (
        REPO_ROOT / "src" / "herdr_routines" / "pipeline_watchdog.py"
    ).read_text()
    module_docs = watchdog[: watchdog.find('"""', len('"""')) + 3].lower()
    # The new behaviour is named, with its cap ...
    assert "resume_count" in module_docs
    assert "resume" in module_docs
    # ... and the old wrong promise is gone: not "never resumes", not a plain
    # "does not resume/retry" statement.
    assert "never resum" not in module_docs
    assert "does not resume" not in module_docs

    design = (REPO_ROOT / "docs" / "pipeline" / "design.md").read_text()
    assert "resume_count" in design

    issue_031 = next((REPO_ROOT / "docs" / "process" / "issues").glob("031-*.md"))
    issue_text = issue_031.read_text()
    log = issue_text.split("## Log", 1)[1]
    # The Log entry points issue 031's narrowing at this change (068 deferred it in
    # 031 explicitly) and describes the single attempt and the cap there.
    assert "resume_count" in log
    assert "068" in log
