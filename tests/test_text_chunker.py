"""Tests for text/Markdown chunking and file-type dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from repoask.errors import ParseError
from repoask.parse import chunk_source, language_for_path

FIXTURES = Path(__file__).parent / "fixtures" / "demo_repo"
REPO_ID = "demo-repo"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def load_text(rel_path: str) -> str:
    return (FIXTURES / rel_path).read_text(encoding="utf-8")


def test_language_dispatch_covers_python_markdown_and_config() -> None:
    assert language_for_path("app/service.py") == "python"
    assert language_for_path("README.md") == "markdown"
    assert language_for_path("config.toml") == "text"
    assert language_for_path("Makefile") == "text"
    assert language_for_path("requirements.txt") == "text"
    assert language_for_path("logo.png") is None
    assert language_for_path("archive.zip") is None


def test_markdown_splits_on_headings_and_ignores_fenced_comments() -> None:
    text = load_text("README.md")
    chunks = chunk_source(text, repo_id=REPO_ID, commit=COMMIT, path="README.md")
    symbols = [chunk.symbol for chunk in chunks if chunk.symbol]
    assert symbols[0] == "Demo Repo"
    assert {"安装", "配置", "任务流程"} <= set(symbols)
    # A '#' line inside a fenced block is a shell comment, not a heading.
    assert not any("代码块里的这一行不是标题" in (symbol or "") for symbol in symbols)

    lines = text.split("\n")
    for chunk in chunks:
        assert chunk.kind == "text"
        assert chunk.content == "\n".join(lines[chunk.start_line - 1 : chunk.end_line])


def test_plain_config_file_is_one_line_accurate_chunk() -> None:
    text = load_text("config.toml")
    chunks = chunk_source(text, repo_id=REPO_ID, commit=COMMIT, path="config.toml")
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.start_line == 1
    assert "port = 8080" in chunk.content
    assert chunk.content == "\n".join(text.split("\n")[chunk.start_line - 1 : chunk.end_line])


def test_markdown_without_headings_falls_back_to_line_windows() -> None:
    text = "\n".join(f"line {index}" for index in range(1, 30))
    chunks = chunk_source(
        text, repo_id=REPO_ID, commit=COMMIT, path="notes.md", max_chunk_lines=10
    )
    assert len(chunks) > 1
    assert all(chunk.part_count == len(chunks) for chunk in chunks)
    lines = text.split("\n")
    for chunk in chunks:
        assert chunk.content == "\n".join(lines[chunk.start_line - 1 : chunk.end_line])


def test_unsupported_extension_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        chunk_source("binary", repo_id=REPO_ID, commit=COMMIT, path="image.png")
