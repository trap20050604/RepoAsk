"""Behaviour tests for the tree-sitter Python chunker.

These tests pin the two properties the rest of the system relies on:
1. every chunk's ``content`` is exactly the original lines ``[start_line, end_line]``;
2. no non-blank line is dropped, including in files with syntax errors.
"""

from __future__ import annotations

from pathlib import Path

from repoask.models import Chunk, content_sha256
from repoask.parse.python_chunker import chunk_python_source, decode_python_source

FIXTURES = Path(__file__).parent / "fixtures" / "demo_repo"
REPO_ID = "demo-repo"
COMMIT = "0123456789abcdef0123456789abcdef01234567"

PYTHON_FILES = ["app/service.py", "app/parser.py", "app/pipeline.py", "app/broken.py"]


def load_chunks(rel_path: str, **kwargs) -> tuple[list[Chunk], str]:
    raw = (FIXTURES / rel_path).read_bytes()
    text, encoding = decode_python_source(raw)
    chunks = chunk_python_source(
        text,
        repo_id=REPO_ID,
        commit=COMMIT,
        path=rel_path,
        encoding=encoding,
        **kwargs,
    )
    return chunks, text


def by_symbol(chunks: list[Chunk]) -> dict[str, Chunk]:
    symbols: dict[str, Chunk] = {}
    for chunk in chunks:
        if chunk.symbol:
            symbols.setdefault(chunk.symbol, chunk)
    return symbols


def covered_rows(chunks: list[Chunk]) -> set[int]:
    rows: set[int] = set()
    for chunk in chunks:
        rows.update(range(chunk.start_line - 1, chunk.end_line))
    return rows


def test_chunk_content_matches_original_lines() -> None:
    """The invariant: content == original lines[start-1:end], never reformatted."""
    for rel_path in PYTHON_FILES:
        chunks, text = load_chunks(rel_path)
        lines = text.split("\n")
        assert chunks, f"no chunks produced for {rel_path}"
        for chunk in chunks:
            expected = "\n".join(lines[chunk.start_line - 1 : chunk.end_line])
            assert chunk.content == expected, f"line slice mismatch in {rel_path} {chunk.chunk_id}"


def test_metadata_is_version_bound_and_consistent() -> None:
    for rel_path in PYTHON_FILES:
        chunks, _ = load_chunks(rel_path)
        ids = [chunk.chunk_id for chunk in chunks]
        assert len(ids) == len(set(ids)), f"duplicate chunk ids in {rel_path}"
        for chunk in chunks:
            assert chunk.repo_id == REPO_ID
            assert chunk.commit == COMMIT
            assert chunk.path == rel_path
            assert chunk.content_hash == content_sha256(chunk.content)
            assert 1 <= chunk.start_line <= chunk.end_line


def test_no_non_blank_line_is_lost() -> None:
    """Coverage guarantee, including the file with a syntax error."""
    for rel_path in PYTHON_FILES:
        chunks, text = load_chunks(rel_path)
        lines = text.split("\n")
        covered = covered_rows(chunks)
        missing = [
            index + 1
            for index, line in enumerate(lines)
            if line.strip() and index not in covered
        ]
        assert not missing, f"{rel_path} lost lines {missing[:10]}"


def test_decorated_function_includes_decorator_lines() -> None:
    chunks, text = load_chunks("app/service.py")
    lines = text.split("\n")
    symbol_chunks = by_symbol(chunks)
    decorated = symbol_chunks["load_tasks"]
    decorator_row = next(
        index for index, line in enumerate(lines) if line.strip() == "@with_retry(times=3)"
    )
    assert decorated.start_line == decorator_row + 1
    assert decorated.content.split("\n")[0] == "@with_retry(times=3)"
    assert decorated.kind == "function"


def test_nested_functions_become_their_own_chunks() -> None:
    chunks, _ = load_chunks("app/service.py")
    symbol_chunks = by_symbol(chunks)
    expected = {
        "with_retry.decorator": "with_retry",
        "with_retry.decorator.wrapper": "with_retry.decorator",
        "load_tasks.normalize": "load_tasks",
    }
    for symbol, parent in expected.items():
        chunk = symbol_chunks[symbol]
        assert chunk.kind == "nested_function", symbol
        assert chunk.parent_symbol == parent, symbol

    # The parent function keeps its full body, which is why the nested function
    # is intentionally present twice (documented trade-off).
    outer = symbol_chunks["load_tasks"]
    assert "def normalize" in outer.content


def test_chinese_comments_are_preserved() -> None:
    chunks, _ = load_chunks("app/service.py")
    joined = "\n".join(chunk.content for chunk in chunks)
    assert "去掉首尾空白，避免重复任务" in joined
    assert "只重试可恢复的错误" in joined
    assert "\ufffd" not in joined, "replacement characters mean decoding went wrong"
    assert all(chunk.encoding == "utf-8" for chunk in chunks)


def test_class_header_stops_before_first_member() -> None:
    """A decorated first member must not be swallowed by the class header."""
    chunks, _ = load_chunks("app/parser.py")
    symbol_chunks = by_symbol(chunks)
    header = symbol_chunks["ConfigParser"]
    assert header.kind == "class"
    assert header.content.startswith("class ConfigParser:")
    assert "supported_formats" in header.content
    assert "def is_empty" not in header.content

    is_empty = symbol_chunks["ConfigParser.is_empty"]
    assert is_empty.kind == "method"
    assert is_empty.content.lstrip().startswith("@property")
    assert is_empty.start_line > header.end_line
    assert is_empty.parent_context is not None
    assert is_empty.parent_context.symbol == "ConfigParser"
    assert is_empty.parent_context.content.startswith("class ConfigParser:")


def test_method_symbols_are_qualified_and_kinds_correct() -> None:
    chunks, _ = load_chunks("app/parser.py")
    symbol_chunks = by_symbol(chunks)
    for name in ("ConfigParser.__init__", "ConfigParser.parse", "ConfigParser.normalize_key"):
        assert name in symbol_chunks, f"missing {name}"
        assert symbol_chunks[name].kind == "method"
        assert symbol_chunks[name].parent_symbol == "ConfigParser"
    assert symbol_chunks["parse_or_default"].kind == "function"
    assert symbol_chunks["parse_or_default"].parent_symbol is None


def test_module_header_becomes_a_text_chunk() -> None:
    chunks, _ = load_chunks("app/service.py")
    headers = [chunk for chunk in chunks if chunk.kind == "text"]
    assert headers, "module docstring/imports should be covered by a text chunk"
    first = min(headers, key=lambda c: c.start_line)
    assert first.start_line == 1
    assert first.content.startswith('"""演示用的小型服务模块')


def test_syntax_error_file_still_produces_flagged_chunks() -> None:
    chunks, text = load_chunks("app/broken.py")
    assert chunks, "a file with a syntax error must still produce chunks"
    assert any(chunk.has_syntax_error for chunk in chunks)
    # An unclosed bracket makes tree-sitter swallow the rest of the file into one
    # ERROR node, so the chunker re-parses top-level blocks. The healthy function
    # after the broken one must still be found, with absolute file line numbers.
    later = by_symbol(chunks)["later_function"]
    assert later.content == "def later_function() -> int:\n    return 1"
    assert later.start_line == text.split("\n").index("def later_function() -> int:") + 1
    assert later.has_syntax_error is False
    lines = text.split("\n")
    for chunk in chunks:
        assert chunk.content == "\n".join(lines[chunk.start_line - 1 : chunk.end_line])


def test_large_function_is_split_with_overlap_and_signature_context() -> None:
    body = "\n".join(f"    total += {index}" for index in range(200))
    text = f"def big() -> int:\n    total = 0\n{body}\n    return total\n"
    lines = text.split("\n")
    chunks = chunk_python_source(
        text,
        repo_id=REPO_ID,
        commit=COMMIT,
        path="app/big.py",
        max_chunk_lines=40,
        max_chunk_chars=10_000,
        overlap_lines=5,
    )
    parts = [chunk for chunk in chunks if chunk.symbol == "big"]
    assert len(parts) > 1
    assert all(part.part_count == len(parts) for part in parts)
    assert [part.part_index for part in parts] == list(range(1, len(parts) + 1))
    assert parts[0].start_line == 1
    assert parts[-1].end_line == len(lines) - 1  # trailing newline yields one empty line
    for part in parts:
        assert part.content == "\n".join(lines[part.start_line - 1 : part.end_line])
        assert part.parent_context is not None
        assert part.parent_context.content.startswith("def big() -> int:")
    # Consecutive parts must share lines, otherwise a symbol split mid-body
    # would create an unreadable gap.
    for left, right in zip(parts, parts[1:]):
        assert right.start_line <= left.end_line


def test_chunking_is_deterministic() -> None:
    first, _ = load_chunks("app/service.py")
    second, _ = load_chunks("app/service.py")
    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]


def test_crlf_and_bom_do_not_shift_line_numbers() -> None:
    raw = "\ufeffdef f():\r\n    return 1\r\n".encode("utf-8")
    text, encoding = decode_python_source(raw)
    assert encoding == "utf-8-sig"
    assert text == "def f():\n    return 1\n"
    chunks = chunk_python_source(
        text, repo_id=REPO_ID, commit=COMMIT, path="bom.py", encoding=encoding
    )
    function = by_symbol(chunks)["f"]
    assert (function.start_line, function.end_line) == (1, 2)
    assert function.content == "def f():\n    return 1"


def test_undecodable_file_falls_back_to_latin1_and_records_it() -> None:
    raw = "def f():\n    return 'caf\xe9'\n".encode("latin-1")
    text, encoding = decode_python_source(raw)
    assert encoding == "latin-1"
    chunks = chunk_python_source(
        text, repo_id=REPO_ID, commit=COMMIT, path="latin.py", encoding=encoding
    )
    assert chunks[0].encoding == "latin-1"
    assert "caf\xe9" in "\n".join(chunk.content for chunk in chunks)
