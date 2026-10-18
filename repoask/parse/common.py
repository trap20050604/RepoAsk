"""Small helpers shared by the Python and text chunkers.

Line numbering convention used across the project:
``lines = text.split("\\n")`` so that index ``i`` is exactly tree-sitter row ``i``
and the 1-based line number is ``i + 1``. A chunk covering rows ``[a, b]`` has
``content == "\\n".join(lines[a:b+1])``.
"""

from __future__ import annotations

from collections.abc import Sequence


def trim_bounds(lines: Sequence[str], start_row: int, end_row: int) -> tuple[int, int] | None:
    """Strip blank lines at both ends of a row range.

    Returns ``None`` when the range contains only whitespace. Trimming keeps a
    contiguous slice, so the original-text invariant is preserved.
    """
    while start_row <= end_row and not lines[start_row].strip():
        start_row += 1
    while end_row >= start_row and not lines[end_row].strip():
        end_row -= 1
    if start_row > end_row:
        return None
    return start_row, end_row


def needs_split(lines: Sequence[str], max_lines: int, max_chars: int) -> bool:
    if len(lines) > max_lines:
        return True
    return sum(len(line) + 1 for line in lines) > max_chars


def split_windows(
    lines: Sequence[str],
    max_lines: int,
    max_chars: int,
    overlap_lines: int,
) -> list[tuple[int, int]]:
    """Greedy, overlap-aware windows over ``lines`` as inclusive local ranges.

    Progress is guaranteed: every window advances at least one line, so a very
    long single line still gets split across windows instead of looping.
    """
    if max_lines < 1:
        raise ValueError("max_lines must be >= 1")
    if max_chars < 1:
        raise ValueError("max_chars must be >= 1")
    overlap = max(0, min(overlap_lines, max_lines - 1))

    total = len(lines)
    windows: list[tuple[int, int]] = []
    start = 0
    while start < total:
        end = start
        chars = 0
        while end < total and (end - start + 1) <= max_lines:
            cost = len(lines[end]) + 1
            if chars + cost > max_chars and end > start:
                break
            chars += cost
            end += 1
        end -= 1
        windows.append((start, end))
        if end >= total - 1:
            break
        start = max(end + 1 - overlap, start + 1)
    return windows
