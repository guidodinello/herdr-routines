---
id: "001"
title: Plugin manifest (herdr-plugin.toml)
status: done
priority: medium
area: plugin
---

## Description

Actions-only plugin manifest (no startup hook, no daemon): invoke
`herdr-routines run <job>` / `status` from inside the Herdr UI via keybinding
or `herdr plugin action invoke`, and make the tool installable with
`herdr plugin install guidodinello/herdr-routines`.

Design already done in [`docs/plan-v1.md`](../../plan-v1.md) §8.4. v1 made
config/state paths env-var-aware (`HERDR_PLUGIN_CONFIG_DIR` /
`HERDR_PLUGIN_STATE_DIR`) precisely so this needs no file moves later. Stays
within the documented plugin model — systemd keeps owning the schedule.

## Acceptance

- `herdr-plugin.toml` manifest present, actions map to existing CLI commands.
- `herdr plugin install guidodinello/herdr-routines` works from a clean
  checkout.
- No new daemon/startup-hook behavior introduced — systemd still owns
  scheduling.

## Log

- **2026-08-25**: shipped (PR #29, `20260825T000735Z` — pipeline dogfood
  run). `herdr-plugin.toml` exists at repo root. `ROADMAP.md` still listed
  this as open at curation time — stale entry.
