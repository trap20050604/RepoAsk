"""Safe, read-only scanning of a *local* repository directory.

Safety rules enforced here (the same rules later apply to GitHub imports):
  * only regular files under the resolved root are read - never a path outside it;
  * symlinks are never followed, so a link pointing outside the repository cannot
    leak neighbouring files onto the index;
  * obviously irrelevant or binary files are skipped with a recorded reason;
  * file count, per-file size, total size and elapsed time are bounded.

Nothing in the target repository is imported, executed or evaluated. Only bytes
are read.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from repoask.errors import IngestLimitExceeded, UnsafePathError
from repoask.parse import language_for_path

IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        "build",
        "dist",
        ".idea",
        ".vscode",
        ".tox",
        ".nox",
    }
)

IGNORED_SUFFIXES = frozenset(
    {
        ".pyc",
        ".pyo",
        ".pyd",
        ".so",
        ".dll",
        ".exe",
        ".dylib",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".whl",
        ".jar",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".bmp",
        ".pdf",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".mp3",
        ".mp4",
        ".mov",
        ".avi",
        ".sqlite",
        ".db",
        ".lock",
    }
)

# Files without an extension that should still be considered (checked by name).
_BINARY_SNIFF_BYTES = 8192


class IngestLimits(BaseModel):
    """Hard ceilings so one repository cannot exhaust memory or wall-clock time."""

    max_files: int = Field(default=2_000, ge=1)
    max_file_bytes: int = Field(default=1_000_000, ge=1)
    max_total_bytes: int = Field(default=50_000_000, ge=1)
    max_seconds: float = Field(default=60.0, gt=0)


@dataclass(frozen=True)
class ScannedFile:
    rel_path: str
    size_bytes: int
    language: str


@dataclass
class ScanResult:
    root: Path
    files: list[ScannedFile] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    total_bytes: int = 0
    elapsed_ms: int = 0

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


def _is_binary(head: bytes) -> bool:
    return b"\x00" in head


def resolve_git_commit(root: Path, timeout_seconds: float = 10.0) -> str | None:
    """Return the HEAD commit of ``root`` when it is exactly a git work tree root.

    A sub-directory of a git repository is *not* accepted: labelling files with a
    commit that belongs to a parent repository would be misleading.
    """
    try:
        top_level = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not head or not top_level:
        return None
    try:
        if Path(top_level).resolve() != root:
            return None
    except OSError:
        return None
    return head


def ensure_within_root(root: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and fail loudly if it is not inside ``root``."""
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise UnsafePathError(f"path escapes repository root: {candidate}")
    return resolved


def scan_repository(root: str | Path, limits: IngestLimits | None = None) -> ScanResult:
    """Walk ``root`` and return the indexable files, recording every skip reason."""
    limits = limits or IngestLimits()
    root_path = Path(root)
    if not root_path.is_dir():
        raise UnsafePathError(f"not a directory: {root_path}")
    root_path = root_path.resolve()
    started = time.perf_counter()

    result = ScanResult(root=root_path)
    stack: list[tuple[Path, str]] = [(root_path, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            result.skipped.append((prefix or ".", "unreadable_directory"))
            continue
        for entry in entries:
            rel_path = f"{prefix}{entry.name}"
            if time.perf_counter() - started > limits.max_seconds:
                raise IngestLimitExceeded(
                    f"repository scan exceeded {limits.max_seconds}s after "
                    f"{len(result.files)} files"
                )
            try:
                if entry.is_symlink():
                    # Never followed: a link may point outside the repository.
                    result.skipped.append((rel_path, "symlink_not_followed"))
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in IGNORED_DIRECTORIES:
                        result.skipped.append((rel_path, "ignored_directory"))
                        continue
                    stack.append((Path(entry.path), f"{rel_path}/"))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    result.skipped.append((rel_path, "not_a_regular_file"))
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                result.skipped.append((rel_path, "unreadable"))
                continue

            if entry.name.startswith(".") and entry.name not in {".gitignore", ".env.example"}:
                result.skipped.append((rel_path, "hidden_file"))
                continue
            if Path(entry.name).suffix.lower() in IGNORED_SUFFIXES:
                result.skipped.append((rel_path, "ignored_suffix"))
                continue
            language = language_for_path(rel_path)
            if language is None:
                result.skipped.append((rel_path, "unsupported_type"))
                continue
            if stat.st_size > limits.max_file_bytes:
                result.skipped.append((rel_path, "file_too_large"))
                continue

            # Sniff for NUL bytes: a mislabelled binary should not enter the index.
            try:
                with open(entry.path, "rb") as handle:
                    if _is_binary(handle.read(_BINARY_SNIFF_BYTES)):
                        result.skipped.append((rel_path, "binary_content"))
                        continue
            except OSError:
                result.skipped.append((rel_path, "unreadable"))
                continue

            if len(result.files) + 1 > limits.max_files:
                raise IngestLimitExceeded(
                    f"repository has more than {limits.max_files} indexable files"
                )
            if result.total_bytes + stat.st_size > limits.max_total_bytes:
                raise IngestLimitExceeded(
                    f"indexable content exceeds {limits.max_total_bytes} bytes"
                )
            result.files.append(
                ScannedFile(rel_path=rel_path, size_bytes=stat.st_size, language=language)
            )
            result.total_bytes += stat.st_size

    result.files.sort(key=lambda item: item.rel_path)
    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result


def read_bytes_checked(root: Path, rel_path: str) -> bytes:
    """Read one scanned file, refusing any path that escapes ``root``."""
    if rel_path.startswith("/") or rel_path.startswith("\\") or ".." in Path(rel_path).parts:
        raise UnsafePathError(f"unsafe relative path: {rel_path}")
    target = ensure_within_root(root, root / rel_path)
    if not target.is_file():
        raise UnsafePathError(f"not a regular file: {rel_path}")
    return target.read_bytes()
