# Spec v2 — issue 082: carry the review select mode as a Literal from config to history

Run: 20261109T050000Z · Issue: `docs/process/issues/082-review-select-mode-literal.md` (low, infra) · v1→v2 changes are listed in [`## Changelog v1→v2`](#changelog-v1v2).

## Problem

`config.py` validates a `my_open_prs` job's `select` list against
`VALID_REVIEW_SELECT` (config.py:135, `frozenset({"author", "review_requested"})`),
then stores it as a plain `tuple[str, ...]` on `Job.select` (config.py:553). Every
downstream consumer re-types it as an unchecked `str`:

- `auto_fix.ReviewPR.mode` (auto_fix.py:159, a `str` with only a comment as the contract)
- `GhClient.search_prs(modes: Sequence[str])` (auto_fix.py:112, real impl at :341)
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
    - `GhClient.search_prs(self, modes: Sequence[ReviewSelectMode], ...)`
      (protocol, :112)
    - real `RealGhClient.search_prs` (:341) — same annotation; its internal
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
`mode: ReviewSelectMode = "author"`. The `ReviewGh` fake
(tests/test_review_scan.py:163) must change its `truncated_modes:
tuple[str, ...] = ()` param (:177) to `tuple[ReviewSelectMode, ...]` — it
feeds `ReviewSearch(..., truncated_modes=self.truncated_modes)` (:224), which
fails mypy once the field narrows (v1 missed this site). The other test fakes
declaring `search_prs(self, modes: Sequence[str], ...)` (test_auto_fix.py:141,
test_gates.py:93, test_github_event.py:257, test_pipeline_run.py:489) need no
edit — a wider accepted parameter type is mypy-compatible (contravariance)
with the narrowed protocol, and their `ReviewSearch(prs=(), ...)` /
`None` returns never touch `truncated_modes`. test_review_scan.py:220 already
uses `Any`. The report renderer's `truncated_modes: Sequence[str]` param
(review_scan.py:327) stays open on purpose — `Sequence[ReviewSelectMode]`
flows into it by covariance, and its output is human/JSON text, not typed
state.

## Files touched

- `src/herdr_routines/config.py` (definition + derivation + `Job.select`)
- `src/herdr_routines/auto_fix.py` (protocol, impl, `ReviewPR`, `_parse_review_node`, `ReviewSearch`)
- `src/herdr_routines/review_scan.py` (`Candidate`, `ScanOutcome`, `Classification`)
- `src/herdr_routines/history.py` (`HistoryExtra.select_mode`)
- `tests/test_review_scan.py` (`_pr` helper annotation; `ReviewGh` fake's
  `truncated_modes` param), `tests/test_review_migration.py` (`_pr` helper
  annotation)

## Risks

- [blocking] **mypy cascade via `REVIEW_SEARCH_QUALIFIERS`** — its values are keyed by plain
  `str`; getattr-style lookups along the chain are fine, but any site indexing it
  with an unvalidated runtime string resurfaces. No known site does. —
  confidence: high (the four call sites along the chain were read; all pass
  already-typed values)
- [blocking] **Literal narrowing at parse boundaries** — every place converting YAML/GraphQL
  payloads to the chain goes through existing `isinstance`/membership guards whose
  surroundings are `Any`/`str`; if one isn't (mypy points it out), the fix is
  annotating the intermediate, never a `cast` (acceptance forbids casts). —
  confidence: high (the two boundaries — config's builder at config.py:1706–1722
  and `_parse_review_node`'s caller — are guarded exactly this way)
- [blocking] **Test helpers** — the two `_pr` helpers, the `ReviewGh` fake's
  `truncated_modes` param, plus any other test that
  builds `ReviewPR` from a plain `str` variable; found by `uv run mypy` and fixed
  with a Literal annotation. — confidence: high (tests are inside the mypy
  file set, so nothing can hide)
- [non-blocking] **Behavior drift** — none intended: this is typing-only. Validation error text
  (`sorted(VALID_REVIEW_SELECT)`) is unchanged; JSONL output byte-identical. —
  confidence: high (pinned by round-trip and validation tests that must stay
  green unmodified)

## Verification

1. `uv run pytest -q` green (round-trip/JSONL tests untouched).
2. `scripts/ci-check.sh` green (lint + typecheck + tests; mypy covers `src` and `tests`).
3. `uv run mypy --strict src` green (the acceptance clause; no new `cast`/`type: ignore`
   introduced — `warn_unused_ignores` must not fire anything new).
4. Structural check: `Job.select`, `ReviewPR.mode`, `Candidate.select_mode`,
   `HistoryExtra.select_mode` all `ReviewSelectMode`; `VALID_REVIEW_SELECT == frozenset(get_args(ReviewSelectMode))`
   assertable in a quick REPL or a one-line existing config test addition (optional).

## Acceptance criteria

1. [blocking] One spec-derived Literal in config: config.py defines
   `ReviewSelectMode = Literal["author", "review_requested"]` next to the
   selector Literals, `VALID_REVIEW_SELECT` is derived with
   `frozenset(get_args(ReviewSelectMode))` (annotation stays `frozenset[str]`
   so the runtime membership checks keep type-checking), the two independent
   value-set copies collapse into it, and `Job.select` reads
   `tuple[ReviewSelectMode, ...] | None` (config.py:553) with no `cast` —
   builder validation text unchanged (empty/duplicate/bogus `select` and the
   `select`-requires-`kind: review` rejects all still fire, and the happy
   tuple still loads as `("author", "review_requested")`). — confidence:
   high — Test: test_review_job_config_validation
2. [blocking] auto_fix chain narrowed, single point of truth imported:
   `GhClient.search_prs` (protocol, auto_fix.py:112) and
   `RealGhClient.search_prs` (:341) both take
   `Sequence[ReviewSelectMode]`; `ReviewPR.mode` (:159),
   `ReviewSearch.truncated_modes` (:170) and `_parse_review_node`'s
   `mode` (:895) read `ReviewSelectMode`; the impl still builds exactly one
   alias per selected mode off the same open `REVIEW_SEARCH_QUALIFIERS:
   dict[str, str]` and returns the same `ReviewSearch` payload — a scan over
   a real qualification still returns the author-owned candidate plus its
   truncation records intact. — confidence: high —
   Test: test_review_scan_selects_open_non_draft_prs
3. [blocking] The original bug is dead at the type level: `_skip_reason`'s
   `pr.mode == "author"` comparison (review_scan.py:112) is mypy-checked —
   a typo'd comparison could no longer compile — while every existing skip
   rule (ghost author, fork head-repo, auto-branch opt-in) keeps behaving
   identically on the narrowed Candidates. — confidence: high —
   Test: test_review_scan_skips_fork_prs
4. [blocking] review_scan state surfaces all closed: `Candidate.select_mode`
   (review_scan.py:70), `ScanOutcome.truncated_modes` (:82) and
   `Classification.truncated_modes` (:245) read `ReviewSelectMode` /
   `tuple[ReviewSelectMode, ...]`; the
   `modes = list(job.select or ())` handoff (:128) flows into `search_prs`
   with no intermediate retyping, and the report renderer
   (`truncated_modes: Sequence[str]` at review_scan.py:327) still accepts
   them by covariance and renders the same truncated-modes note for a
   truncated search. — confidence: high —
   Test: test_review_report_lists_candidates_and_skips
5. [blocking] History write-side schema narrowed, read-back left open:
   `HistoryExtra.select_mode` (history.py:166) reads `ReviewSelectMode`, the
   tick writer (tick.py:4930) passes `cand.select_mode` Literal→Literal,
   and `HistoryRecord.extra` stays `Mapping[str, object] | None` — a record
   written with a `select_mode` still round-trips with unknown extra keys
   preserved, untouched by this change. — confidence: high —
   Test: test_review_job_selection_unchanged_after_migration
6. [blocking] Byte-for-byte stability of the outputs this chain feeds:
   candidate JSONL records (including the `select_mode` key, name and key
   order included) and the `ghr review-scan` human/JSON rendering are
   unchanged by the Literal threading. — confidence: high —
   Test: test_round_trip_is_byte_identical_and_keeps_key_order
7. [blocking] The chain's end state is pinned structurally by a new test
   (added with the change itself): a test asserts
   via `typing.get_type_hints` that `Job.select`, `ReviewPR.mode`,
   `ReviewSearch.truncated_modes`, `Candidate.select_mode`,
   `ScanOutcome.truncated_modes`, `Classification.truncated_modes` and
   `HistoryExtra.select_mode` are built from `ReviewSelectMode`, that
   `VALID_REVIEW_SELECT == frozenset(get_args(ReviewSelectMode))`, and that
   no `cast` or new `type: ignore` was introduced anywhere in the diff
   (`uv run mypy` — which covers src and tests — stays green with
   `warn_unused_ignores` quiet). — confidence: high —
   Test: test_review_select_mode_literal_threads_from_config_to_history
8. [non-blocking] Test-side edits stay within the three sites v2 names (the two
   `_pr` helpers and the `ReviewGh` fake's `truncated_modes` param):
   the four `Sequence[str]` fakes
   (test_auto_fix.py:141, test_gates.py:93, test_github_event.py:257,
   test_pipeline_run.py:489) are provably unedited, and `uv run pytest -q`
   plus `scripts/ci-check.sh` (lint incl. the dead-code check, typecheck,
   tests) stay green with no whitelist additions. — confidence: medium —
   Test: test_lint_runs_the_dead_code_check

## Review

- blocking: 1–7 — the Literal's one home with derived validation (1), the
  protocol/impl/dataclass narrowing in auto_fix (2), the type-checking of the
  comparison that motivated the issue (3), the review_scan state surfaces
  (4), the history write-side without widening the read-back boundary (5),
  byte-level output stability (6), and the get_type_hints + no-cast structural
  pin (7) all gate the merge.
- non-blocking: 8 — test-helper churn and lint/whitelist follow-through don't
  gate a typing-only change; mypy over `["src", "tests"]` already forces the
  three named sites.
- Basis: every criterion pins a structural or behavioral fact over the change
  v1's Approach already described, anchored to existing tests that must stay
  green unmodified (1–6) plus one new structural pin (7) matching the
  repo's contract-test convention (`*_review_tiers_present`-style pins).
  Confidence is high throughout because every site, class name, line number
  and test named was re-verified on this tree (2026-11-09); AC8 is medium
  because vulture's view of a get_type_hints-based test is only learned by
  running it.

## Changelog v1→v2

- **v1 (2026-11-09)**: first draft. Approach: one `ReviewSelectMode` Literal
  defined in config and threaded through config → auto_fix → review_scan →
  history-writer; no `cast`; no behavior change.
- **v2 (this update, after review)**:
  - Bumped the header to v2 with a pointer here.
  - Added `## Acceptance criteria`: eight numbered items, each tagged
    `[blocking]`/`[non-blocking]` with a `confidence:` tier and ending in an
    exact `Test: <name>`. Seven anchor existing pins that must stay green
    unmodified; one new test
    (`test_review_select_mode_literal_threads_from_config_to_history`) pins
    the chain's annotation end state and the no-cast clause, added with the
    change itself.
  - Added a `## Review` section stating the blocking/non-blocking split and
    the basis for the confidence tiers.
  - Tagged every Risks bullet `[blocking]`/`[non-blocking]` with a
    `confidence:` tier (v1's Risks had neither); the "Behavior drift" risk
    was re-classed non-blocking — a typing-only change cannot gate the merge
    on a behavior risk that its own tests pin. No risk content changed.
  - Corrected the class name in Problem: v1 wrote `GtClient.search_prs` —
    the protocol is `GhClient` (auto_fix.py:61, `class GhClient(Protocol)`),
    and the real impl class at :341 is `RealGhClient` (v1's Approach also
    said "`GhCli`"). Line numbers were correct; names now are too.
  - Corrected the test-impact claim: v1 said the only test edits are the two
    `_pr(...)` helpers and that all other fakes "need no edit". The `ReviewGh`
    fake in tests/test_review_scan.py (class at :163) declares
    `truncated_modes: tuple[str, ...] = ()` at :177 and passes it into
    `ReviewSearch(..., truncated_modes=self.truncated_modes)` at :224, which
    fails mypy once `ReviewSearch.truncated_modes` narrows — so that fake's
    param must also become `tuple[ReviewSelectMode, ...]`. v2 commits to it
    in the Approach, the Files-touched list and AC7/AC8. The four
    `Sequence[str]` fakes remain provably unedited (contravariance + their
    `ReviewSearch(prs=())` returns), and v1's `Any`-fake note
    (test_review_scan.py:220) was verified and kept.
  - Documented the deliberately open boundary the change must *not* cross:
    the report renderer's `truncated_modes: Sequence[str]` param
    (review_scan.py:327) and `HistoryRecord.extra`'s
    `Mapping[str, object] | None` both stay wide; the new acceptance items
    (4, 5) pin this instead of leaving it implied.
