"""RRF 融合测试（离线）。

证明融合只依赖名次、能正确合并两路、权重与边界行为符合预期；
不涉及任何模型调用。
"""

from __future__ import annotations

import pytest

from repoask.retrieve.fusion import DEFAULT_RRF_K, describe_fusion, fuse_rrf
from repoask.retrieve.keyword import SearchHit
from repoask.testing import make_chunk


def hit(content: str, *, path: str, start_line: int, score: float, source: str) -> SearchHit:
    return SearchHit(
        chunk=make_chunk(content, path=path, start_line=start_line),
        score=score,
        source=source,
    )


KEYWORD = [
    hit("def a(): pass", path="app/a.py", start_line=1, score=9.0, source="keyword"),
    hit("def b(): pass", path="app/b.py", start_line=2, score=7.0, source="keyword"),
    hit("def c(): pass", path="app/c.py", start_line=3, score=5.0, source="keyword"),
]
VECTOR = [
    hit("def b(): pass", path="app/b.py", start_line=2, score=0.9, source="vector"),
    hit("def d(): pass", path="app/d.py", start_line=4, score=0.8, source="vector"),
]


def test_rrf_score_uses_ranks_not_raw_scores() -> None:
    fused = fuse_rrf(KEYWORD, VECTOR, top_k=10)
    by_path = {item.chunk.path: item for item in fused}
    # b 在关键词里第 2、向量里第 1
    expected_b = 1 / (DEFAULT_RRF_K + 2) + 1 / (DEFAULT_RRF_K + 1)
    assert by_path["app/b.py"].score == pytest.approx(round(expected_b, 6))
    # a 只在关键词第 1
    assert by_path["app/a.py"].score == pytest.approx(round(1 / (DEFAULT_RRF_K + 1), 6))
    assert by_path["app/b.py"].score > by_path["app/a.py"].score


def test_fusion_merges_duplicates_and_records_provenance() -> None:
    fused = fuse_rrf(KEYWORD, VECTOR, top_k=10)
    b = next(item for item in fused if item.chunk.path == "app/b.py")
    assert b.source == "hybrid"
    assert b.ranks == {"keyword": 2, "vector": 1}
    assert b.source_scores["keyword"] == 7.0
    assert b.source_scores["vector"] == 0.9
    # 同一分块只出现一次
    assert len([item for item in fused if item.chunk.path == "app/b.py"]) == 1


def test_result_order_follows_fused_score() -> None:
    fused = fuse_rrf(KEYWORD, VECTOR, top_k=10)
    scores = [item.score for item in fused]
    assert scores == sorted(scores, reverse=True)
    assert fused[0].chunk.path == "app/b.py"


def test_top_k_truncates_after_fusion() -> None:
    fused = fuse_rrf(KEYWORD, VECTOR, top_k=2)
    assert len(fused) == 2


def test_zero_weight_removes_one_source() -> None:
    fused = fuse_rrf(KEYWORD, VECTOR, top_k=10, weights={"vector": 0.0})
    assert [item.chunk.path for item in fused[:3]] == ["app/a.py", "app/b.py", "app/c.py"]
    assert "vector" not in fused[0].ranks


def test_empty_sources_are_handled() -> None:
    assert fuse_rrf([], [], top_k=5) == []
    keyword_only = fuse_rrf(KEYWORD, [], top_k=5)
    assert [item.chunk.path for item in keyword_only] == ["app/a.py", "app/b.py", "app/c.py"]
    assert all(item.source == "hybrid" for item in keyword_only)
    vector_only = fuse_rrf([], VECTOR, top_k=5)
    assert [item.chunk.path for item in vector_only] == ["app/b.py", "app/d.py"]


def test_invalid_parameters_are_rejected() -> None:
    with pytest.raises(ValueError):
        fuse_rrf(KEYWORD, VECTOR, k=0)
    with pytest.raises(ValueError):
        fuse_rrf(KEYWORD, VECTOR, top_k=0)


def test_describe_fusion_counts_provenance() -> None:
    fused = fuse_rrf(KEYWORD, VECTOR, top_k=10)
    text = describe_fusion(fused)
    assert "两路都命中 1 条" in text
    assert "仅关键词 2 条" in text
    assert "仅向量 1 条" in text
    assert describe_fusion([]) == "融合后没有候选"
