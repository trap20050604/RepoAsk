"""Qdrant 本地持久化向量库。

三条硬约束（对应功能要求 B 与 C）：

1. **集合按“模型 + 维度”命名**：换 embedding 模型或维度时物理上落到另一个集合，
   旧向量不可能被误用。
2. **写入与检索都强制带 repo_id 与 commit**：同一集合里可以存放多个版本，
   但任何一次检索都只能返回指定版本的点。
3. **集合维度必须与当前模型一致**：打开集合时比对维度，不一致直接报错，
   而不是让相似度在错误的维度上静默计算。

本地模式（``QdrantClient(path=...)``）的限制已在 ``docs/handoff.md`` 记录：
没有 ``search()``（用 ``query_points``），``create_payload_index`` 在本地是 no-op。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel, ConfigDict
from qdrant_client import QdrantClient, models

from repoask.errors import VectorStoreError
from repoask.models import Chunk

COLLECTION_PREFIX = "repoask"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def collection_name(model: str, dimensions: int) -> str:
    """由模型名与维度派生的集合名，保证不同模型不会共用集合。"""
    slug = _SLUG_RE.sub("_", (model or "unknown").lower()).strip("_")[:40] or "unknown"
    return f"{COLLECTION_PREFIX}_{slug}_{int(dimensions)}"


def point_id(chunk_id: str) -> str:
    """由 chunk_id 派生稳定 UUID，重复导入幂等（同一 chunk 覆盖同一点）。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))


class VectorHit(BaseModel):
    """向量检索的一个命中；只带标识与分数，正文从索引中回取。"""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    score: float
    payload: dict[str, object]


def _vector_size(info: object) -> int:
    params = getattr(getattr(info, "config", None), "params", None)
    vectors = getattr(params, "vectors", None)
    if vectors is None:
        raise VectorStoreError("集合没有可用的向量配置")
    size = getattr(vectors, "size", None)
    if size is not None:
        return int(size)
    if isinstance(vectors, dict) and vectors:
        first = next(iter(vectors.values()))
        return int(getattr(first, "size"))
    raise VectorStoreError("无法从集合配置中读取向量维度")


class VectorStore:
    """一个集合的读写封装；用完必须 ``close()``（本地模式会持有目录锁）。"""

    def __init__(self, path: str | Path, *, collection: str, dimensions: int) -> None:
        if dimensions <= 0:
            raise VectorStoreError("向量维度必须为正数")
        self.path = Path(path).resolve()
        self.path.mkdir(parents=True, exist_ok=True)
        self.collection = collection
        self.dimensions = int(dimensions)
        self._client = QdrantClient(path=str(self.path))
        try:
            self._ensure_collection()
        except Exception:
            self._client.close()
            raise

    def _ensure_collection(self) -> None:
        if self._client.collection_exists(self.collection):
            existing = _vector_size(self._client.get_collection(self.collection))
            if existing != self.dimensions:
                raise VectorStoreError(
                    f"集合 {self.collection!r} 已是 {existing} 维，"
                    f"与当前要求的 {self.dimensions} 维不一致；请重建索引"
                )
            return
        self._client.create_collection(
            collection_name=self.collection,
            vectors_config=models.VectorParams(
                size=self.dimensions, distance=models.Distance.COSINE
            ),
        )

    # -- 写 ---------------------------------------------------------------
    def upsert_chunks(
        self,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]],
        *,
        repo_id: str,
        commit: str,
        model: str,
        batch_size: int = 128,
    ) -> int:
        """写入一批分块向量；拒绝混入其他版本，拒绝维度不符。"""
        if len(chunks) != len(vectors):
            raise VectorStoreError(
                f"分块数 {len(chunks)} 与向量数 {len(vectors)} 不一致"
            )
        points: list[models.PointStruct] = []
        for chunk, vector in zip(chunks, vectors):
            if len(vector) != self.dimensions:
                raise VectorStoreError(
                    f"{chunk.chunk_id} 的向量是 {len(vector)} 维，集合要求 {self.dimensions} 维"
                )
            if chunk.repo_id != repo_id or chunk.commit != commit:
                raise VectorStoreError(
                    f"拒绝写入其他版本的分块：{chunk.repo_id}@{chunk.commit}"
                )
            points.append(
                models.PointStruct(
                    id=point_id(chunk.chunk_id),
                    vector=list(vector),
                    payload={
                        "repo_id": chunk.repo_id,
                        "commit": chunk.commit,
                        "path": chunk.path,
                        "symbol": chunk.symbol,
                        "kind": chunk.kind,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "content_hash": chunk.content_hash,
                        "chunk_id": chunk.chunk_id,
                        "embedding_model": model,
                    },
                )
            )
        for start in range(0, len(points), batch_size):
            self._client.upsert(
                collection_name=self.collection,
                points=points[start : start + batch_size],
                wait=True,
            )
        return len(points)

    # -- 读 ---------------------------------------------------------------
    def search(
        self,
        vector: Sequence[float],
        *,
        repo_id: str,
        commit: str,
        top_k: int = 6,
    ) -> list[VectorHit]:
        """按仓库与 commit 过滤后的向量检索。"""
        if len(vector) != self.dimensions:
            raise VectorStoreError(
                f"查询向量是 {len(vector)} 维，集合要求 {self.dimensions} 维"
            )
        query_filter = models.Filter(
            must=[
                models.FieldCondition(key="repo_id", match=models.MatchValue(value=repo_id)),
                models.FieldCondition(key="commit", match=models.MatchValue(value=commit)),
            ]
        )
        result = self._client.query_points(
            collection_name=self.collection,
            query=list(vector),
            query_filter=query_filter,
            limit=max(1, top_k),
            with_payload=True,
        )
        return [
            VectorHit(
                chunk_id=str(getattr(point, "id", "")),
                score=float(getattr(point, "score", 0.0)),
                payload=dict(getattr(point, "payload", None) or {}),
            )
            for point in result.points
        ]

    def delete_version(self, *, repo_id: str, commit: str) -> None:
        """删除某个版本的向量（重新导入同一 commit 前调用）。"""
        query_filter = models.Filter(
            must=[
                models.FieldCondition(key="repo_id", match=models.MatchValue(value=repo_id)),
                models.FieldCondition(key="commit", match=models.MatchValue(value=commit)),
            ]
        )
        self._client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(filter=query_filter),
        )

    def count(self) -> int:
        return int(self._client.count(self.collection).count)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "VectorStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
