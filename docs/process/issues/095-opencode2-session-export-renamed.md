---
id: "095"
title: "opencode 2 renamed `opencode export`, so a blocked pipeline stage never falls back on a quota error"
status: done
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

- The export uses `opencode session export` (written to a file, as today). No host runs
  opencode 1 any more (Pi 2.4.1, laptop 2.1.4), so the old command is dropped, not kept.
- `_session_api_error` recognises opencode 2's `error` (`status` + `message`, `type`
  `provider.*`); opencode 1's `APIError` shape is dropped with the command. A fixture holds
  the real message above.
- A stage that settles `blocked` with a 429 like the one above goes to `fallback_model`.
- `scripts/ci-check.sh` is green.

## Log

- **2026-11-26**: filed. Command rename and new shape verified on the Pi (opencode 2.4.1).
  Related: issue 091 (screen markers for the same quota case).
- **2026-11-26**: fixed. `_opencode_export` runs `opencode session export <id>`;
  `_session_api_error` reads the `error` of the last `assistant`/`idle` message when its
  `type` starts with `provider.` (so a `system` message after an aborted turn does not hide
  it, and `aborted` is not a provider error). Checked live on the Pi (opencode 2.4.1):
  - The 429 export of `ses_de9a0ad5fffemFKDFnn5F06wsI` now reads
    `429: Free usage limit reached. ...` through the new code.
  - The pipe truncation of opencode 1.18.35 does not reproduce: the Pi's largest session,
    4246526 bytes, piped to `wc -c` gave the file's size on three of three runs. The temp
    file stays, as it costs nothing.
  - Every error shape in the Pi's `opencode.db`: `provider.free-limit` 429 (with
    `retryAfterMs`), `provider.quota` 402, `provider.error` (no status), `aborted`. The
    flat `error` sits on an `assistant` or `idle` message; an aborted export ends with a
    `system` message after the errored turn.
  - Correction: that session ran `opencode/deepseek-v4.1-flash-free` (stage 6's primary),
    not big-pickle. All nine 429s on 11-25/26 were deepseek-v4.1-flash-free.
  - Zen's free limit: per model or per account is still open. On 11-25 a 429 with
    `retryAfterMs` to 11-26 00:21 was followed by ~200 deepseek calls at 15:00 the same
    day, so `retryAfterMs` overstates the lockout.
  Not deployed: the next Pi stage that hits a quota error is the live confirmation.
