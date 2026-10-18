"""Tests for safe local repository scanning.

Each test states the safety property it pins, because a regression here means
reading files the user never asked us to read.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from repoask.errors import IngestLimitExceeded, UnsafePathError
from repoask.ingest.local import (
    IngestLimits,
    ensure_within_root,
    read_bytes_checked,
    resolve_git_commit,
    scan_repository,
)

FIXTURES = Path(__file__).parent / "fixtures" / "demo_repo"


def write(path: Path, content: str) -> Path:
    """Write with explicit LF so byte comparisons are platform-independent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return path


def test_fixture_repo_is_scanned_with_expected_files() -> None:
    scan = scan_repository(FIXTURES)
    paths = {item.rel_path for item in scan.files}
    assert {"app/service.py", "app/parser.py", "app/pipeline.py", "app/broken.py"} <= paths
    assert "README.md" in paths
    assert "config.toml" in paths
    by_path = {item.rel_path: item for item in scan.files}
    assert by_path["app/service.py"].language == "python"
    assert by_path["README.md"].language == "markdown"
    assert by_path["config.toml"].language == "text"
    assert scan.total_bytes > 0


def test_ignored_directories_and_unsupported_files_are_skipped(tmp_path: Path) -> None:
    write(tmp_path / "keep.py", "def keep():\n    return 1\n")
    write(tmp_path / ".git" / "config.py", "x = 1\n")
    write(tmp_path / "__pycache__" / "cached.py", "x = 1\n")
    write(tmp_path / "node_modules" / "dep.py", "x = 1\n")
    write(tmp_path / "logo.png", "not really a png")
    write(tmp_path / "notes.xyz", "unknown extension")
    write(tmp_path / ".hidden.py", "x = 1\n")

    scan = scan_repository(tmp_path)
    paths = {item.rel_path for item in scan.files}
    assert paths == {"keep.py"}
    reasons = dict(scan.skipped)
    assert reasons[".git"] == "ignored_directory"
    # Binary-ish suffixes are filtered before the type allow-list is consulted.
    assert reasons["logo.png"] == "ignored_suffix"
    assert reasons["notes.xyz"] == "unsupported_type"
    assert reasons[".hidden.py"] == "hidden_file"


def test_binary_content_is_detected_even_with_py_suffix(tmp_path: Path) -> None:
    (tmp_path / "blob.py").write_bytes(b"def x():\x00\n    return 1\n")
    write(tmp_path / "ok.py", "def ok():\n    return 1\n")
    scan = scan_repository(tmp_path)
    assert {item.rel_path for item in scan.files} == {"ok.py"}
    assert dict(scan.skipped)["blob.py"] == "binary_content"


def test_oversized_file_is_skipped_with_reason(tmp_path: Path) -> None:
    write(tmp_path / "big.py", "x = 1\n" * 100)
    scan = scan_repository(tmp_path, IngestLimits(max_file_bytes=50))
    assert scan.files == []
    assert dict(scan.skipped)["big.py"] == "file_too_large"


def test_file_count_limit_raises(tmp_path: Path) -> None:
    for index in range(5):
        write(tmp_path / f"m{index}.py", "x = 1\n")
    with pytest.raises(IngestLimitExceeded):
        scan_repository(tmp_path, IngestLimits(max_files=2))


def test_symlinks_are_never_followed(tmp_path: Path) -> None:
    write(tmp_path / "real.py", "x = 1\n")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("secret = 1\n", encoding="utf-8")
    link = tmp_path / "link.py"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks requires privileges on this machine")
    scan = scan_repository(tmp_path)
    assert {item.rel_path for item in scan.files} == {"real.py"}
    assert dict(scan.skipped)["link.py"] == "symlink_not_followed"


def test_read_bytes_rejects_paths_outside_the_root(tmp_path: Path) -> None:
    write(tmp_path / "ok.py", "x = 1\n")
    assert read_bytes_checked(tmp_path, "ok.py") == b"x = 1\n"
    with pytest.raises(UnsafePathError):
        read_bytes_checked(tmp_path, "../outside.py")
    with pytest.raises(UnsafePathError):
        read_bytes_checked(tmp_path, "C:/Windows/win.ini")
    with pytest.raises(UnsafePathError):
        read_bytes_checked(tmp_path, "/etc/passwd")


def test_ensure_within_root_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        ensure_within_root(tmp_path, tmp_path / ".." / "elsewhere")
    assert ensure_within_root(tmp_path, tmp_path / "inner").is_relative_to(tmp_path)


def test_scan_rejects_non_directory(tmp_path: Path) -> None:
    target = write(tmp_path / "file.py", "x = 1\n")
    with pytest.raises(UnsafePathError):
        scan_repository(target)


def test_non_git_directory_has_no_commit(tmp_path: Path) -> None:
    write(tmp_path / "x.py", "x = 1\n")
    assert resolve_git_commit(tmp_path) is None
