"""RRF（Reciprocal Rank Fusion）：按排名合并多路检索结果。

为什么按排名而不是按分数：BM25 分数与余弦相似度不在同一量纲，直接相加/加权
会把“分数量级大的那一路”变成唯一决定因素。RRF 只使用名次：

    score(d) = Σ_source  weight_source / (k + rank_source(d))

k 默认 60（原论文的经验值）。同一分块在多路命中时只保留一条，记录各路名次与
分数，便于对照实验复盘。
"""

from __future__ import annotations

from repoask.retrieve.keyword import SearchHit

DEFAULT_RRF_K = 60
DEFAULT_WEIGHTS: dict[str, float] = {"keyword": 1.0, "vector": 1.0}


def fuse_rrf(
    keyword_hits: list[SearchHit],
    vector_hits: list[SearchHit],
    *,
    top_k: int = 6,
    k: int = DEFAULT_RRF_K,
    weights: dict[str, float] | None = None,
) -> list[SearchHit]:
    """融合关键词与向量两路结果，返回 ``source="hybrid"`` 的候选。

    每个分块记录：融合分数、各路名次（``ranks``）、各路原始分数
    （``source_scores``）、以及关键词命中词（``matched_terms``）。
    """
    if k <= 0:
        raise ValueError("k must be positive")
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    merged_weights = {**DEFAULT_WEIGHTS, **(weights or {})}

    merged: dict[str, dict[str, object]] = {}
    for source, hits in (("keyword", keyword_hits), ("vector", vector_hits)):
        for rank, hit in enumerate(hits, start=1):
            chunk_id = hit.chunk.chunk_id
            entry = merged.get(chunk_id)
            if entry is None:
                entry = {
                    "chunk": hit.chunk,
                    "score": 0.0,
                    "ranks": {},
                    "scores": {},
                    "terms": [],
                }
                merged[chunk_id] = entry
            entry["score"] = float(entry["score"]) + merged_weights.get(source, 1.0) / (k + rank)
            ranks: dict[str, int] = entry["ranks"]  # type: ignore[assignment]
            scores: dict[str, float] = entry["scores"]  # type: ignore[assignment]
            ranks[source] = rank
            scores[source] = hit.score
            if source == "keyword":
                terms: list[str] = entry["terms"]  # type: ignore[assignment]
                terms.extend(hit.matched_terms)

    def sort_key(entry: dict[str, object]) -> tuple[float, int, str, int]:
        chunk = entry["chunk"]
        ranks: dict[str, int] = entry["ranks"]  # type: ignore[assignment]
        return (
            -float(entry["score"]),
            min(ranks.values()) if ranks else 0,
            getattr(chunk, "path", ""),
            getattr(chunk, "start_line", 0),
        )

    fused: list[SearchHit] = []
    for entry in sorted(merged.values(), key=sort_key)[:top_k]:
        fused.append(
            SearchHit(
                chunk=entry["chunk"],  # type: ignore[arg-type]
                score=round(float(entry["score"]), 6),
                matched_terms=sorted({term for term in entry["terms"]}),  # type: ignore[union-attr]
                source="hybrid",
                ranks=dict(entry["ranks"]),  # type: ignore[arg-type]
                source_scores=dict(entry["scores"]),  # type: ignore[arg-type]
            )
        )
    return fused


def describe_fusion(hits: list[SearchHit]) -> str:
    """给 CLI/轨迹用的一句话摘要。"""
    if not hits:
        return "融合后没有候选"
    only_keyword = sum(1 for hit in hits if set(hit.ranks) == {"keyword"})
    only_vector = sum(1 for hit in hits if set(hit.ranks) == {"vector"})
    both = sum(1 for hit in hits if len(hit.ranks) == 2)
    return (
        f"融合 {len(hits)} 条：两路都命中 {both} 条，仅关键词 {only_keyword} 条，"
        f"仅向量 {only_vector} 条"
    )
