"""Small factories for building chunks/evidence in tests.

Kept inside the package so tests do not need their own import machinery; this
module contains no production logic.
"""

from __future__ import annotations

import hashlib
import math
import time

from repoask.embed.client import EmbeddingResponse
from repoask.models import Chunk, Evidence, ParentContext, content_sha256
from repoask.retrieve.keyword import tokenize

DEFAULT_REPO_ID = "demo-repo"
DEFAULT_COMMIT = "0123456789abcdef0123456789abcdef01234567"


class StubEmbedder:
    """确定性桩向量：把 token 哈希到固定维度，共享 token 越多相似度越高。

    它不是语义模型，只用于离线测试向量链路的接线与约束；排序结果没有质量含义。
    """

    def __init__(self, model: str = "stub-embedding-v1", dimensions: int = 16) -> None:
        self.model = model
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> EmbeddingResponse:
        started = time.perf_counter()
        vectors = [self._vector(text) for text in texts]
        return EmbeddingResponse(
            vectors=vectors,
            model=self.model,
            dimensions=self.dimensions,
            prompt_tokens=None,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in tokenize(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            slot = digest[0] % self.dimensions
            sign = 1.0 if digest[1] % 2 == 0 else -1.0
            vector[slot] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            vector[0] = 1.0
            norm = 1.0
        return [value / norm for value in vector]


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
