---
id: "027"
title: "Pi /tmp tmpfs hygiene (agent-runtime .so leak)"
status: done
priority: medium
area: infra
---

## Description

The Raspberry Pi's `/tmp` is a **2 GB tmpfs backed by RAM**. Agent runtimes for
the "free" model providers bundled in `models.json` (nano-gpt / orcarouter /
qiniu-ai) drop a compiled Rust shared object into `/tmp` — `/.3cdc<16-hex>-00000001.so`,
~5 MB, aarch64 — **on every agent spawn, and never delete it**. Add pytest
artifacts (`/tmp/pytest-of-guido`), the `/tmp/opencode` directory, and leaked
plugin files and the tmpfs fills.

When `/tmp` is at 100%, `herdr agent start` fails with `timed out waiting for
agent startup` — indistinguishable from a quota-exhaustion `blocked` without a
disk check (`free -h` / `df -h` / `du`). Hit on run `20260830T050021Z`: 6 failed
start attempts across 2 workspaces/models (~40 min stall) before a manual
`rm -rf /tmp/.3cdc* /tmp/opencode /tmp/pytest-of-guido` freed 1.4 GB (100% →
34%). The run report recorded the cleanup as `/tmp/.dcc*`, but that glob was
wrong — the real pattern is `/.3cdc*.so` (the report's own `/tmp/.d*`
inventory).

## Acceptance

- An age-based cleanup removes `/.3cdc*.so` (and sibling native-runtime leaks),
  `/tmp/pytest-of-*`, and other stale agent/pytest artifacts older than a
  configurable window, on a schedule, without touching anything newer.
- Safe against live runs: an in-flight agent's runtime file (if still dlopened /
  written this run) is never removed mid-spawn.
- Either the leak is fixed at the source (provider runtime cleans up after itself)
  **or** the scheduled cleanup keeps `/tmp` under a threshold; `/tmp` never fills
  enough to stall an agent start.
- A failure diagnosis distinguishes full-`/tmp`/disk from quota/`blocked` (log
  `df -h /tmp`, not just the start timeout).

## Log

- **2026-08-30**: filed from run `20260830T050021Z` next-steps. Investigation:
  Codex is **not** involved (no `codex` binary nor `~/.codex` on the Pi). The
  `.so` is a Rust-compiled, stripped client runtime (regex + net symbols per
  `strings`/`nm`), created per agent spawn for the free-model providers. Ambient
  mtimes (hourly tick agents 05:00/06:00/07:00/08:00) show it leaks on every
  spawn, not just pipeline runs.
