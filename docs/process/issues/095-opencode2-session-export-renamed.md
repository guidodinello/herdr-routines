---
id: "095"
title: "opencode 2 renamed `opencode export`, so a blocked pipeline stage never falls back on a quota error"
status: open
priority: high
area: pipeline
---

## Description

`pipeline_run._opencode_export` runs `opencode export <session_id>`. opencode 2 removed that
top-level command: it prints the CLI help and exits non-zero, so every stage since the Pi's
upgrade to 2.4.1 logs `opencode export <id> failed: ERROR` (7 times on 2026-11-25 and 11-26).
The command is now `opencode session export [--sanitize] <session>`.

This is not only a lost transcript. `_fallback_cause` reads the export through
`_session_api_error` to tell a provider error from a permission prompt when a stage settles
`blocked`. With no export, a quota error looks like a real block and `fallback_model` never
runs. That happened on 2026-11-26: run `20261126T050000Z` stage 6 (session
`ses_de9a0ad5fffemFKDFnn5F06wsI`, `opencode/big-pickle`) hit Zen's free limit and the run
ended `failed (stage_blocked)`.

The document's shape changed too. `opencode session export` on 2.4.1 returns
`{"info": ..., "messages": [...]}` (151179 bytes for that session), but the last message is
now a flat event, not opencode 1's `info.error = {"name": "APIError", "data": {...}}`:

```json
{"id": "msg_21660a486001G5DLjrc3x2FRQC", "type": "idle", "outcome": "failed",
 "error": {"type": "provider.free-limit", "message": "Free usage limit reached. ...",
           "status": 429, "retryAfterMs": 66079000}}
```

So `_session_api_error` returns None even with a working export.

## Acceptance

- The export uses `opencode session export` (written to a file, as today: the pipe
  truncation note may no longer apply but is not re-verified here), on hosts with opencode 2.
- `_session_api_error` recognises opencode 2's `error` (`status` + `message`, `type`
  `provider.*`) as well as opencode 1's `APIError`; a fixture holds the real message above.
- A stage that settles `blocked` with a 429 like the one above goes to `fallback_model`.
- `scripts/ci-check.sh` is green.

## Log

- **2026-11-26**: filed. Command rename and new shape verified on the Pi (opencode 2.4.1).
  Related: issue 091 (screen markers for the same quota case).
