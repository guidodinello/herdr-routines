---
id: "045"
title: "gc removes registered worktrees but leaves orphaned directories and dangling symlinks behind"
status: done
priority: low
area: cli
---

## Description

`gc --delete` removes a branch and its *registered* worktree. It does not touch
entries in the worktree parent directory that git no longer knows about, so the
directory accumulates debris that no command reports and no command cleans.

Found on the Pi immediately after the 2026-09-06 sweep (20 branches deleted, 0
failures). `~/.herdr/worktrees/herdr-routines/` still contained:

```
DIR      auto-pipeline-20260826t031438Z  entries=0  has_git=no      <- orphan
SYMLINK  auto-pipeline-20260902T050021Z -> ...t050021z  target=NO   <- dangling
DIR      auto-pipeline-20260903t050016z  entries=19 has_git=yes     <- live, registered
SYMLINK  auto-pipeline-20260903T050016Z -> ...t050016z  target=yes  <- live alias
```

Two of the four entries were debris; `git worktree list` knew about neither. They
were removed by hand.

### The case-variant symlinks are the interesting part

Branch names carry an uppercase timestamp (`auto/pipeline-20260903T050016Z`) while
the worktree directory is lowercased (`...t050016z`), and something — herdr, most
likely — creates a mixed-case **symlink** alongside pointing at the real directory.
That alias is legitimate while its target lives. When `gc` removes the target it
leaves the symlink behind, now dangling.

So the debris is not random: every collected worktree that had an alias leaves one
dangling symlink. The pile grows at the same rate as the pipeline runs.

### Why it matters, modestly

Nothing breaks today — this is filed `low` deliberately. But:

- A dangling symlink named after a branch is actively misleading when someone lists
  the directory looking for a run's worktree.
- Anything that later globs this directory to infer live runs (`pipeline_watchdog`
  reads `state.json` under `auto-pipeline-*/` by exactly this kind of pattern) can
  be confused by an entry that resolves to nothing.
- It is the same class of gap as issue 039: the tool reports a clean result while
  the directory it manages is not clean.

## Design

Extend `gc`'s removal step, and its inventory:

- After removing a registered worktree, remove a same-named case-variant symlink
  pointing at it, if one exists. Only remove a symlink whose target is the directory
  just deleted — never a symlink to something still live.
- Report orphans in `gc --dry-run`: entries under the worktree parent that match
  `auto-*` but appear in no `git worktree list` — empty directories and dangling
  symlinks — as a distinct section, so the inventory tells the whole truth. Listing
  them read-only is the useful half and carries no risk.
- Collect them under `--delete` only when they are provably inert: a dangling
  symlink, or a directory that is empty and has no `.git`. **Never** recursively
  delete a non-empty unregistered directory — that could be someone's manual
  checkout or a worktree whose registration was lost, and destroying it is
  unrecoverable. Report those instead and let a human decide.

## Acceptance criteria

1. A dangling symlink left by a collected worktree is reported by `--dry-run`. Test: `test_gc_dry_run_reports_dangling_symlink`
2. `--delete` removes a dangling symlink. Test: `test_gc_delete_removes_dangling_symlink`
3. `--delete` removes an empty unregistered `auto-*` directory. Test: `test_gc_delete_removes_empty_orphan_dir`
4. A non-empty unregistered directory is reported but never deleted. Test: `test_gc_delete_never_removes_nonempty_orphan`
5. A live symlink whose target is still registered is untouched. Test: `test_gc_preserves_live_worktree_alias`
