---
id: "048"
title: "gc's orphan scan is hardcoded to herdr-routines' worktree dir, so --repo <other> can delete another project's worktree"
status: done
priority: high
area: cli
---

## Description

Issue 045's orphan sweep derives its scan directory from a hardcoded default, not
from the repository `--repo` points at:

```python
orphans = find_orphans(
    worktrees_root or _default_pipeline_worktrees_root(),   # ~/.herdr/worktrees/herdr-routines
    list(all_worktree_paths),                                # ...but registered paths come from --repo
)
```

`_default_pipeline_worktrees_root()` returns `~/.herdr/worktrees/herdr-routines`
unconditionally. The *registered* paths it checks against come from the target
repo's `git worktree list`. So when `gc` is pointed at any other repository, it
scans **herdr-routines'** worktree directory and compares it against **the other
repo's** registrations — where, of course, nothing matches.

Every herdr-routines worktree therefore reads as an orphan.

Observed on the Pi, 2026-09-06, running `gc --dry-run --repo .../repos/fitted
--base development`:

```
ORPHANS (present on disk, absent from `git worktree list`):
  non-empty directory (unregistered; left alone) /home/guido/.herdr/worktrees/herdr-routines/auto-pipeline-20260903t050016z
1 orphan(s) found (0 collectible)
```

That path is a **live, registered herdr-routines worktree** — the retained one for
the in-flight `20260903T050016Z` run. fitted's own 47 worktrees live at
`~/.herdr/worktrees/fitted` and were never scanned.

### Why this is `high`, not cosmetic

It reported `0 collectible` here only because that entry happened to be a non-empty
directory, which issue 045's design deliberately never deletes. Had it been an empty
directory or a dangling symlink — both of which `--delete` *does* collect, and both
of which herdr-routines' own worktree dir contained until a few hours ago — then
`gc --delete --yes --repo <some-other-repo>` would have **deleted a live
herdr-routines worktree entry while pointed at a completely different project**.

The blast radius is cross-repo, which is the one thing a per-repo tool should never
be.

### Why the tests did not catch it

`tests/test_gc.py`'s orphan tests all pass `--worktrees-root` explicitly, so they
exercise the parameter and never the default derivation. This is the same failure
shape as issue 042 and issue 038: a test that only ever runs the configuration the
real invocation does not use. The bug appeared the first time `gc` was pointed at a
second repository.

## Design (proposal)

Derive the scan root from the target repo instead of a constant:

- Take the common parent of the repo's own registered worktree paths
  (`git worktree list` already gives them, and `collect_rows` already returns them),
  falling back to `~/.herdr/worktrees/<repo-name>` when there are none registered.
- Keep `--worktrees-root` as an explicit override for tests and unusual layouts.
- **Refuse to scan a directory that contains no registered worktree of the target
  repo** — a scan root that matches nothing registered is a misconfiguration, and
  reporting every entry in it as an orphan is precisely the dangerous behaviour. Warn
  and skip the orphan sweep rather than listing another project's worktrees.

The same hardcoded default is used by `check_inflight`'s
`_default_pipeline_worktrees_root()` for the in-flight state.json probe. That one is
arguably correct — in-flight detection is specifically about *pipeline* runs, which
only exist for herdr-routines — but it should be commented as deliberate so the next
reader does not "fix" it into the same bug.

## Acceptance criteria

1. `gc --repo <other>` scans that repo's worktree directory, not herdr-routines'. Test: `test_gc_orphan_scan_follows_target_repo`
2. A live worktree of an unrelated repo is never reported as an orphan. Test: `test_gc_never_reports_other_repo_worktree_as_orphan`
3. A scan root containing no registered worktree of the target repo is skipped with a warning, not swept. Test: `test_gc_skips_orphan_sweep_on_unrelated_root`
4. `--worktrees-root` still overrides the derived value. Test: `test_gc_worktrees_root_override_respected`
