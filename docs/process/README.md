# Process

Single-tier issue tracking for herdr-routines, adapted from fitted's
[three-tier ADR/PDR/Issue system](../../../fitted/docs/process/README.md) but
flattened: this is a single-user CLI tool, and `ROADMAP.md` already carries
the "why" narrative and horizon grouping (Now/Next/Later/Parking Lot) that
ADRs and PDRs would otherwise own. One tier is enough.

## Issues

One `.md` file per curated roadmap item, in
[`docs/process/issues/`](issues/), `NNN-short-description.md`, numbered
sequentially (never reused).

Frontmatter:

```yaml
---
id: "001"
title: One-line summary
status: open | blocked | in-progress | done
priority: high | medium | low
area: pipeline | cli | plugin | config | infra
gate: what has to be true before/while this is worked on (optional — omit if unconditionally ready)
---
```

`gate` is the one field this convention adds beyond fitted's issue template —
it's how `ROADMAP.md` already reasons about readiness ("Now" = no gate,
"Next"/"Later" = gate not yet clear), so it's carried into the file instead of
being lost when an item graduates out of prose.

Query: `grep -l "status: open" docs/process/issues/*.md`

## Scope

Only `ROADMAP.md`'s **Now** horizon is curated into issues — items with no
gate, ready to build. Next/Later/Parking Lot items are gated on evidence
(mostly "a few weeks of real Pi runs") that hasn't arrived yet; converting
them to files now would be structure for ideas that aren't designed yet.
Promote an item to an issue file when it graduates to Now.

## Relationship to `ROADMAP.md`

`ROADMAP.md` keeps the one-liner + horizon grouping (light-index pattern,
same as `~/projects/PENDING.md`); the full narrative — description, update
log, links to design docs — lives in the issue file. This split exists so a
future automated selector (see `ROADMAP.md` Later § "Autonomous task
selection for the pipeline") has a flat, frontmatter-queryable list to pick
from instead of parsing prose.
