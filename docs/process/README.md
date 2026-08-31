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

## Runbooks

Operational how-tos that aren't curated roadmap features live beside this file
as `*.md` (not under `issues/`, which is frontmatter-driven for `pick-feature`):

- [`pi-update-runbook.md`](pi-update-runbook.md) — what to do when a
  herdr-routines PR merges: fast-forward the Pi runner checkout, migrate config
  if the schema changed, validate.

## Audits / design reviews

Independent design-review records (e.g. a spec audited by a separate agent before
it goes to `pick-feature`) live in [`audits/`](audits/) as
`audit-<reviewer>-v<N>.md`, one file per revision — keeping the `audit-*` naming
and `<area>/audits/` location already used by `docs/pipeline/audits/`
(`audit-muse.md`, `audit-xpreview.md`).

These are **not** placed under [`issues/`](issues/): that directory is
frontmatter-driven for `herdr-routines pick-feature` (`pick_feature.py` globs
`*.md` and parses `id/title/status/priority/area`), so any stray file there —
an audit, a runbook, a scratch note — would break selection (or worse, be picked
as an issue). Keep audits, runbooks, and prose out of `issues/`.

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
