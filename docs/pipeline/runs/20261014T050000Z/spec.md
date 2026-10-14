# spec.md v1 — Issue 024: Spawn a session on the fly from Telegram

Run: 20261014T050000Z · Issue: `docs/process/issues/024-spawn-session-from-telegram.md`

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
- **Command injection via target token:** alias must never be interpolated
  into a shell; pass as argv (Herdr plugin commands are shell-less argv
  arrays) and validate against `[a-z][a-z0-9_-]` + allowlist lookup.
- **Agent-name collision:** live `rt-<name>` blocks `agent start`
  (`agent_name_taken`; cf. `herdr.py:605`, `tick.py:3005`). Spawn must use a
  unique suffixed name and surface `agent_name_live` instead of wedging.
- **Workspace/pane leaks:** every cold spawn leaves a workspace + branch
  (plan-v1 §6: no auto-GC). Unbounded phone-triggered spawns accumulate;
  mitigate with explicit alias scoping and documenting manual `gc` cleanup.
- **Notification race (023 open item):** `execute_run` closing panes on settle
  (G-16/PR #42) may race the bridge's `pane.agent_status_changed` hook; the
  spawn reply (explicit pane id) is the reliable handle, not the async
  notification.
- **Scope creep into scheduler:** must not add a startup hook daemon or second
  clock (plan-v1 §8.2: plugin system owns no timer). Keep systemd timer as
  sole scheduler.
