# Runbook: updating the Pi after a herdr-routines PR merges

The herdr-routines repo has **two** checkouts on the Pi. Updating the wrong one
is the classic mistake, so read this first:

| Path | Role | Who updates it |
| ---- | ---- | -------------- |
| `~/projects/herdr-routines` | **Runner** — the installed executable the systemd service runs (`uv run herdr-routines tick`) | **Manual** — this runbook |
| `~/.local/state/herdr-routines/repos/herdr-routines` | **Work/target repo** — a plain clone used when a job works *on* this project (e.g. `babysit-prs`) | Automatic — job runner clone lifecycle (issue 016) |

**You almost always want the runner checkout** (`~/projects/herdr-routines`).
The service's `WorkingDirectory` is that path, so that's the code that actually
runs each tick. The `repos/` checkout only matters when a routine targets the
herdr-routines project itself.

The service runs a fresh oneshot each tick (every 5 min), so it picks up any
new code/config on the *next* tick — no restart or daemon-reload needed after a
merge.

## When to run

- A PR touching `src/herdr_routines/`, `deploy/`, or the config schema merges to
  `main` with **green CI**.
- Docs-only PRs don't need the runner updated (but are harmless to pull).

**Normally you don't run this by hand at all** (issue 053):
`herdr-routines-update.timer` fires `herdr-routines self-update` every night at
21:30 UTC (the unit's `OnCalendar` pins `UTC`, so this is 18:30 on the Pi's
`America/Montevideo`), which does steps 0-4 below automatically — CI check,
fast-forward onto the gated commit, validate, `git reset --keep` rollback on
failure — and defers silently when the tick lock is held, a `kind: pipeline` run is
in flight, CI is not green, or the commit has already been rolled back once. The
steps below are still the procedure for the two cases it cannot cover: a **config
schema migration** (below), and a host it has *refused* (dirty tree, not on
`main`, detached HEAD, an unpushed hand-hotfix, or a checkout still sitting on a
commit that failed its own validation) — both of which notify and explain
themselves. Check what it did with:

```sh
ssh $HOST "systemctl --user status herdr-routines-update.service"
ssh $HOST "journalctl --user -u herdr-routines-update.service -n 50 --no-pager"
ssh $HOST "cd ~/projects/herdr-routines && uv run herdr-routines self-update --dry-run"
```

`--dry-run` fetches, checks CI and prints the `old..new` range and deploy diff, then
stops before the fast-forward. It reports `deferred` (exit 0) — including under a held
tick lock, which is correct and not a failure.

## Steps

```sh
HOST=pi   # ssh alias; key auth via ~/.ssh/config

# 0. Verify CI is green on the merge commit (never ship a red runner)
gh run list --commit <merge-oid> --limit 3

# 1. Look at the live runner checkout
ssh $HOST "cd ~/projects/herdr-routines && git log --oneline -1 && git status --porcelain"

# 2. Fast-forward the RUNNER checkout (not the repos/ one)
ssh $HOST "cd ~/projects/herdr-routines && git fetch origin && git pull --ff-only origin main"

# 3. If the PR changed the config schema, migrate ~/.config/herdr-routines/jobs.yaml
#    (see "Config migrations" below). `uv` is NOT on non-interactive PATH —
#    use the full path.

# 4. Validate — ok: N job(s) valid is the pass signal
ssh $HOST "cd ~/projects/herdr-routines && ~/.local/bin/uv run herdr-routines validate"
```

Back up the live config before migrating:

```sh
ssh $HOST "cp ~/.config/herdr-routines/jobs.yaml ~/.config/herdr-routines/jobs.yaml.bak"
```

## Config migrations

The one non-trivial part is when a merged PR renames/reshapes the job config
schema. The old config won't error loudly — it may be silently **ignored**,
which drops the job. Check `deploy/jobs.example.yaml` at the new HEAD for the
canonical shape and diff against the live `~/.config/herdr-routines/jobs.yaml`.

Validate locally against the new code *before* deploying by pulling the merge
into a throwaway worktree and loading the edited config through it.

Example (PR #56, 2026-08-30): `auto_fix:` container removed in favor of
top-level `checks`/`target`/`max_workers_per_tick`/`max_attempts_per_target`.
Baby-prs became:

```yaml
max_workers_per_tick: 3
max_attempts_per_target: 3
checks:
  - pr_health:   # mapping form — a bare `- pr_health` string fails validation
```

Example (issue 049): jobs with `checks:` now require `kind: gated` in the same
job file. Add `kind: gated` to `~/.config/herdr-routines/jobs.d/babysit-prs.yaml`
(or the equivalent entry in `jobs.yaml`) right after the `base:` key. Without
this, `validate` / `tick` raise a ConfigError containing `kind: gated` and
`checks`.

## Notes / gotchas

- `ssh pi` (alias), not `guido@raspberrypi.local` (that returns publickey denied).
- `uv` is not on PATH in non-interactive ssh — use `~/.local/bin/uv`.
- `herdr-routines validate` prints pre-existing `$ROUTINE_REPORT` warnings for
  other jobs; `ok: N job(s) valid` is the pass line.
- **Issue closing is carried by the PR, not done manually after merge.** The
  implementing PR commits its issue's `docs/process/issues/<file>` `status:` →
  `done`, so the flip lands on `main` atomically at merge. After pulling a merged
  PR, **do not** hand-flip any issue status — it's already in main via the PR, and
  you shouldn't leave the issue `open`/`in-progress` expecting a manual close.
- The runner fast-forward is automated, with the manual steps above as its
  fallback: `herdr-routines-update.timer` (issue 053) does the green-CI gate, the
  fast-forward and the out-of-process validate on its own, and rolls back with
  `git reset --keep` if the new code will not load the host's config. This is the
  one carve-out in the `ROADMAP.md` Parking-lot release/update strategy — herdr-
  routines' own changes already flow through PR + green CI, so the Pi may
  fast-forward *itself*. Plugins, the herdr CLI and `jobs.yaml` stay explicit and
  are never auto-mutated, and `deploy/` changes are reported by the notification
  rather than applied. Run the manual steps when the timer refuses the host (it
  says why) or when a merge needs a config migration before it can validate.
