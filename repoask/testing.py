"""Small factories for building chunks/evidence in tests.

Kept inside the package so tests do not need their own import machinery; this
module contains no production logic.
"""

from __future__ import annotations

from repoask.models import Chunk, Evidence, ParentContext, content_sha256

DEFAULT_REPO_ID = "demo-repo"
DEFAULT_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def make_chunk(
    content: str,
    *,
    repo_id: str = DEFAULT_REPO_ID,
    commit: str = DEFAULT_COMMIT,
    path: str = "app/module.py",
    symbol: str | None = None,
    kind: str = "function",
    start_line: int = 1,
    parent_context: ParentContext | None = None,
) -> Chunk:
    digest = content_sha256(content)
    end_line = start_line + len(content.split("\n")) - 1
    return Chunk(
        repo_id=repo_id,
        commit=commit,
        path=path,
        symbol=symbol,
        start_line=start_line,
        end_line=end_line,
        content_hash=digest,
        chunk_id=f"{repo_id}:{commit[:12]}:{path}:{kind}:{start_line}-{end_line}:{digest[:10]}",
        kind=kind,  # type: ignore[arg-type]
        content=content,
        parent_context=parent_context,
        language="python",
    )


def make_evidence(chunk: Chunk, evidence_id: str = "E1", *, score: float = 1.0) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        repo_id=chunk.repo_id,
        commit=chunk.commit,
        path=chunk.path,
        symbol=chunk.symbol,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        content=chunk.content,
        content_hash=chunk.content_hash,
        score=score,
        parent_context=chunk.parent_context,
    )
