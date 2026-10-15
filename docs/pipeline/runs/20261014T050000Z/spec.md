# spec.md v2 — Issue 024: Spawn a session on the fly from Telegram

Run: 20261014T050000Z · Issue: `docs/process/issues/024-spawn-session-from-telegram.md`

v1 → v2 changes are listed in [`## Changelog v1→v2`](#changelog-v1v2).

## Problem

The `cokekitten/herdr-telegram-bridge` plugin (issue 023, done) only steers
*existing* panes via reply-to-message. There is no cold-start path: from a
fresh Telegram message, with no notification to reply to, there is no way to
create a workspace and start an agent against a named repo/job so it can then
be steered from the phone.

## Approach

1. **Command intake (new, small):** recognize a cold-start command message,
   e.g. `/run <job>` (jobs.yaml job name) or `/start <repo>` (short repo
   alias). Parse strictly: one target token, reject extra args.
2. **Auth first:** compare sender `chat_id` against the single configured
   allowlist value (same secrets file the bridge token/chat_id was moved to
   per 023 log). Non-matching chat → drop silently or with a generic refusal;
   never reveal the mapping. This check runs before any Herdr call.
3. **Target mapping (config, explicit allowlist):** resolve the token via a
   static map (new `telegram_spawn:` block in jobs config or a sibling
   `spawn-aliases.yaml`) of alias → `{repo path, agent_kind, workspace mode}`.
   Reuse the pipeline launcher mechanics (`HerdrClient.workspace_create` +
   `build_agent_start_args` / `agent start`, cf. `runner.execute_run`,
   `tick.launch_pipeline`, `pipeline_prepare`) — do not reimplement layout
   creation; `agent start` needs an existing pane (`plan-v1.md` verified
   fact). Unknown/unmapped target → helpful reply listing valid aliases, not
   a silent no-op (acceptance criterion).
4. **Reply with the handle:** on success, reply in-chat with workspace/pane id
   (and agent name `rt-<job>-<ts>`) so the existing bridge reply-to-steer
   flow takes over from there. On failure (name taken, Herdr down), reply
   with the short error, same as `run` failures.
5. **No clock, no daemon:** this is an event handler on inbound Telegram
   messages (bridge poller / bot webhook), not a scheduler addition. It must
   not touch `tick.lock`, history JSONL terminal-state logic, or the systemd
   timer. Optional: append a `running`-style audit line to history for
   traceability, but never a fabricated terminal state.

Out of scope: rich argument passing (prompt text from chat), auto-approve /
skip-permissions (plan-v1 §2 deliberate omission stands), GC of spawned
workspaces (manual, per plan-v1 §6).

## Files touched

- **New:** `src/herdr_routines/telegram_spawn.py` — parse, auth, map lookup,
  `workspace_create` + `agent start` orchestration, reply formatting. Pure
  parse/map functions kept importable for tier-1 tests.
- **Config:** `src/herdr_routines/config.py` (+ `jobs.yaml` schema /
  `deploy/jobs.example.yaml`) — `telegram_spawn:` alias map + `allowed_chat_id`
  wiring; `validate` rejects unknown repos, non-git paths, duplicate aliases.
- **Adapter:** `src/herdr_routines/herdr.py` — reuse only; extend solely if a
  needed argv builder is missing.
- **Bridge glue:** script or bridge-plugin config hooking the new entrypoint
  to inbound messages (bot command handler / poller callback); secrets stay in
  the existing secrets file, never in repo.
- **Tests:** `tests/test_telegram_spawn.py` — auth reject, unknown-target
  helpful reply, argv construction via FakeRunner, name-collision path.
- **Docs:** `deploy/README.md` (setup: token, chat_id, alias map) + this spec.

## Risks

- **Auth bypass / chat_id spoofing:** Telegram `chat_id` is the only gate;
  misconfigured allowlist (empty = allow-all) would expose arbitrary agent
  spawn. Mitigate: fail-closed when unset; `validate` errors if missing.
  [blocking] confidence: high
- **Command injection via target token:** alias must never be interpolated
  into a shell; pass as argv (Herdr plugin commands are shell-less argv
  arrays) and validate against `[a-z][a-z0-9_-]` + allowlist lookup.
  [blocking] confidence: high
- **Agent-name collision:** live `rt-<name>` blocks `agent start`
  (`agent_name_taken`; cf. `herdr.py:605`, `tick.py:3005`). Spawn must use a
  unique suffixed name and surface `agent_name_live` instead of wedging.
  [blocking] confidence: high
- **Workspace/pane leaks:** every cold spawn leaves a workspace + branch
  (plan-v1 §6: no auto-GC). Unbounded phone-triggered spawns accumulate;
  mitigate with explicit alias scoping and documenting manual `gc` cleanup.
  [non-blocking] confidence: medium
- **Notification race (023 open item):** `execute_run` closing panes on settle
  (G-16/PR #42) may race the bridge's `pane.agent_status_changed` hook; the
  spawn reply (explicit pane id) is the reliable handle, not the async
  notification.
  [non-blocking] confidence: medium
- **Scope creep into scheduler:** must not add a startup hook daemon or second
  clock (plan-v1 §8.2: plugin system owns no timer). Keep systemd timer as
  sole scheduler.
  [blocking] confidence: high

## Acceptance criteria

Cold-start spawn from Telegram (issue 024: three acceptance bullets expanded
into pinned, test-named items). Severity tiers: `blocking` = the feature does
not do what issue 024 asked, or does it in a way the phone flow cannot use;
`non-blocking` = correctness of follow-on details that must not gate the
cold-start path. Each item carries a `confidence:` rating for the reviewer's
certainty in the pin.

1. A recognized command message (`/run <job>` / `/start <repo>`) with exactly
   one target token parses to that target; messages with zero tokens, extra
   args, or non-command text are rejected without any Herdr call. blocking —
   the intake contract; loose parsing spawns the wrong checkout. confidence:
   high. Test: test_spawn_parses_cold_start_command
2. A sender whose `chat_id` does not match the single configured allowlist
   value is dropped (silently or with a generic refusal that never reveals the
   mapping or valid aliases), and no `workspace_create` / `agent start` call
   is issued. blocking — the only gate; a leak here exposes arbitrary agent
   spawn. confidence: high. Test: test_spawn_rejects_unauthorized_chat_id
3. A missing or empty `allowed_chat_id` fails closed: spawn refuses and
   `validate` errors instead of treating unset as allow-all. blocking — an
   empty allowlist as allow-all is the auth-bypass shape called out in Risks.
   confidence: high. Test: test_spawn_fails_closed_without_allowlist
4. An authenticated but unknown / unmapped target is rejected with a helpful
   reply listing the valid aliases, not a silent no-op. blocking — verbatim
   issue acceptance bullet; a silent drop is indistinguishable from a lost
   message on a phone. confidence: high. Test: test_spawn_unknown_target_lists_aliases
5. Alias-map `validate` rejects unknown repos, non-git paths, and duplicate
   aliases at load with the existing message shape. blocking — a bad map
   silently pointing at the wrong checkout bites only at spawn time.
   confidence: high. Test: test_spawn_alias_map_validation
6. The target token is never interpolated into a shell: it is validated
   against `[a-z][a-z0-9_-]` plus the allowlist lookup and passed only as
   shell-less argv (cf. `build_agent_start_args`). blocking — phone input is
   untrusted; interpolation is command injection. confidence: high. Test:
   test_spawn_target_never_shell_interpolated
7. An authorized, mapped spawn calls `workspace_create` plus `agent start`
   with the mapped `{repo path, agent_kind, workspace mode}` (no reimplemented
   layout creation) and replies in-chat with the workspace/pane id and agent
   name `rt-<job>-<ts>` so the bridge reply-to-steer flow takes over.
   blocking — verbatim issue acceptance bullet; without the handle the phone
   cannot steer. confidence: high. Test: test_spawn_success_replies_with_pane_handle
8. When `agent start` fails with `agent_name_taken`, spawn retries with a
   unique suffixed name and surfaces `agent_name_live` instead of wedging.
   blocking — the live-name holdover documented in `herdr.py:605` would
   otherwise brick every phone spawn until manual reap. confidence: medium.
   Test: test_spawn_handles_agent_name_collision
9. Spawn failures (name taken after retry, Herdr down) reply in-chat with the
   short error, same as `run` failures, and never write a fabricated terminal
   state to history (at most a `running`-style audit line). non-blocking —
   error-path polish and history honesty for the phone flow. confidence:
   medium. Test: test_spawn_failure_replies_with_short_error
10. The handler is event-only: it touches no `tick.lock`, no history JSONL
    terminal-state logic, and no systemd timer / second clock. non-blocking —
    pins the plan-v1 §8.2 no-daemon boundary so a phone feature cannot grow a
    scheduler. confidence: medium. Test: test_spawn_has_no_scheduler_side_effects

## Changelog v1→v2

Stage 2 (independent spec review) over v1. No change to the problem statement,
the approach (intake → auth-first → static alias map → reply with handle, no
clock/daemon), the out-of-scope list, or the files-touched list — v1's design
was verified against
`docs/process/issues/024-spawn-session-from-telegram.md` and kept.

Additions:

- `## Acceptance criteria` — 10 numbered items expanding the issue's three
  acceptance bullets (recognized command spawns + replies with pane; unknown
  targets get a helpful reply; only the configured `chat_id` can spawn), each
  ending `Test: <exact function name>` in `tests/test_telegram_spawn.py` so
  stage 3 has no name to invent.
- Severity and certainty tiers — every acceptance item and every risk now
  carries a `blocking` / `non-blocking` label plus a `confidence: high|medium`
  rating; tier semantics defined at the top of `## Acceptance criteria`.

Corrections to v1:

- Title `v1` → `v2` with a pointer to this changelog.
- `## Risks` items annotated with `[blocking]` / `[non-blocking]` and
  `confidence:` ratings (v1 had neither); no risk content changed.
- No files-touched, approach, or scope changes: review confirmed the file list
  (new `telegram_spawn.py`, `config.py` `telegram_spawn:` map +
  `allowed_chat_id`, `herdr.py` reuse-only, bridge glue, new
  `tests/test_telegram_spawn.py`, `deploy/README.md`) matches the issue's
  cold-start contract.
