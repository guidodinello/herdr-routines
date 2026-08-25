# spec: herdr-plugin.toml front-door manifest — 20260825T000735Z

## Problem

`herdr-routines` is currently invoked only from the shell (`herdr-routines run/status`) and on a schedule owned by systemd user timers (`herdr-routines tick` every 5 min). There is no in-Herdr front door: users cannot trigger `run`/`status` from the Herdr UI via keybinding or `herdr plugin action invoke`, and the tool is not installable via `herdr plugin install guidodinello/herdr-routines`.

Research in `docs/plan-v1.md:582-586` (§8.4) found the Herdr plugin system **cannot own the clock** — the manifest (documented at `herdr.dev/docs/plugins/`) declares build commands, startup hooks, actions, event handlers and panes but has no timer/cron/tick event, and startup hooks are explicitly "one-shot initialization ... rather than supervised daemons" that must exit. All three community plugins work around this by detaching a daemon from a startup hook, against the documented model. The platform-intended clock is an external scheduler (systemd — already implemented in `deploy/systemd/`).

What is missing is the thin, additive complement: a manifest that exposes *actions only* as a front door into the existing CLI, while systemd keeps owning the schedule.

## Approach

Add one new file at the repo root, `herdr-plugin.toml`, declaring **actions only — no `startup`/background-hook fields, no daemon**:

- Manifest shape follows `herdr.dev/docs/plugins/` exactly: `name`, `version`, and an `actions` list. No `startup`/`[[startup]]`/daemon-detach entries — v1's research found the plugin system explicitly cannot own the schedule, so the manifest must not try.
- Two actions:
  - `run` — invokes `herdr-routines run <job>`, taking a required `job` name parameter (the job `name` key from `jobs.yaml`, i.e. `[a-z][a-z0-9_-]{0,23}` as validated in `src/herdr_routines/config.py:60`). Implemented as a direct `herdr-routines run` invocation; if Herdr actions require a wrapper script (per manifest `command`/`shell` conventions), add a minimal shim that forwards `job` to the CLI and preserves exit codes.
  - `status` — invokes `herdr-routines status` with no parameters.
- Both actions are user-invocable workflows triggered via keybinding or `herdr plugin action invoke --plugin guidodinello/herdr-routines --action run/status`, staying entirely within the documented plugin model.

No file-location migration: `plan-v1.md:582-586` made config/state path resolution env-var-aware specifically so this manifest needs no moves. Verify (not re-derive) that fallback still holds:

- `src/herdr_routines/config.py:138-147` — `default_config_path()` returns `$HERDR_PLUGIN_CONFIG_DIR/jobs.yaml` when set, else `~/.config/herdr-routines/jobs.yaml`.
- `src/herdr_routines/history.py:31-40` — `default_history_path()` returns `$HERDR_PLUGIN_STATE_DIR/history.jsonl` when set, else `~/.local/state/herdr-routines/history.jsonl`.
- `src/herdr_routines/tick.py:34-41` — `default_lock_path()` and `src/herdr_routines/cli.py:37-48` (`default_log_path()`) / `src/herdr_routines/runner.py:187-196` (`default_reports_dir()`) follow the same `HERDR_PLUGIN_STATE_DIR` fallback.

Systemd continues to be the scheduler; the plugin never ticks, never polls, never holds a lock. `tick` remains the only writer that acquires `tick.lock` (`src/herdr_routines/tick.py:44-62`).

Verification: `herdr plugin install guidodinello/herdr-routines` succeeds from a clean checkout; `herdr plugin action invoke --plugin herdr-routines run --param job=<existing-job>` and `status` produce the same output/exit codes as the equivalent shell CLI; `herdr --help` shows no new `--version`-style flag interaction; manifest validates against `herdr.dev/docs/plugins/` schema (no `startup` key).

## Files touched

- `herdr-plugin.toml` (new, repo root) — the only required change. Declares `name = "herdr-routines"` (or `guidodinello/herdr-routines` per registry naming), `version` matching `pyproject.toml:3` (`0.1.0`), and `[[actions]]` entries for `run` and `status`. No `startup`/`hooks` section.
- Shim script if needed for action invocation (e.g. `scripts/herdr-plugin-run.sh` or inline `command` in manifest) — only if Herdr action `command` cannot directly interpolate the `job` param into `herdr-routines run $job`. Thin forwarding, no scheduler logic, no file I/O beyond what the CLI already does. Omit entirely if direct `command = "herdr-routines run {{job}}"` works.
- No changes to `src/herdr_routines/config.py`, `history.py`, `tick.py`, `cli.py`, `runner.py` — verify the env-var fallbacks above rather than editing them; any edit there is out of scope for this purely additive feature.
- Tests/docs — no production code changes beyond the manifest/shim, but a follow-up should add a manifest-schema lint (assert no `startup` key, assert both actions present) and a smoke test invoking actions via `herdr plugin action invoke` against a fake Herdr server.

## Risks

- **Manifest schema drift.** The plugin docs at `herdr.dev/docs/plugins/` are the authority; a field name mismatch (`actions` vs `action`, param declaration syntax, `command` quoting) would make `herdr plugin install` fail. Mitigation: copy the documented example verbatim, validate with `herdr plugin validate` / `herdr plugin install --dry-run` if available, and keep `name`/`version` in sync with `pyproject.toml:3` / `src/herdr_routines/__init__.py:3`.
- **Startup-hook temptation / daemon creep.** The main risk this design avoids is re-introducing a daemon via `startup` (as all three community plugins do). Any `startup`/`ensure-worker --detach` addition would violate the "actions only" contract, reintroduce NFS double-fire and silent-crash risks noted in `docs/plan-v1.md:532`, and conflict with `TimeoutStartSec` / `tick.lock` ownership. Mitigation: CI lint that `herdr-plugin.toml` contains no `startup` key.
- **Action param handling.** `run` must take a `job` name that matches `NAME_RE` (`src/herdr_routines/config.py:60`). An empty or malformed param should surface the same `ConfigError`/`no such job` exit as `herdr-routines run` does, not a silent no-op. The shim (if any) must forward exit codes unchanged so Herdr UI shows failure.
- **Env-var fallback regression (low risk, verify anyway).** If a future edit drops the `HERDR_PLUGIN_CONFIG_DIR`/`HERDR_PLUGIN_STATE_DIR` fallback, plugin installs would silently read/write different paths than shell/systemd invocations. The spec intentionally requires *verification* (`config.py:138-147`, `history.py:31-40`, `tick.py:34-41`) not re-derivation, and a regression test that `default_config_path()`/`default_history_path()` respect the env vars.
- **Scope creep.** No scheduler, no tick-loop, no `permission_mode`, no GC, no notification changes. Keeping the diff to one TOML file (+ optional shim) makes review/rollback trivial and avoids interaction with `runner.py:305-509` / `tick.py:85-252`.

## Acceptance criteria

1. `herdr-plugin.toml` exists at repo root, declares `name`/`version` and exactly two actions `run` (with `job` param) and `status`, and contains no `startup`/daemon key Test: test_plugin_manifest_actions_only
2. `herdr plugin install guidodinello/herdr-routines` succeeds and both actions invoke the CLI with identical exit codes to shell Test: test_plugin_action_invoke_run_and_status
3. Config/state resolution still prefers `HERDR_PLUGIN_CONFIG_DIR`/`HERDR_PLUGIN_STATE_DIR` with XDG fallback Test: test_plugin_env_var_fallback

## Review notes

blocking: manifest must be actions-only (no startup hook/daemon); run action must forward job param; env-var fallback at config.py:138-147 / history.py:31-40 must still hold
non-blocking: shim is optional if manifest can interpolate param directly; version string sync with pyproject.toml is cosmetic but should match
confidence: high
