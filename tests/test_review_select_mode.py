import inspect
import re
import types
from pathlib import Path
from typing import Union, get_args, get_origin, get_type_hints

from herdr_routines import auto_fix, config, history, review_scan


def _element(hint: object) -> object:
    origin = get_origin(hint)
    if origin is tuple:
        return get_args(hint)[0]
    if origin is Union or origin is types.UnionType:
        rest = [a for a in get_args(hint) if a not in (None, type(None))]
        assert len(rest) == 1
        return _element(rest[0])
    return hint


def test_review_select_mode_literal_threads_from_config_to_history() -> None:
    from herdr_routines.config import ReviewSelectMode

    assert get_args(ReviewSelectMode) == ("author", "review_requested")
    assert config.VALID_REVIEW_SELECT == frozenset(get_args(ReviewSelectMode))

    assert _element(get_type_hints(config.Job)["select"]) == ReviewSelectMode
    assert get_type_hints(auto_fix.ReviewPR)["mode"] == ReviewSelectMode
    assert (
        _element(get_type_hints(auto_fix.ReviewSearch)["truncated_modes"])
        == ReviewSelectMode
    )
    assert get_type_hints(review_scan.Candidate)["select_mode"] == ReviewSelectMode
    assert (
        _element(get_type_hints(review_scan.ScanOutcome)["truncated_modes"])
        == ReviewSelectMode
    )
    assert (
        _element(get_type_hints(review_scan.Classification)["truncated_modes"])
        == ReviewSelectMode
    )
    assert get_type_hints(history.HistoryExtra)["select_mode"] == ReviewSelectMode

    src = Path(inspect.getfile(config)).parent
    for name in ("config.py", "auto_fix.py", "review_scan.py", "history.py"):
        text = (src / name).read_text()
        assert not re.search(r"\bcast\(", text), name
        assert "type: ignore" not in text, name
