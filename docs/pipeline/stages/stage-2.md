# Stage 2 — Spec review + update (adds acceptance criteria)

Review `$WT/docs/pipeline/runs/$RUN_ID/spec.md` v1. Produce spec v2 with an added `## Acceptance criteria` section: numbered items, each ends `Test: <name>` (exact test name). Also add `## Changelog v1→v2` inside the same file describing changes, and ensure `blocking`/`non-blocking` and `confidence:` tiers are present. Commit: `git -C "$WT" add docs/pipeline/runs/$RUN_ID/spec.md && git commit -m "spec: v2 acceptance for $RUN_ID"`.
