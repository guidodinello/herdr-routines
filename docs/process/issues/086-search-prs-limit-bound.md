---
id: "086"
title: "Enforce GitHub's 100-result ceiling on search_prs limit"
status: open
priority: low
area: infra
---

## Description

`auto_fix.search_prs(modes, limit: int = 100)` and `review_scan`'s `limit: int = 100`
(~:258) pass `limit` to a GitHub search. The bound exists only in prose: a comment
at auto_fix ~:883 calls 100 "GitHub's own ceiling". A larger value would fail at
GitHub, not here. Today every caller uses the default, so this is latent.

Options: a module constant plus a `ValueError` guard, or remove the parameter and
always use the constant.

## Acceptance

- The 100 ceiling is defined once, in code, not only in a comment.
- A value above it fails locally with a clear error, or can no longer be passed.
- `scripts/ci-check.sh` is green.

## Log

- **2026-10-26**: filed from the type-health audit's INVESTIGATE list.
