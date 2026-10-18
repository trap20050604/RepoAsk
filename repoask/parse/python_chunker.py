"""Structure-aware chunking of Python source using tree-sitter.

What becomes a chunk:
  * ``function``          module-level function (full body)
  * ``method``            function directly inside a class
  * ``nested_function``   function inside another function
  * ``class``             class header region: from ``class ...`` through the
                          last line before its first nested definition
  * ``text``              any remaining non-blank lines (imports, module
                          docstring, ``if __name__ == "__main__":`` glue, ...)

Deliberate trade-offs (see docs/architecture.md):
  * A nested function appears both in its parent function's chunk and in its
    own chunk. This keeps parent bodies readable for cross-file questions;
    downstream context building de-duplicates identical content.
  * A class chunk never repeats its methods, so method code is indexed once.
  * Syntax-error files still produce chunks: tree-sitter is error tolerant, and
    the coverage pass guarantees no non-blank line is silently dropped.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from typing import Literal

import tree_sitter_python as _tree_sitter_python
from tree_sitter import Language, Node, Parser

from repoask.models import Chunk, ParentContext, content_sha256
from repoask.parse.common import needs_split, split_windows, trim_bounds

DEFAULT_MAX_CHUNK_LINES = 120
DEFAULT_MAX_CHUNK_CHARS = 6_000
DEFAULT_OVERLAP_LINES = 8

# One Language instance is fine to share; a Parser is cheap and NOT thread safe,
# so chunk_python_source creates its own per call.
_LANGUAGE = Language(_tree_sitter_python.language())

_DEF_TYPES = frozenset({"function_definition", "class_definition"})
_DECORATED = "decorated_definition"
# A class member may be wrapped in decorated_definition, so the "first nested
# definition" search must look for both forms or a decorated first method would
# make the class header cover the entire class body.
_DEF_OR_DECORATED = _DEF_TYPES | {_DECORATED}

ContainerKind = Literal["module", "class", "function"]

_DEF_LINE = re.compile(r"^(?:async\s+def|def|class)\s")


def decode_python_source(raw: bytes) -> tuple[str, str]:
    """Decode source bytes and normalise CRLF to LF.

    Only the line terminator is normalised: it cannot change tree-sitter's row
    count, so line numbers still match the file on disk. Undecodable bytes fall
    back to latin-1 (never fails), and the chosen encoding is recorded so a
    reviewer can see the file was not read as UTF-8.
    """
    encoding = "utf-8"
    if raw.startswith(codecs.BOM_UTF8):
        text = raw.decode("utf-8-sig")
        encoding = "utf-8-sig"
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
            encoding = "latin-1"
    return text.replace("\r\n", "\n"), encoding


def _identifier(node: Node) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return "<anonymous>"
    return name_node.text.decode("utf-8", errors="replace")


def _first_child_of_type(node: Node, types: frozenset[str]) -> Node | None:
    for child in node.children:
        if child.type in types:
            return child
    return None


@dataclass
class _Emitter:
    """Collects chunks for one file, tracking which rows are already covered."""

    lines: list[str]
    repo_id: str
    commit: str
    path: str
    encoding: str
    max_chunk_lines: int = DEFAULT_MAX_CHUNK_LINES
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS
    overlap_lines: int = DEFAULT_OVERLAP_LINES
    chunks: list[Chunk] = field(default_factory=list)
    covered: set[int] = field(default_factory=set)

    # -- construction -----------------------------------------------------
    def _context(self, symbol: str | None, rows: tuple[int, int] | None) -> ParentContext | None:
        if rows is None:
            return None
        start_row, end_row = rows
        return ParentContext(
            symbol=symbol,
            start_line=start_row + 1,
            end_line=end_row + 1,
            content="\n".join(self.lines[start_row : end_row + 1]),
        )

    def _add(
        self,
        start_row: int,
        end_row: int,
        *,
        kind: str,
        symbol: str | None,
        parent_symbol: str | None,
        parent_context: ParentContext | None,
        has_syntax_error: bool,
        part: tuple[int, int] | None,
    ) -> None:
        content = "\n".join(self.lines[start_row : end_row + 1])
        digest = content_sha256(content)
        start_line, end_line = start_row + 1, end_row + 1
        chunk_id = (
            f"{self.repo_id}:{self.commit[:12]}:{self.path}:"
            f"{kind}:{start_line}-{end_line}:{digest[:10]}"
        )
        self.chunks.append(
            Chunk(
                repo_id=self.repo_id,
                commit=self.commit,
                path=self.path,
                symbol=symbol,
                start_line=start_line,
                end_line=end_line,
                content_hash=digest,
                chunk_id=chunk_id,
                kind=kind,  # type: ignore[arg-type]
                language="python",
                content=content,
                parent_symbol=parent_symbol,
                parent_context=parent_context,
                part_index=part[0] if part else None,
                part_count=part[1] if part else None,
                has_syntax_error=has_syntax_error,
                encoding=self.encoding,
            )
        )
        self.covered.update(range(start_row, end_row + 1))

    def emit(
        self,
        start_row: int,
        end_row: int,
        *,
        kind: str,
        symbol: str | None,
        parent_symbol: str | None,
        parent_context: ParentContext | None,
        signature: ParentContext | None,
        has_syntax_error: bool = False,
    ) -> None:
        """Emit one chunk, splitting it first when it exceeds the size budget."""
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
                # A split part keeps its own signature, which is more useful
                # context than the enclosing container's header.
                self._add(
                    start_row + local_start,
                    start_row + local_end,
                    kind=kind,
                    symbol=symbol,
                    parent_symbol=parent_symbol,
                    parent_context=signature or parent_context,
                    has_syntax_error=has_syntax_error,
                    part=(index, total),
                )
            return
        self._add(
            start_row,
            end_row,
            kind=kind,
            symbol=symbol,
            parent_symbol=parent_symbol,
            parent_context=parent_context,
            has_syntax_error=has_syntax_error,
            part=None,
        )

    # -- AST walking ------------------------------------------------------
    def collect(self, root: Node) -> None:
        self._walk(root, scope=None, container_sig=None, container_kind="module")

    def _walk(
        self,
        node: Node,
        *,
        scope: str | None,
        container_sig: ParentContext | None,
        container_kind: ContainerKind,
    ) -> None:
        for child in node.children:
            if child.type == _DECORATED:
                def_node = _first_child_of_type(child, _DEF_TYPES)
                if def_node is None:
                    continue
                outer = child
            elif child.type in _DEF_TYPES:
                def_node, outer = child, child
            else:
                continue
            self._emit_definition(
                def_node,
                outer,
                scope=scope,
                container_sig=container_sig,
                container_kind=container_kind,
            )

    def _signature_rows(self, def_node: Node, outer: Node) -> tuple[int, int] | None:
        """Rows of the decorator/def header ending before the body starts."""
        body = def_node.child_by_field_name("body")
        start_row = outer.start_point.row
        if body is None:
            end_row = min(start_row + 2, def_node.end_point.row)
        else:
            end_row = body.start_point.row - 1
        end_row = min(end_row, def_node.end_point.row)
        if end_row < start_row:
            return None
        return start_row, end_row

    def _emit_definition(
        self,
        def_node: Node,
        outer: Node,
        *,
        scope: str | None,
        container_sig: ParentContext | None,
        container_kind: ContainerKind,
    ) -> None:
        name = _identifier(def_node)
        symbol = f"{scope}.{name}" if scope else name
        signature = self._context(symbol, self._signature_rows(def_node, outer))
        body = def_node.child_by_field_name("body")

        if def_node.type == "class_definition":
            first_def = (
                _first_child_of_type(body, _DEF_OR_DECORATED) if body is not None else None
            )
            if first_def is not None:
                header_end = first_def.start_point.row - 1
            else:
                header_end = def_node.end_point.row
            header_end = min(header_end, def_node.end_point.row)
            self.emit(
                outer.start_point.row,
                header_end,
                kind="class",
                symbol=symbol,
                parent_symbol=scope,
                parent_context=container_sig,
                signature=signature,
                has_syntax_error=def_node.has_error,
            )
            if body is not None:
                self._walk(
                    body, scope=symbol, container_sig=signature, container_kind="class"
                )
            return

        kind: str
        if container_kind == "class":
            kind = "method"
        elif container_kind == "function":
            kind = "nested_function"
        else:
            kind = "function"

        self.emit(
            outer.start_point.row,
            def_node.end_point.row,
            kind=kind,
            symbol=symbol,
            parent_symbol=scope,
            parent_context=container_sig,
            signature=signature,
            has_syntax_error=def_node.has_error,
        )
        if body is not None:
            self._walk(body, scope=symbol, container_sig=signature, container_kind="function")

    # -- coverage ---------------------------------------------------------
    def fill_gaps(self, file_has_error: bool) -> None:
        """Turn every uncovered run of non-blank lines into a ``text`` chunk."""
        total = len(self.lines)
        runs: list[tuple[int, int]] = []
        row = 0
        while row < total:
            if row in self.covered:
                row += 1
                continue
            start = row
            while row < total and row not in self.covered:
                row += 1
            runs.append((start, row - 1))
        for start, end in runs:
            if all(not self.lines[index].strip() for index in range(start, end + 1)):
                continue
            self.emit(
                start,
                end,
                kind="text",
                symbol=None,
                parent_symbol=None,
                parent_context=None,
                signature=None,
                has_syntax_error=file_has_error,
            )


def _definition_group_starts(lines: list[str]) -> list[int]:
    """Rows at column 0 that begin a definition, pulling in its decorators.

    Used only by the fault-tolerance path: an unclosed bracket makes tree-sitter
    consume the rest of the file into one ``ERROR`` node, so we re-parse the file
    as independent top-level blocks and keep the blocks that parse cleanly.
    """
    starts: set[int] = set()
    for row, line in enumerate(lines):
        if not line.strip() or line[:1] in (" ", "\t"):
            continue
        if not _DEF_LINE.match(line):
            continue
        start = row
        probe = row - 1
        while probe >= 0:
            candidate = lines[probe]
            if not candidate.strip():
                break
            if candidate.startswith("@"):
                start = probe
                probe -= 1
                continue
            break
        starts.add(start)
    return sorted(starts)


def _shift_chunk(chunk: Chunk, offset: int) -> Chunk:
    """Move a chunk parsed from an isolated block to absolute file line numbers."""
    start_line = chunk.start_line + offset
    end_line = chunk.end_line + offset
    parent_context = chunk.parent_context
    if parent_context is not None:
        parent_context = parent_context.model_copy(
            update={
                "start_line": parent_context.start_line + offset,
                "end_line": parent_context.end_line + offset,
            }
        )
    digest = content_sha256(chunk.content)
    return chunk.model_copy(
        update={
            "start_line": start_line,
            "end_line": end_line,
            "parent_context": parent_context,
            "chunk_id": (
                f"{chunk.repo_id}:{chunk.commit[:12]}:{chunk.path}:"
                f"{chunk.kind}:{start_line}-{end_line}:{digest[:10]}"
            ),
        }
    )


def _recover_top_level_blocks(
    emitter: "_Emitter",
    lines: list[str],
    *,
    repo_id: str,
    commit: str,
    path: str,
    encoding: str,
) -> int:
    """Re-parse independent top-level blocks; return how many chunks recovered."""
    starts = _definition_group_starts(lines)
    if not starts:
        return 0
    recovered = 0
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        block_lines = lines[start:end]
        block_text = "\n".join(block_lines)
        if not block_text.strip():
            continue
        block_chunks = chunk_python_source(
            block_text,
            repo_id=repo_id,
            commit=commit,
            path=path,
            encoding=encoding,
            max_chunk_lines=emitter.max_chunk_lines,
            max_chunk_chars=emitter.max_chunk_chars,
            overlap_lines=emitter.overlap_lines,
            # One level only: a still-broken block must fall back to text, not
            # start its own recovery pass (that would recurse forever).
            allow_recovery=False,
        )
        # Keep a block only when the whole block parses; otherwise its lines stay
        # as plain text and are still covered and flagged as erroneous.
        if any(chunk.has_syntax_error for chunk in block_chunks):
            continue
        for block_chunk in block_chunks:
            shifted = _shift_chunk(block_chunk, start)
            emitter.chunks.append(shifted)
            emitter.covered.update(range(shifted.start_line - 1, shifted.end_line))
            recovered += 1
    return recovered


def chunk_python_source(
    text: str,
    *,
    repo_id: str,
    commit: str,
    path: str,
    encoding: str = "utf-8",
    max_chunk_lines: int = DEFAULT_MAX_CHUNK_LINES,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
    allow_recovery: bool = True,
) -> list[Chunk]:
    """Chunk one Python file. Never raises for syntax errors; always covers lines."""
    lines = text.split("\n")
    parser = Parser(_LANGUAGE)
    tree = parser.parse(text.encode("utf-8"))
    emitter = _Emitter(
        lines=lines,
        repo_id=repo_id,
        commit=commit,
        path=path,
        encoding=encoding,
        max_chunk_lines=max_chunk_lines,
        max_chunk_chars=max_chunk_chars,
        overlap_lines=overlap_lines,
    )
    file_has_error = bool(tree.root_node.has_error)
    emitter.collect(tree.root_node)
    if allow_recovery and file_has_error and not emitter.chunks:
        # Whole-file error recovery destroyed all structure; retry block by block.
        _recover_top_level_blocks(
            emitter,
            lines,
            repo_id=repo_id,
            commit=commit,
            path=path,
            encoding=encoding,
        )
    emitter.fill_gaps(file_has_error)
    return sorted(emitter.chunks, key=lambda c: (c.start_line, c.end_line, c.chunk_id))
