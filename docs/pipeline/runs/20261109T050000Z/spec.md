# Spec v1 — issue 082: carry the review select mode as a Literal from config to history

Run: 20261109T050000Z · Issue: `docs/process/issues/082-review-select-mode-literal.md` (low, infra)

## Problem

`config.py` validates a `my_open_prs` job's `select` list against
`VALID_REVIEW_SELECT` (config.py:135, `frozenset({"author", "review_requested"})`),
then stores it as a plain `tuple[str, ...]` on `Job.select` (config.py:553). Every
downstream consumer re-types it as an unchecked `str`:

- `auto_fix.ReviewPR.mode` (auto_fix.py:159, a `str` with only a comment as the contract)
- `GtClient.search_prs(modes: Sequence[str])` (auto_fix.py:112, real impl at :341)
- `review_scan.Candidate.select_mode` (review_scan.py:70) and
  `ScanOutcome/Classification.truncated_modes`
- `history.HistoryExtra.select_mode` (history.py:166 — the write-side schema)

Consequence: a typo in a comparison such as `pr.mode == "author"` (review_scan.py:112)
or in a GraphQL alias keyed by mode is invisible to mypy. There are two independent
copies of the closed value set (config's frozenset and comments in three modules).

## Approach

Define one spec-derived Literal in config and thread it through the config →
auto_fix → review_scan → history-writer chain. No `cast`; no behavior change.

1. **config.py** — define next to the existing selector Literals (same pattern as
   `TargetSelector` at config.py:126, which already derives its set with `get_args`):
   `ReviewSelectMode = Literal["author", "review_requested"]`, and
   `VALID_REVIEW_SELECT: frozenset[str] = frozenset(get_args(ReviewSelectMode))`.
   The annotation stays `frozenset[str]` so the membership validation at
   config.py:1718 (`mode not in VALID_REVIEW_SELECT` where `mode` is a runtime `str`)
   keeps type-checking untouched.
   Change `Job.select: tuple[ReviewSelectMode, ...] | None` (config.py:553). The
   builder at config.py:1706–1722 validates each `select_raw` item then does
   `select = tuple(select_raw)` — no cast needed: items arrive untyped/`Any` from the
   YAML `list`, and mypy accepts `Any` → Literal at the dataclass boundary.
2. **auto_fix.py** — import `ReviewSelectMode` (module already imports from config,
   auto_fix.py:23, so no import cycle). Change:
   - `GtClient.search_prs(self, modes: Sequence[ReviewSelectMode], ...)`
     (protocol, :112)
   - real `GhCli.search_prs` (:341) — same annotation; its internal
     `aliases = [m for m in modes if m in REVIEW_SEARCH_QUALIFIERS]` infers
     `list[ReviewSelectMode]`; the guard stays (GraphQL payload keying reads it).
   - `truncated: tuple[ReviewSelectMode, ...]` on `ReviewSearch` (:170) and the
     local `truncated` list append (:364-372).
   - `ReviewPR.mode: ReviewSelectMode` (:159; comment becomes the Literal).
   - `_parse_review_node(node: object, mode: ReviewSelectMode)` (:895) — its only
     caller passes an alias from the typed `modes` sequence.
   - `REVIEW_SEARCH_QUALIFIERS: dict[str, str]` (:884) stays open-keyed: it is used
     by validation/lookup against runtime strings, not part of the typed chain.
3. **review_scan.py** — `Candidate.select_mode: ReviewSelectMode` (:70),
   `ScanOutcome.truncated_modes: tuple[ReviewSelectMode, ...]` (:82),
   `Classification.truncated_modes` likewise (:245). `modes = list(job.select or ())`
   (:128) now infers `list[ReviewSelectMode]` and flows straight into
   `search_prs`. `_skip_reason`'s `pr.mode == "author"` (:112) becomes mypy-checked.
4. **history.py** — `HistoryExtra.select_mode: ReviewSelectMode` (:166), the
   write-side schema only. `HistoryRecord.extra` keeps its open-ended
   `Mapping[str, object] | None` for read-back — that boundary is issue 064's
   scoping, deliberately not widened here (records on disk stay open-ended).
5. **tick.py / cli.py** — no edits expected: tick.py:4930 writes
   `select_mode=cand.select_mode` into `HistoryExtra` (now Literal→Literal), and
   cli.py:1756/1799 only render/serialize it (f-string / JSON, both fine).

Test-side (tests are mypy-checked via `[tool.mypy] files = ["src", "tests"]`):
the two `_pr(...)` helpers in tests/test_review_scan.py:147 and
tests/test_review_migration.py:59 must change `mode: str = "author"` →
`mode: ReviewSelectMode = "author"`. Test fakes declaring
`search_prs(self, modes: Sequence[str], ...)`
(test_auto_fix.py:141, test_gates.py:93, test_github_event.py:257,
test_pipeline_run.py:489) need no edit — a wider accepted parameter type is
mypy-compatible (contravariance) with the narrowed protocol. test_review_scan.py:220
already uses `Any`.

## Files touched

- `src/herdr_routines/config.py` (definition + derivation + `Job.select`)
- `src/herdr_routines/auto_fix.py` (protocol, impl, `ReviewPR`, `_parse_review_node`, `ReviewSearch`)
- `src/herdr_routines/review_scan.py` (`Candidate`, `ScanOutcome`, `Classification`)
- `src/herdr_routines/history.py` (`HistoryExtra.select_mode`)
- `tests/test_review_scan.py`, `tests/test_review_migration.py` (`_pr` helper annotations)

## Risks

- **mypy cascade via `REVIEW_SEARCH_QUALIFIERS`** — its values are keyed by plain
  `str`; getattr-style lookups along the chain are fine, but any site indexing it
  with an unvalidated runtime string resurfaces. No known site does.
- **Literal narrowing at parse boundaries** — every place converting YAML/GraphQL
  payloads to the chain goes through existing `isinstance`/membership guards whose
  surroundings are `Any`/`str`; if one isn't (mypy points it out), the fix is
  annotating the intermediate, never a `cast` (acceptance forbids casts).
- **Test helpers** — the two `_pr` helpers named above, plus any other test that
  builds `ReviewPR` from a plain `str` variable; found by `uv run mypy` and fixed
  with a Literal annotation.
- **Behavior drift** — none intended: this is typing-only. Validation error text
  (`sorted(VALID_REVIEW_SELECT)`) is unchanged; JSONL output byte-identical.

## Verification

1. `uv run pytest -q` green (round-trip/JSONL tests untouched).
2. `scripts/ci-check.sh` green (lint + typecheck + tests; mypy covers `src` and `tests`).
3. `uv run mypy --strict src` green (the acceptance clause; no new `cast`/`type: ignore`
   introduced — `warn_unused_ignores` must not fire anything new).
4. Structural check: `Job.select`, `ReviewPR.mode`, `Candidate.select_mode`,
   `HistoryExtra.select_mode` all `ReviewSelectMode`; `VALID_REVIEW_SELECT == frozenset(get_args(ReviewSelectMode))`
   assertable in a quick REPL or a one-line existing config test addition (optional).
