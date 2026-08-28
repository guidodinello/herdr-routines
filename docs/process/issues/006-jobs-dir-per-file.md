---
id: "006"
title: "Split jobs.yaml into one file per job (jobs.d/<name>.yaml)"
status: open
priority: medium
area: config
---

## Description

Replace the monolithic `jobs.yaml` list with a `jobs.d/` directory discovered
by listing, one `jobs.d/<name>.yaml` per job, the filename doubling as (or
validated against) the job's `name`. Shared fields (today's top-level
`defaults:` block — `agent_kind`, `workspace`, `timezone`, etc.) move to a
sibling `defaults.yaml` that the loader merges *under* each job file's own
fields.

Why (from `ROADMAP.md` Next §): editing a single job today means hand-editing
a block inside a bigger file — fragile for scripted edits (flipping an
`enabled` flag needs a regex/line-number substitution rather than a plain
file write), disable-by-rename or `git mv` is more legible, and a syntax
error in one job's file can't break parsing of the others.

Real work, not a config reshuffle: `src/herdr_routines/config.py` needs the
directory-discovery + merge logic and filename/`name` consistency
validation; `validate` / `status` / `history` / `scheduled` / `ps` must stop
assuming a single file path.

## Acceptance

- Loader discovers every `jobs.d/*.yaml`, merges `defaults.yaml` under each
  job's own fields, and errors clearly when a file's `name` disagrees with
  its filename.
- A YAML syntax error in one job file surfaces that file by name and does not
  prevent the other jobs from loading.
- `validate`, `scheduled`, `ps`, and `history` all work against the directory
  layout with no single-file-path assumption left.
- Migration path for the existing `jobs.yaml` is documented (or the loader
  accepts both during a transition).

## Log

- **2026-08-27**: curated from `ROADMAP.md` Next §. The scripted-edit
  friction was hit again this day trimming `fitted-pr-review-4`/`-5` to
  `enabled: false` by line-number `sed`.
