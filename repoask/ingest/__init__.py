"""Repository ingestion (local now, GitHub in stage 4)."""

from repoask.ingest.local import (
    GitWorktreeState,
    IngestLimits,
    ScanResult,
    ScannedFile,
    ensure_within_root,
    inspect_git_worktree,
    is_indexable_path,
    read_bytes_checked,
    resolve_git_commit,
    scan_repository,
)

__all__ = [
    "GitWorktreeState",
    "IngestLimits",
    "ScanResult",
    "ScannedFile",
    "ensure_within_root",
    "inspect_git_worktree",
    "is_indexable_path",
    "read_bytes_checked",
    "resolve_git_commit",
    "scan_repository",
]
