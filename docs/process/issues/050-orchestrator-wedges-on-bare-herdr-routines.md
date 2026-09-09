---
id: "050"
title: "orchestrator prompt calls a bare `herdr-routines` that isn't on PATH — it then probes ~/.local/bin and wedges"
status: done
priority: high
area: pipeline
---

## Description

The 20260909T050000Z nightly run produced no PR. `tick` dispatched it and issue
037's fail-fast path worked exactly as designed — a `blocked` settle at second
~30, a captured tail, an `## Outcome: failed` stub, reconciled on the next tick.
What it was stuck on:

`~/.local/state/herdr-routines/reports/20260909T050000Z.tail.txt`:

```
$ ls -la ~/.local/bin/; echo "===PATH==="; echo $PATH
...
herdr-routines isn't in ~/.local/bin. Let me check how it's invoked - it may
be a uv tool, or accessed via uv run. Let me look at the pipeline-launch
scripts to see how they invoke things.
  ⠙ Read ~/.local/bin/pipeline-launch-nightly.sh
  △ Permission required
    ← Access external directory ~/.local/bin
```

Chain:

1. **`docs/pipeline/orchestrator-prompt.md` invoked `herdr-routines` bare** — the
   prerequisite `herdr-routines sync-repo`, `pick-feature`, and both
   `herdr-routines gate` calls. But `herdr-routines` is **not installed as a
   standalone binary** on the pipeline host: no `uv tool install`, nothing in
   `~/.local/bin`, not on `PATH`. Every `deploy/systemd/*.service` unit runs it as
   `uv run herdr-routines …`; the prompt was the one place that didn't.
2. The bare call fails command-not-found. The orchestrator (opencode
   `big-pickle`) tries to self-heal — `ls ~/.local/bin`, then `Read
   ~/.local/bin/pipeline-launch-nightly.sh`.
3. `~/.local/bin` is not on the opencode `permission.external_directory` allowlist
   → `blocked` prompt → nobody to answer at 02:00 → run dead.

Compounding: `~/.local/bin` still held **five superseded standalone launcher
scripts** (`pipeline-launch-nightly.sh`, `-gc.sh`, `-plugin.sh`,
`-panelifecycle.sh`, `-statuscli.sh`) from before issue 026 folded the launcher
into the repo (`scripts/pipeline-launch.sh`). They are the honeypot the
orchestrator reached for.

Note the older runs (#106, #112) opened PRs despite the same bare call — the
orchestrator worked around the missing command silently on those nights. #112 was
the first run on `big-pickle` (after #110), and #109 also carries the "was
`opencode/muse-spark`" note; model behaviour on the command-not-found path is not
deterministic. The prompt was wrong either way.

## Fix

1. **`orchestrator-prompt.md`: `herdr-routines …` → `uv run herdr-routines …`**
   everywhere (prerequisite sync, `pick-feature`, Gate CI, Gate 6), plus an
   explicit note in the Prerequisite section that it is not a standalone binary.
2. **`setup.md` §3: remove the stale `~/.local/bin/pipeline-launch-*.sh`
   predecessors** as a per-host step — the reason the orchestrator looked at
   `~/.local/bin` at all.
3. **Do not** widen the allowlist to `~/.local/bin/**` — `external_directory` has
   no read-only mode, and a self-editing pipeline should not also be able to
   overwrite `herdr`/`gh`/`uv`.
4. **`deploy/opencode.pipeline.json`: track the opencode permission allowlist**
   in one committed file, referenced by `setup.md` §4 and `design.md`, instead of
   an inline JSON block that already drifted (`~/.cache/uv/**` was live on the Pi
   and missing from the docs).

## Acceptance criteria

1. The orchestrator prompt invokes `herdr-routines` only via `uv run`. Test: `test_prompt_invokes_herdr_routines_via_uv_run`
2. The prompt states `herdr-routines` is not a standalone binary on pipeline hosts. Test: `test_prompt_notes_herdr_routines_not_a_binary`
3. `deploy/opencode.pipeline.json` is valid JSON and allowlists the dirs the pipeline needs. Test: `test_opencode_pipeline_permission_file`
4. `setup.md` points at the tracked permission file and does not tell operators to add `~/.local/bin`. Test: `test_setup_references_tracked_opencode_file`
