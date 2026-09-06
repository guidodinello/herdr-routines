---
id: "034"
title: "Pipeline never gates on CI: a red PR passes every stage"
status: done
priority: high
area: pipeline
---

## Description

The overnight pipeline can open a PR whose CI is failing, pass all six gates, and
write `## Outcome: ok`. It did exactly that on 2026-09-04 with PR #81, which has
been sitting red ever since.

`Lint Python` fails on that PR:

```
$ uv run ruff format --check .
4 files would be reformatted, 37 files already formatted
```

`origin/main` is clean (`39 files already formatted`), so the pipeline introduced
the breakage.

Nothing in the pipeline can see this:

- **Gate 3** runs `uv run pytest -q` and nothing else. Tests were green (428
  passed) — they were never the failing check.
- **Gate 4** checks that the PR exists on the right branch and that the issue
  `status: done` flip is committed.
- **No stage anywhere in `docs/pipeline/orchestrator-prompt.md` calls
  `gh pr checks`** or otherwise reads `statusCheckRollup`.

The run report proudly cites "428 tests green" as evidence of health. It is true
and irrelevant: the pipeline is structurally blind to every CI job that isn't
pytest, which on this repo means both ruff invocations and the typechecker.

Note that `.github/workflows/ci.yml` runs **both** `ruff format --check .` **and**
`ruff check .`. A local `pytest`-only gate cannot stand in for CI.

## Design

**Decided 2026-09-06: the gate moves into code** (see issue 035 for the shared
rationale). Both this issue and 035 create `src/herdr_routines/gates.py` and are
therefore **one PR, not two** — they are no longer independently parallelizable.

Two changes, cheapest first:

1. **Widen gate 3** to the same three commands CI runs, so the implementer fixes
   its own lint before ever opening a PR:

   ```sh
   uv run ruff format --check . && uv run ruff check . && uv run pytest -q
   ```

2. **Add a real CI gate after stage 4** (the authoritative one — gate 3 only
   proves the local tree is clean, not that CI agrees). After the PR is open,
   poll until checks are non-pending and require success:

   ```sh
   gh pr checks "$PR" --watch --fail-fast
   # or, poll-and-inspect:
   gh pr view "$PR" --json statusCheckRollup \
     | jq -e '[.statusCheckRollup[] | select(.conclusion=="FAILURE")] | length == 0'
   ```

   A FAILURE conclusion must route into stage 6 as a must-fix item, alongside the
   review threads — not abort the run, since a lint slip is exactly the kind of
   thing stage 6 exists to clean up.

Bound the poll (CI on this repo finishes in well under 2 minutes; a 10-minute cap
with a `SKIPPED`/`NEUTRAL`-tolerant filter is plenty) so a stuck check can't eat
the orchestrator's deadline.

### Where the logic lives

The CI gate ships as `herdr-routines gate --stage ci --pr <n>` backed by
`gates.py`, and the prompt calls it rather than inlining jq. The acceptance
criteria below are only implementable that way: `test_ci_gate_tolerates_skipped_checks`
needs something to import. Gate 3's lint widening stays as prompt text — it is a
command the implementer runs, not a verdict the orchestrator computes.

## Acceptance criteria

1. Gate 3 fails when `ruff format --check .` or `ruff check .` fails, even with a
   green pytest. Test: `test_gate3_fails_on_lint_error`
2. A PR whose CI concludes FAILURE does not reach `## Outcome: ok`. Test:
   `test_pipeline_ci_failure_routes_to_stage6`
3. `SKIPPED` and `NEUTRAL` check conclusions are not treated as failures. Test:
   `test_ci_gate_tolerates_skipped_checks`
