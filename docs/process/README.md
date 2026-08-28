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
it's how `ROADMAP.md` reasoned about readiness by horizon; carried into the
file so it isn't lost when an item graduates out of prose. On a `blocked`
issue it names the decision that must land first; omit it once the item is
`open`.

Query: `grep -l "status: open" docs/process/issues/*.md`

## Scope

Every `ROADMAP.md` item across all horizons is curated into an issue file
(as of the 2026-08-27 backfill). `status` carries the readiness that the
horizon used to imply:

- **`open`** — designed enough to build; `pick-feature` selects from these.
- **`blocked`** — gated on an *unmade design decision* (not elapsed time);
  the `gate` field says what. Not selectable until promoted to `open`.
- **`in-progress`** / **`done`** — as usual.

The "a few weeks of real Pi runs" gate that most Next/Later items shared was
declared met on 2026-08-27 (several days of clean nightly runs); those items
were promoted to `open` rather than left as prose.

## Relationship to `ROADMAP.md`

`ROADMAP.md` keeps the one-liner + horizon grouping (light-index pattern,
same as `~/projects/PENDING.md`); the full narrative — description, update
log, links to design docs — lives in the issue file. This split exists so the
automated selector (`herdr-routines pick-feature`, issue `013`) has a flat,
frontmatter-queryable list to pick from instead of parsing prose.
