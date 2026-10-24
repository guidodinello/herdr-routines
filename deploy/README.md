# Deploying herdr-routines

Five systemd **user** unit pairs drive this tool — no daemon of our own, no root required. See
[`../docs/plan-v1.md`](../docs/plan-v1.md) §3 for the full rationale.

## Install

`herdr-routines.service`'s `WorkingDirectory` and `ExecStart` hardcode this checkout's path and
this host's `uv` location (`%h/projects/herdr-routines`, `%h/.local/bin/uv`) — edit both before
copying the unit to a host with a different checkout path or `uv` install location.

```sh
mkdir -p ~/.config/systemd/user
cp systemd/herdr-server.service systemd/herdr-routines.timer systemd/herdr-routines.service \
   systemd/herdr-routines-watchdog.timer systemd/herdr-routines-watchdog.service \
   systemd/herdr-routines-digest.timer systemd/herdr-routines-digest.service \
   systemd/herdr-routines-update.timer systemd/herdr-routines-update.service \
   systemd/herdr-routines-tmp-sweep.timer systemd/herdr-routines-tmp-sweep.service \
   ~/.config/systemd/user/
systemctl --user daemon-reload
```

`herdr-routines-watchdog.{timer,service}` is a second, independent unit pair (own
`ExecStart=... herdr-routines pipeline-watchdog`, own 15-min `OnCalendar`) — **not** a
`jobs.d/` entry. `tick`'s job model (`herdr-routines.timer` above) only ever dispatches an
agent prompt per job; the pipeline stall watchdog (issue 031) spawns no agent and issues no
prompt, so it gets its own timer the same way `tick` itself does, rather than being forced
into a job shape it doesn't fit.

`herdr-routines-digest.{timer,service}` is a third, independent unit pair for the same
reason (issue 010): the daily digest reads existing `history.jsonl` + the reports dir and
posts one summary notification — it spawns no agent, dispatches no job, and needs no new
per-job config field, so it gets its own once-a-morning timer rather than a `jobs.d/` entry.
Edit `--timezone` in the unit's `ExecStart` (default in the example is
`America/Montevideo`) to your own; drop `--notify` to only print to the service log.

`herdr-routines-update.{timer,service}` is a fourth unit pair, and the one that keeps this
checkout current (issue 053). It runs once a night at 21:32:30 UTC — the unit's
`OnCalendar` pins `UTC` explicitly, so it does not follow the host's
`America/Montevideo`, where that hour is 18:32 — half-way between two 5-minute
ticks, because at 21:30 it collided with one and deferred. `ExecStart` is
`uv run herdr-routines self-update`, which fast-forwards **this** checkout to
`origin/main` — but only onto the exact commit the CI gate was asked about, and only
after the new code has validated this host's live config *out of process*; if that
validate fails the checkout goes back to where it was with `git reset --keep` and a
notification. It reports (and applies) nothing under `deploy/` — unit changes are
reported in the notification and installed by hand, per this file. Deferrals (a pipeline run in flight, a red or pending CI, a commit already rolled
back) are silent and exit 0. A held tick lock is retried for up to 5 minutes first;
if it is *still* held the run defers (exit 0) with a WARNING and a notification,
since that is a night lost; refusals (dirty tree, not on `main`, detached HEAD, a
checkout still sitting on a commit that failed validation) notify and exit 1.
Preview it any time without touching the checkout:

```sh
uv run herdr-routines self-update --dry-run
```

`herdr-routines-tmp-sweep.{timer,service}` is a host-hygiene pair, not part of the tool
proper. opencode extracts a ~14 MiB native library to `/tmp/.<hex>-<hex>.so` on every
launch, and agents killed mid-run leave theirs behind; the stock `systemd-tmpfiles` 10-day
window is far too slow when `/tmp` is a small RAM-backed tmpfs (the Pi's filled on
2026-10-20). The unit deletes this user's files of exactly that name shape once they are
two hours old, every hour. Skip it on a host whose `/tmp` is on disk and roomy.

Note the unit updates a hardcoded path, not its own `WorkingDirectory`: `ExecStart`
passes no `--path`, so the checkout acted on is the flag's default,
`~/projects/herdr-routines`. That happens to equal the unit's `WorkingDirectory`, but
by convention rather than by derivation — if you move one, pass `--path` explicitly in
the other. The point of the fixed default is that it does *not* follow the cwd, so a
hand-run `self-update` from the `~/.local/state/herdr-routines/repos/` clone updates the
runner and not that one (issue 016).

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

**The overnight feature-pipeline is one of these jobs, not a separate launcher** (issue
026): copy [`jobs.d/feature-pipeline.yaml`](jobs.d/feature-pipeline.yaml) alongside your
other `jobs.d/` entries. Its opencode agents also need the permission allowlist in
[`opencode.pipeline.json`](opencode.pipeline.json) merged into the host's
`~/.config/opencode/opencode.json` — see [`../docs/pipeline/setup.md`](../docs/pipeline/setup.md) §4. `tick` dispatches a `kind: pipeline` job as a detached
`systemd-run --user` unit (`scripts/pipeline-launch.sh`) and returns immediately — it does
**not** need its own timer the way the pre-026 launcher did. Migrating an existing
standalone `pipeline-nightly.timer`/`.service` pair: disable and remove them
(`systemctl --user disable --now pipeline-nightly.timer`) once the `jobs.d/` entry is
validated — running both would double-dispatch the same nightly run.

Then:

```sh
uv sync
uv run herdr-routines validate
uv run herdr-routines tmp-hygiene --dry-run  # optional: preview /tmp cleanup
uv run herdr-routines prune reports --dry-run  # optional: preview state-dir cleanup (issue 021)
systemctl --user enable --now herdr-server.service
systemctl --user enable --now herdr-routines.timer
systemctl --user enable --now herdr-routines-watchdog.timer
systemctl --user enable --now herdr-routines-digest.timer
systemctl --user enable --now herdr-routines-update.timer
```

**Note on /tmp hygiene (issue 027):** every `tick` runs one age-based `/tmp` reap
before it dispatches any job, under the tick lock. It removes top-level entries
matching `.3cdc*` (files), `pytest-of-*`, `opencode` and `opencode-*` whose newest mtime
anywhere in the tree is older than `max_age_s`. A reap error is logged and the tick
carries on; it never blocks or fails dispatch. Configure it with `tmp_hygiene:` under
`defaults:` (`defaults.yaml` in a `jobs.d/` layout):

```yaml
tmp_hygiene:
  enabled: true    # false turns the tick reap off
  max_age_s: 3600  # default: 1 hour
  tmp_dir: /tmp    # default
```

No block means the reap runs with these defaults; only `enabled: false` turns it off.
A per-job `tmp_hygiene:` still validates but is ignored, because the reap runs once per
tick, not once per job. `herdr-routines tmp-hygiene [--dry-run]` runs the same reap by
hand. On agent start failure, `df -h /tmp` is logged to the report tail and the
`RunOutcome.diagnosis` field distinguishes `tmp_full` from quota `blocked`.

The tick reap does not replace the `herdr-routines-tmp-sweep` timer described above.
Keep both wherever you run that timer. The timer deletes opencode's
`/tmp/.<16 hex>-<8 hex>.so` libraries, the files that filled the Pi on 2026-10-20, and
the tick reap's `.3cdc*` glob does not match that name shape. The timer also keeps
running when the tick timer is stopped or wedged.

**Note on state-dir retention (issue 021):** nothing is collected unless you ask.
`history.jsonl` rotation is opt-in via the `retention:` block (a rename at the end of
every tick — it can never lose a record); reports and rolled history are only deleted by
`herdr-routines prune`, which requires `--yes` and spares anything belonging to an
in-flight pipeline run. Copy [`jobs.d/retention.yaml`](jobs.d/retention.yaml) or the
commented block in [`jobs.example.yaml`](jobs.example.yaml) to set thresholds, and put
`prune` on a timer only once you have read its `--dry-run` output — see
[`../docs/plan-v1.md`](../docs/plan-v1.md#retention).

**Note on trusted review authors:** unattended agents only act on feedback from
trusted authors. The `pr_health` auto-fix only considers PRs opened (from a branch in
the repo itself, never a fork) and unresolved review threads started by a trusted login,
and pipeline stage 7 is handed only the review threads and summaries those logins wrote.
The same set scopes the rest: a `github_event` `issues:opened` check without
`match.author` only fires on issues a trusted login opened (`prs:labels` and `ci:failed`
only fire on PRs from a branch in the repo itself), a `kind: review` job only counts a
review marker a trusted login posted, and pipeline gate 6 only counts tier labels from
trusted logins. The trusted set is the top-level `trusted_review_authors:` list plus the authenticated
`gh` user, resolved at runtime and never written into config. When the key is absent the
list is `[copilot-pull-request-reviewer]` (Copilot's PR reviewer); a configured list
replaces that default, and `[]` means "only the gh user". In a `jobs.d/` layout it lives
in its own `trusted_review_authors.yaml` whose whole document is the list. See the
commented block in [`jobs.example.yaml`](jobs.example.yaml).

## Cold-start spawn from Telegram (issue 024)

The Telegram bot (issue 023) steers *existing* panes by replying to a notification. The cold-start path complements it: a `/run <alias>` or `/start <alias>`
message, with no notification to reply to, starts an agent against the mapped checkout (in a
fresh `auto/spawn-<alias>-<ts>` worktree by default, laid out exactly like a `workspace:
worktree` job run) and replies with the pane handle the reply-to-steer flow then uses.

Configure it with a top-level `telegram_spawn:` block in `jobs.yaml` (or a `telegram_spawn.yaml`
sibling in a `jobs.d/` layout — see the commented block in
[`jobs.example.yaml`](jobs.example.yaml)):

```yaml
telegram_spawn:
  allowed_chat_id: 123456789   # REQUIRED: the only chat that may spawn, a private 1:1 chat.
                               # Empty or a group/channel id = validate fails.
  aliases:
    - alias: nightly           # the token after /run or /start
      repo: /home/guido/projects/fitted
      agent_kind: opencode     # optional, default claude
      workspace: worktree      # optional, default worktree (a fresh branch's worktree;
                               # `root` runs in the checkout itself)
      base: main               # optional, default main: the branch a worktree spawn starts from
      model: opencode/big-pickle # optional: default model for agent_kind (claude, codex, opencode)
```

The message can pick the harness and model: `/run <alias> [harness] [model]`.

| Message | Starts |
|---|---|
| `/run nightly` | the alias's `agent_kind`, with its `model` if set |
| `/run nightly opus` | the alias's `agent_kind` with model `opus` |
| `/run nightly claude` | Claude on its default model (the alias's `model` belongs to its own harness) |
| `/run nightly opencode opencode/big-pickle` | opencode with that model |

The harness must be a known `agent_kind`. A model works only with claude, codex and opencode, and
must start with a letter or digit, so it never reads as a flag. A malformed `/run` from the allowed
chat gets a usage reply listing the aliases; any other chat gets the generic refusal.

`uv run herdr-routines validate` rejects an empty `allowed_chat_id`, a non-private one, a
duplicate alias, and an alias whose `repo` does not exist or is not a git checkout — at load
time, not at spawn time on someone's phone. Only the chat id reaches the entrypoint, not the
sender, so `allowed_chat_id` must be a private 1:1 chat (a positive id): a group, supergroup or
channel id is negative ([Telegram bot API ids](https://core.telegram.org/api/bots/ids)) and is
rejected, because every member of that chat could otherwise spawn.

**Wiring the inbound message.** The bot that polls Telegram must call the event entrypoint
once per `/run` or `/start` message and send back whatever it prints. Telegram allows one
`getUpdates` consumer per bot token, so this has to be the bot already running, not a second
poller. On the Pi that bot is `relay/herdr_telegram.py` from the
[`guidodinello/herdr-remote`](https://github.com/guidodinello/herdr-remote) fork (branch
`pi-deploy`, unit `herdr-telegram.service`). It runs the hook when `HERDR_TG_SPAWN_CMD` is set
in `~/.config/herdr-remote/config.env`:

```sh
HERDR_TG_SPAWN_CMD=/home/guido/projects/herdr-routines/scripts/herdr-spawn-from-telegram.sh
HERDR_ROUTINES_BIN=/home/guido/projects/herdr-routines/.venv/bin/herdr-routines
```

With it set, `/run <alias>` and `/start <alias>` spawn, and a bare `/start` still shows the
bot's dashboard. Set `allowed_chat_id` to the same chat as the bot's `HERDR_TG_CHAT_ID`, which
must be your private chat with the bot (not a group). The
entrypoint and shim, for any other caller:

```sh
uv run herdr-routines spawn-from-telegram --chat-id "$CHAT_ID" --text "$MESSAGE_TEXT"
# or the PATH-agnostic shim (also accepts TELEGRAM_CHAT_ID / TELEGRAM_MESSAGE_TEXT):
scripts/herdr-spawn-from-telegram.sh --chat-id "$CHAT_ID" --text "$MESSAGE_TEXT"
```

The handler is event-only: it writes no `tick.lock`, no history terminal state, and owns no
timer — the systemd `herdr-routines.timer` stays the sole scheduler. Secrets (the bot token)
stay in the bridge's own secrets file; only the non-secret `allowed_chat_id` and alias map live
in the host-specific jobs config, which is not committed.

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
