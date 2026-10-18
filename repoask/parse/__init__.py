"""Chunker dispatch: Python via tree-sitter, everything else as text.

``language_for_path`` decides indexability *before* any file is read, which
keeps ingestion cheap and makes the supported-file list explicit.
"""

from __future__ import annotations

from repoask.errors import ParseError
from repoask.models import Chunk
from repoask.parse.python_chunker import (
    DEFAULT_MAX_CHUNK_CHARS,
    DEFAULT_MAX_CHUNK_LINES,
    DEFAULT_OVERLAP_LINES,
    chunk_python_source,
    decode_python_source,
)
from repoask.parse.text_chunker import chunk_markdown_source, chunk_plain_text_source

__all__ = [
    "DEFAULT_MAX_CHUNK_CHARS",
    "DEFAULT_MAX_CHUNK_LINES",
    "DEFAULT_OVERLAP_LINES",
    "chunk_python_source",
    "chunk_source",
    "decode_python_source",
    "decode_source",
    "language_for_path",
    "SUPPORTED_EXTENSIONS",
]

# Shared decoder for every text file, not just Python: BOM handling, CRLF
# normalisation and a latin-1 fallback so a single odd byte cannot fail a build.
decode_source = decode_python_source

PYTHON_EXTENSIONS = frozenset({".py", ".pyi"})
MARKDOWN_EXTENSIONS = frozenset({".md", ".markdown"})
TEXT_EXTENSIONS = frozenset(
    {".txt", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json", ".rst", ".env"}
)
# Extensionless files that are still worth indexing in a Python project.
# Stored lowercase because lookup is case-insensitive.
TEXT_FILENAMES = frozenset(
    {"dockerfile", "makefile", "requirements.txt", "setup.cfg", ".gitignore", ".env.example"}
)

SUPPORTED_EXTENSIONS = PYTHON_EXTENSIONS | MARKDOWN_EXTENSIONS | TEXT_EXTENSIONS


def language_for_path(path: str) -> str | None:
    """Return ``"python"``, ``"markdown"``, ``"text"`` or ``None`` if unsupported."""
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    lower = name.lower()
    if lower.endswith(".py") or lower.endswith(".pyi"):
        return "python"
    if lower.endswith(".md") or lower.endswith(".markdown"):
        return "markdown"
    if lower in TEXT_FILENAMES:
        return "text"
    dot = lower.rfind(".")
    if dot != -1 and lower[dot:] in TEXT_EXTENSIONS:
        return "text"
    return None


def chunk_source(
    text: str,
    *,
    repo_id: str,
    commit: str,
    path: str,
    encoding: str = "utf-8",
    language: str | None = None,
    max_chunk_lines: int = DEFAULT_MAX_CHUNK_LINES,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> list[Chunk]:
    """Chunk one already-decoded file. Raises ``ParseError`` if unsupported."""
    resolved = language or language_for_path(path)
    if resolved == "python":
        return chunk_python_source(
            text,
            repo_id=repo_id,
            commit=commit,
            path=path,
            encoding=encoding,
            max_chunk_lines=max_chunk_lines,
            max_chunk_chars=max_chunk_chars,
            overlap_lines=overlap_lines,
        )
    if resolved == "markdown":
        return chunk_markdown_source(
            text,
            repo_id=repo_id,
            commit=commit,
            path=path,
            encoding=encoding,
            max_chunk_lines=max_chunk_lines,
            max_chunk_chars=max_chunk_chars,
            overlap_lines=overlap_lines,
        )
    if resolved == "text":
        return chunk_plain_text_source(
            text,
            repo_id=repo_id,
            commit=commit,
            path=path,
            encoding=encoding,
            max_chunk_lines=max_chunk_lines,
            max_chunk_chars=max_chunk_chars,
            overlap_lines=overlap_lines,
        )
    raise ParseError(f"unsupported file type for chunking: {path}")
