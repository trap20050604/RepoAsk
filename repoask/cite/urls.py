"""GitHub blob URL construction and input validation.

Only ``https://github.com/<owner>/<repo>`` inputs are accepted. This module is
also the single place that turns (commit, path, line range) into the clickable
link shown to the user, so a malformed path cannot sneak into an href.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

REPO_URL_PATTERN = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")


class InvalidRepoUrl(ValueError):
    """The repository URL is not a public GitHub HTTPS URL."""


class InvalidPath(ValueError):
    """The repository-relative path is absolute or contains traversal."""


def parse_github_repo_url(url: str) -> tuple[str, str]:
    """Return ``(owner, repo)`` or raise :class:`InvalidRepoUrl`."""
    candidate = (url or "").strip()
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        raise InvalidRepoUrl(f"only https://github.com/<owner>/<repo> is allowed: {url!r}")
    if parsed.query or parsed.fragment or parsed.params:
        raise InvalidRepoUrl(f"repository URL must not carry a query/fragment: {url!r}")
    match = REPO_URL_PATTERN.match(candidate)
    if not match:
        raise InvalidRepoUrl(f"cannot parse owner/repo from {url!r}")
    owner, repo = match.group(1), match.group(2)
    if owner in {".", ".."} or repo in {".", ".."}:
        raise InvalidRepoUrl(f"suspicious owner/repo in {url!r}")
    return owner, repo


def normalize_repo_url(url: str) -> str:
    owner, repo = parse_github_repo_url(url)
    return f"https://github.com/{owner}/{repo}"


def validate_relative_path(path: str) -> str:
    """Reject absolute paths, drive letters and traversal; return a clean path."""
    cleaned = path.replace("\\", "/").strip()
    if not cleaned:
        raise InvalidPath("empty path")
    if cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned):
        raise InvalidPath(f"absolute path not allowed: {path!r}")
    parts = [part for part in cleaned.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise InvalidPath(f"path traversal not allowed: {path!r}")
    return "/".join(parts)


def validate_commit(commit: str) -> str:
    if not COMMIT_PATTERN.match(commit or ""):
        raise ValueError(f"not a hex commit id: {commit!r}")
    return commit.lower()


def github_blob_url(
    repo_url: str,
    commit: str,
    path: str,
    start_line: int,
    end_line: int | None = None,
) -> str:
    """Build a line-anchored blob link for a fixed commit.

    ``end_line`` equal to ``start_line`` produces ``#L7`` rather than ``#L7-L7``.
    """
    owner, repo = parse_github_repo_url(repo_url)
    safe_path = validate_relative_path(path)
    safe_commit = validate_commit(commit)
    if start_line < 1:
        raise ValueError("start_line must be >= 1")
    anchor = f"L{start_line}"
    if end_line is not None and end_line > start_line:
        anchor = f"L{start_line}-L{end_line}"
    return f"https://github.com/{owner}/{repo}/blob/{safe_commit}/{safe_path}#{anchor}"
