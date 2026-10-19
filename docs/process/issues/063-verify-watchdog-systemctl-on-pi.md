---
id: "063"
title: "Verify systemctl --user works inside the watchdog unit on the Pi"
status: blocked
priority: high
area: infra
gate: operator task, to be run by hand on the Pi; not for the pipeline
---

## Description

Since PR #187, the pipeline watchdog asks systemd whether a run's unit is
still active before reaping it:

```bash
systemctl --user list-units --all --plain --no-legend \
  --state=active,activating,deactivating,reloading 'herdr-pipeline-*'
```

When that command fails (no user bus, no `XDG_RUNTIME_DIR`), the watchdog
reads "can't tell" and **never reaps**. That is safe, but it silently turns
the watchdog off. The unit tests stub `systemctl`; nobody has run it inside
the real `herdr-routines-watchdog` unit yet.

`blocked` only so `pick-feature` never hands this to the pipeline: it needs a
human on the Pi.

## Acceptance

On the Pi, run the command from inside the watchdog unit's environment, for
example:

```bash
systemd-run --user --wait --pipe --collect \
  systemctl --user list-units --all --plain --no-legend 'herdr-pipeline-*'
```

It must exit 0. Also check `journalctl --user -u herdr-routines-watchdog` for
"systemd liveness is unknown" warnings after a pipeline night. If it fails,
fix the unit environment in `deploy/systemd/`. Then mark this `done`.

## Log

- **2026-10-19**: filed from the audit-fixes run (PR #187 follow-up).
