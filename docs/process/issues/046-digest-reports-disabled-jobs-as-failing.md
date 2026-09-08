---
id: "046"
title: "Digest reports disabled jobs' stale terminal states, so a switched-off job looks broken forever"
status: done
priority: medium
area: infra
---

## Description

`herdr-routines digest` (issue 010) lists every job's last terminal state. It does
not consider whether the job is still `enabled`, so a job that was switched off
keeps showing the failure it happened to end on — indistinguishable from something
actively broken right now.

Measured on the digest's **first real run** against the Pi's live history,
2026-09-06:

```
- fitted-implementer:   failed at 2026-09-02 05:00 -03
- fitted-pr-review-2:   failed at 2026-09-02 07:00 -03
- fitted-pr-review-4:   failed at 2026-09-05 09:00 -03
- fitted-pr-review:     failed at 2026-09-05 06:00 -03
- herdr-pr-ci-fix:      missed at 2026-08-29 17:50 -03
```

Five entries that read as problems. Checking `enabled:` in `jobs.d/`:

| job | enabled | reality |
|---|---|---|
| `fitted-pr-review` | true | **genuinely failing** (`repo_sync_failed`) |
| `fitted-pr-review-4` | true | **genuinely failing** (`repo_sync_failed`) |
| `fitted-implementer` | false | stale — disabled after that run |
| `fitted-pr-review-2` | false | stale — disabled after that run |
| `herdr-pr-ci-fix` | false | stale — disabled after that run |

**Three of five were false alarms** — 60% noise on the first run, and it does not
decay: a disabled job's last failure is frozen in the digest forever.

That directly undercuts what the digest is for. Issue 009's framing is "an
unattended overnight run should push exactly one notification ... not a stream of
progress pings"; a morning summary where most red lines are not real trains the
reader to skim it, which is the same failure as sending too many notifications by a
different route. The bug this whole area exists to prevent — a real failure going
unnoticed — gets *more* likely, not less, if the digest cries wolf.

Related but distinct: it also mislabels. `herdr-pr-ci-fix` shows `missed
(outside_catch_up_window)`, which was accurate at the time and is now just the last
thing that happened before someone turned it off.

## Design (proposal)

The digest already loads the job config to know which jobs exist; `Job.enabled` is
right there.

- **Disabled jobs are not failures.** Either omit them, or render them in a clearly
  separate, non-alarming form (`- fitted-implementer: (disabled)`), with the stale
  state available but plainly marked as historical.
- Prefer **omit by default, `--include-disabled` to show them.** A morning digest
  should answer "what needs me today", and a switched-off job never does. Keep the
  flag so "why is nothing running?" is still answerable from the same command.
- While in there, consider a **staleness guard for enabled jobs too**: an enabled
  job whose last record predates its own schedule by a wide margin is a different
  and more interesting signal ("should have run, didn't") than a plain terminal
  state. Do not silently fold that into the same line — it is closer to `missed`
  than to `failed`. Out of scope here if it grows; file separately.

Do not add a new state store — the digest's constraint from issue 010 stands: read
existing `history.jsonl`, the reports dir, and the job config.

## Acceptance criteria

1. A disabled job does not appear in the default digest. Test: `test_digest_omits_disabled_job`
2. `--include-disabled` shows it, marked as disabled rather than as a failure. Test: `test_digest_include_disabled_marks_not_fails`
3. An enabled job's failing state is still reported unchanged. Test: `test_digest_still_reports_enabled_failure`
4. A digest where every job is disabled says so, rather than rendering empty. Test: `test_digest_all_disabled_is_explicit`
