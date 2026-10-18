"""Repository ingestion (local now, GitHub in stage 4)."""

from repoask.ingest.local import (
    IngestLimits,
    ScanResult,
    ScannedFile,
    ensure_within_root,
    read_bytes_checked,
    resolve_git_commit,
    scan_repository,
)

__all__ = [
    "IngestLimits",
    "ScanResult",
    "ScannedFile",
    "ensure_within_root",
    "read_bytes_checked",
    "resolve_git_commit",
    "scan_repository",
]
