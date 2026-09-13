# Pane and artifact retention policy

Canonical reference for when panes/sessions are cleaned up and how long
artifacts are kept. Issue 011 owns the policy; issue 021 / `gc.py` owns
the pruning mechanism.

## Pane / session cleanup

Every settled run that owns a pane (worktree mode, `workspace != "root"`)
closes its pane **immediately after tail capture**, on both success and
terminal failure. A human resumes-and-inspects via the captured
`session_id` (`history.jsonl` + `state.json:agent_session.value`), not via
a lingering pane.

**Ordering invariant** (never reordered):

1. Capture tail to `reports/{run_id}.tail.txt` (best-effort, never raises,
   `agent_read --source visible --lines 200`).
2. Capture `session_id` via `client.agent_session_id` (before close — a
   closed pane's agent record won't answer `agent get`).
3. `pane_close`.

**Exceptions:**

- **`blocked`**: captures visible tail but deliberately skips `pane_close`
  and `session_id` — the pane stays open so a human can see what the agent
  is stuck on.
- **`workspace == "root"`**: never closes a pane and never captures
  `session_id` on success — root jobs share the ambient workspace.

**Pipeline workers**: same policy — per-stage close on gate-pass
(`design.md` G-16), orchestrator close at end, mirroring routine jobs.
Pipeline tails are written to `reports/pipeline-{run_id}.tail.txt`.

## Artifact retention

| Artifact | Retention | Notes |
|----------|-----------|-------|
| `reports/{run_id}.md` | 14 days | Routine job report |
| `reports/{run_id}.tail.txt` | 14 days | Bounded visible tail (200 lines) |
| `reports/pipeline-{run_id}.md` | 14 days | Pipeline run report |
| `reports/pipeline-{run_id}.tail.txt` | 14 days | Pipeline bounded tail |
| `history.jsonl` | Indefinite | Append-only, small; rotation per issue 021 |
| `session_id` (in history/state) | Indefinite | Short string, not a transcript |

The 14-day window aligns with `gc.py:25` `DEFAULT_OLDER_THAN_DAYS = 14`
for `auto/*` branches. Pruning is explicit (`herdr-routines gc` /
issue 021 rotation command), never automatic without opt-in — matches
issue 021's acceptance ("nothing is deleted automatically without opt-in").

## Tail capture bound

The canonical tail bound is **200 lines** via `agent_read --source
visible --lines 200` (`_capture_visible_tail`). Same bound for success
and failure paths. Documented as a code constant + spec; future tuning
requires a spec change, not a silent constant drift.

## References

- `docs/pipeline/design.md` G-16 (per-stage close, pane-lifecycle v2)
- `docs/plan-v1.md` §93 (alternate-screen scrollback caveat)
- `src/herdr_routines/runner.py` `_capture_visible_tail`
- `src/herdr_routines/gc.py` `DEFAULT_OLDER_THAN_DAYS`
- `docs/process/issues/011-pane-session-retention-policy.md`
