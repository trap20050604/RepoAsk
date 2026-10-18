"""Fallback chunking for README/config/text files.

Python is chunked structurally; everything else is chunked by lines, with
Markdown additionally split on ATX headings so a README section about
"configuration" stays together and can be retrieved on its own.
"""

from __future__ import annotations

from dataclasses import dataclass

from repoask.models import Chunk, content_sha256
from repoask.parse.common import needs_split, split_windows, trim_bounds

DEFAULT_MAX_CHUNK_LINES = 120
DEFAULT_MAX_CHUNK_CHARS = 6_000
DEFAULT_OVERLAP_LINES = 8


@dataclass
class _TextEmitter:
    lines: list[str]
    repo_id: str
    commit: str
    path: str
    language: str
    encoding: str
    max_chunk_lines: int
    max_chunk_chars: int
    overlap_lines: int

    chunks: list[Chunk] | None = None

    def __post_init__(self) -> None:
        if self.chunks is None:
            self.chunks = []

    def emit(self, start_row: int, end_row: int, symbol: str | None) -> None:
        if end_row < start_row:
            return
        bounds = trim_bounds(self.lines, start_row, end_row)
        if bounds is None:
            return
        start_row, end_row = bounds
        block = self.lines[start_row : end_row + 1]
        if needs_split(block, self.max_chunk_lines, self.max_chunk_chars) and len(block) > 1:
            windows = split_windows(
                block, self.max_chunk_lines, self.max_chunk_chars, self.overlap_lines
            )
            total = len(windows)
            for index, (local_start, local_end) in enumerate(windows, start=1):
                self._add(
                    start_row + local_start,
                    start_row + local_end,
                    symbol,
                    (index, total),
                )
            return
        self._add(start_row, end_row, symbol, None)

    def _add(
        self,
        start_row: int,
        end_row: int,
        symbol: str | None,
        part: tuple[int, int] | None,
    ) -> None:
        content = "\n".join(self.lines[start_row : end_row + 1])
        digest = content_sha256(content)
        start_line, end_line = start_row + 1, end_row + 1
        assert self.chunks is not None
        self.chunks.append(
            Chunk(
                repo_id=self.repo_id,
                commit=self.commit,
                path=self.path,
                symbol=symbol,
                start_line=start_line,
                end_line=end_line,
                content_hash=digest,
                chunk_id=(
                    f"{self.repo_id}:{self.commit[:12]}:{self.path}:"
                    f"text:{start_line}-{end_line}:{digest[:10]}"
                ),
                kind="text",
                language=self.language,
                content=content,
                part_index=part[0] if part else None,
                part_count=part[1] if part else None,
                encoding=self.encoding,
            )
        )


def _heading_positions(lines: list[str]) -> list[tuple[int, str]]:
    """ATX headings outside fenced code blocks, as (row, title)."""
    positions: list[tuple[int, str]] = []
    in_fence = False
    for row, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            if hashes <= 6 and (len(stripped) == hashes or stripped[hashes] in " \t"):
                positions.append((row, stripped[hashes:].strip()))
    return positions


def chunk_markdown_source(
    text: str,
    *,
    repo_id: str,
    commit: str,
    path: str,
    encoding: str = "utf-8",
    max_chunk_lines: int = DEFAULT_MAX_CHUNK_LINES,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> list[Chunk]:
    lines = text.split("\n")
    emitter = _TextEmitter(
        lines=lines,
        repo_id=repo_id,
        commit=commit,
        path=path,
        language="markdown",
        encoding=encoding,
        max_chunk_lines=max_chunk_lines,
        max_chunk_chars=max_chunk_chars,
        overlap_lines=overlap_lines,
    )
    headings = _heading_positions(lines)
    if not headings:
        emitter.emit(0, len(lines) - 1, None)
        return emitter.chunks or []

    # Content before the first heading, then one section per heading.
    if headings[0][0] > 0:
        emitter.emit(0, headings[0][0] - 1, None)
    for index, (row, title) in enumerate(headings):
        end_row = headings[index + 1][0] - 1 if index + 1 < len(headings) else len(lines) - 1
        emitter.emit(row, end_row, title or None)
    return emitter.chunks or []


def chunk_plain_text_source(
    text: str,
    *,
    repo_id: str,
    commit: str,
    path: str,
    encoding: str = "utf-8",
    language: str = "text",
    max_chunk_lines: int = DEFAULT_MAX_CHUNK_LINES,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> list[Chunk]:
    lines = text.split("\n")
    emitter = _TextEmitter(
        lines=lines,
        repo_id=repo_id,
        commit=commit,
        path=path,
        language=language,
        encoding=encoding,
        max_chunk_lines=max_chunk_lines,
        max_chunk_chars=max_chunk_chars,
        overlap_lines=overlap_lines,
    )
    emitter.emit(0, len(lines) - 1, None)
    return emitter.chunks or []
