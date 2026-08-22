# herdr-routines

A small cron-style scheduler for [Herdr](https://herdr.dev). It runs as a `systemd` user timer,
reads a YAML job list, decides which jobs are due (with catch-up-after-downtime handling), and
drives the `herdr` CLI to spawn a pane, start a coding agent (Claude Code or OpenCode), and send
it one prompt — unattended.

Herdr has no built-in scheduler, and Claude Routines doesn't cover OpenCode or Herdr-native
workflows. This fills that gap for a single always-on host (a Raspberry Pi, in the intended
deployment) without adding a resident daemon of its own.

See [`docs/plan-v1.md`](docs/plan-v1.md) for the full design: config schema, catch-up semantics,
run history format, and test strategy.

## Status

v1 implemented and verified end to end against a real Herdr session on the laptop (see
`docs/plan-v1.md` build-order record). Not yet deployed to the Pi — that's gated on installing
Herdr there (`~/projects/raspberrypi/roadmap.md` §3).

## Usage

```sh
uv sync
cp deploy/jobs.example.yaml ~/.config/herdr-routines/jobs.yaml   # then edit for your own jobs
uv run herdr-routines validate
uv run herdr-routines run <job> --dry-run   # eyeball the herdr argv before trusting it
uv run herdr-routines status
```

See [`deploy/README.md`](deploy/README.md) for the systemd units and deployment smoke checklist.

## Development

```sh
uv sync --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
