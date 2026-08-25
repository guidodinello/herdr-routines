"""Minimal stdlib fixed-width table rendering shared by the read-only visibility commands
(`ps`, `scheduled`). No new runtime dependencies — see spec 20260825T070012Z §Risks."""

from __future__ import annotations

# Long branch/pane/run ids are truncated rather than allowed to stretch a column past
# what `herdr notification --body` can usefully show; `--json` output is never truncated.
MAX_CELL_WIDTH = 48


def _cell(text: str) -> str:
    if len(text) <= MAX_CELL_WIDTH:
        return text
    return text[: MAX_CELL_WIDTH - 1] + "…"


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    """Fixed-width table: headers on the first line, columns padded to the widest cell.
    Cells longer than MAX_CELL_WIDTH are truncated with an ellipsis."""
    cells = [[_cell(str(c)) for c in row] for row in rows]
    widths = [
        max(len(header), *(len(row[i]) for row in cells)) if cells else len(header)
        for i, header in enumerate(headers)
    ]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths))]
    for row in cells:
        lines.append("  ".join(c.ljust(w) for c, w in zip(row, widths)))
    return "\n".join(lines)
