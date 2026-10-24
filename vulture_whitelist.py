"""Names vulture reports as unused that are in fact used, scanned alongside src/ (see
[tool.vulture] in pyproject.toml). Add an entry only for a false positive, with a one-line
reason; code that is genuinely unused gets deleted instead. tests/ is deliberately not
scanned, so a helper only tests call shows up here as unused — that is the point.

Vulture matches by name, not by owner: whitelisting `X.foo` hides every unused `foo` in
src/. Keep entries specific and few.
"""

from herdr_routines.herdr import AgentEntry, WorkspaceWorktree
from herdr_routines.history import HistoryExtra
from herdr_routines.issue_refinement import ISSUE_REFINEMENT_CRON
from herdr_routines.pipeline_stages import stage_families

# TypedDict keys: written and read through ["key"] / .get("key"), which vulture cannot see.
AgentEntry.agent_status  # set in _parse_agent_list, read via agent.get("agent_status")
WorkspaceWorktree.checkout_path  # set in _parse_workspace_list, read in pipeline_prepare
HistoryExtra.phase  # every HistoryExtra key below is written via extra={...} by a run path
HistoryExtra.trigger
HistoryExtra.reaped_agents
HistoryExtra.fix_worker_agent
HistoryExtra.fix_report_path
HistoryExtra.manifest_path
HistoryExtra.audit_output_path
HistoryExtra.failed_checks
HistoryExtra.max_retries
HistoryExtra.final_attempt
HistoryExtra.primary_run_id
HistoryExtra.retried_from_run_id
HistoryExtra.retry_eligible_reason
HistoryExtra.pipeline_run_id
HistoryExtra.findings_total
HistoryExtra.polled
HistoryExtra.event_number
HistoryExtra.last_seen_event_ids
HistoryExtra.headRefName
HistoryExtra.eligible_reason

# Spec pins asserted by tests against a real deploy file, not read at runtime.
ISSUE_REFINEMENT_CRON  # test_issue_refinement checks the deploy job file's cron against it

# Invariant checks on the static STAGES table. The table is a constant, so a test-time check
# is complete; stage_families (and model_family/MODEL_FAMILY, flagged only through it) live
# beside STAGES so a new model gets its family declared in the same edit.
stage_families
