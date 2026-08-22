# Deploying herdr-routines

Two systemd **user** units drive this tool — no daemon of our own, no root required. See
[`../docs/plan-v1.md`](../docs/plan-v1.md) §3 for the full rationale.

## Install

`herdr-routines.service`'s `WorkingDirectory` and `ExecStart` hardcode this checkout's path and
this host's `uv` location (`%h/projects/herdr-routines`, `%h/.local/bin/uv`) — edit both before
copying the unit to a host with a different checkout path or `uv` install location.

```sh
mkdir -p ~/.config/systemd/user
cp systemd/herdr-server.service systemd/herdr-routines.timer systemd/herdr-routines.service \
   ~/.config/systemd/user/
systemctl --user daemon-reload
```

**On a fresh Pi (not needed on this laptop — `Linger` is already `yes` here):**

```sh
sudo loginctl enable-linger "$(whoami)"
loginctl show-user "$(whoami)" --property=Linger   # must print Linger=yes
```

Without this, user units die at logout and nothing fires overnight.

Copy [`jobs.example.yaml`](jobs.example.yaml) to
`~/.config/herdr-routines/jobs.yaml` (or wherever `herdr_routines.config.default_config_path()`
resolves to) and edit it for your actual jobs. `jobs.yaml` is host-specific — `repo:` paths are
local to whichever machine runs the jobs (see plan §3) — so it is not committed.

Then:

```sh
uv sync
uv run herdr-routines validate
systemctl --user enable --now herdr-server.service
systemctl --user enable --now herdr-routines.timer
```

## Manual smoke checklist

A green test suite proves the code matches our model of the `herdr` CLI — it does not prove
the model is right, or that the systemd environment actually works. Run these in order (see
docs/plan-v1.md §7 tier 3) on any new host before trusting the timer:

1. `uv run herdr-routines validate` — config parses, repo paths exist, and the systemd unit's
   `TimeoutStartSec` covers the largest job timeout.
2. `uv run herdr-routines run <job> --dry-run` — eyeball the exact `herdr` argv it would run.
3. `uv run herdr-routines run <job>` from an interactive shell, watching the pane in Herdr.
4. `systemd-run --user --scope uv run herdr-routines run <job>` — proves the *systemd* user
   environment specifically (no inherited `HERDR_*`, `herdr`/`uv` resolvable on `PATH`, socket
   reachable) rather than your interactive shell's.
5. Point one throwaway job at a `*/5 * * * *` cron with a short prompt, enable the timer, and
   confirm one fire produces exactly one `running` + one terminal record in
   `herdr-routines history <job>`, one report file under the reports dir, and one Herdr
   notification.
6. `systemctl --user stop herdr-server` mid-run once, and confirm the tick's service unit ends
   `failed` (visible via `systemctl --user status herdr-routines.service`) rather than hanging
   forever — this is what `TimeoutStartSec` being finite (not `infinity`) buys you.

Step 5 was performed live against `herdr 0.8.2` on this laptop while building this tool: a
`root`-mode job with a trivial "write PONG to `$ROUTINE_REPORT`" prompt produced
`registered → missed → running → done`, a real report file, and a live `rt-<job>` agent —
confirming the whole loop end to end. Repeat it on the Pi once Herdr is installed there (tracked
separately in this operator's own deployment notes, outside this repo) — that step is separate
from, and not required for, this tool's own correctness.
