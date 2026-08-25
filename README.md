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

## Architecture

![Layered architecture: an imperative shell of cli.py, tick.py, and runner.py above a pure core of config.py, schedule.py, and history.py, with runner.py calling the herdr.py adapter, which forks to either a real subprocess or a FakeRunner used in tests.](docs/diagrams/architecture-layers.svg)

A core with no subprocess/network I/O (`config.py` / `schedule.py` / `history.py` — the latter
two do read/write the YAML config and the JSONL history file, but neither shells out or talks to
Herdr) sits under an imperative shell
(`cli.py` / `tick.py` / `runner.py`); `herdr.py` is the one adapter that shells out to `herdr`,
behind a seam that's faked in tests. See
[`docs/plan-v1.md#diagrams`](docs/plan-v1.md#diagrams) for the full-run sequence diagram and a
breakdown of exactly what a Herdr API change vs. an agent-CLI change would touch.

## Status

v1 implemented and verified end to end against a real Herdr session on the laptop (see
`docs/plan-v1.md` build-order record). Not yet deployed to the Pi — that's gated on installing
Herdr there, tracked separately in this operator's own deployment notes (outside this repo).

## Usage

```sh
uv sync
cp deploy/jobs.example.yaml ~/.config/herdr-routines/jobs.yaml   # then edit for your own jobs
uv run herdr-routines validate
uv run herdr-routines run <job> --dry-run   # eyeball the herdr argv before trusting it
uv run herdr-routines status
```

See [`deploy/README.md`](deploy/README.md) for the systemd units and deployment smoke checklist.
See [`troubleshooting-log.md`](troubleshooting-log.md) for real incidents hit running this on the
Pi and their root causes/fixes.

## Development

```sh
uv sync --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
