"""Project-wide exception types with explicit, user-facing failure reasons."""

from __future__ import annotations


class RepoAskError(Exception):
    """Base class for every error this project raises deliberately."""


class IngestError(RepoAskError):
    """Repository could not be read or violates an ingestion limit."""


class IngestLimitExceeded(IngestError):
    """A size/count/time budget was exceeded while reading the repository."""


class UnsafePathError(IngestError):
    """A path escaped the repository root (absolute, traversal or symlink)."""


class ParseError(RepoAskError):
    """A file could not be decoded or parsed at all."""


class ConfigError(RepoAskError):
    """Required configuration is missing or malformed."""


class IndexVersionMismatch(RepoAskError):
    """An index was built for a different repository or commit than requested."""


class LLMUnavailable(RepoAskError):
    """The chat model is not configured, or the endpoint failed."""


class CitationError(RepoAskError):
    """A citation could not be resolved to real evidence."""
