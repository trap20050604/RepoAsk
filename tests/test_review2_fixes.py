"""第二轮独立审查（基线 c989711）四项缺陷的复现与回归测试。

每个用例先在未修复的实现上失败，修复后通过：
1. 集合名只由 slug 派生，`org/model` 与 `org-model` 撞名，跨模型覆盖向量；
2. 同版本重新分块后旧点残留，旧点占满候选导致“假空检索”；
3. 权重置 0 时该路候选仍以 0 分进入结果；
4. 查询缺失的向量库/集合时会静默创建空库并返回空结果。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from repoask.errors import VectorStoreError
from repoask.index import VectorRef
from repoask.pipeline import build_local_index, search_vector
from repoask.retrieve.fusion import fuse_rrf
from repoask.retrieve.keyword import SearchHit
from repoask.testing import DEFAULT_REPO_ID, StubEmbedder, make_chunk
from repoask.vector.store import VectorStore, collection_name

FIXTURES = Path(__file__).parent / "fixtures" / "demo_repo"


def _build(vector_dir: Path, embedder: StubEmbedder, **kwargs):
    return build_local_index(
        FIXTURES,
        repo_id=DEFAULT_REPO_ID,
        embedder=embedder,
        vector_path=vector_dir,
        **kwargs,
    )


# --------------------------------------------------------------------------
# 1. 集合名必须区分“slug 后相同”的不同模型
# --------------------------------------------------------------------------
def test_collection_name_uses_a_hash_of_the_full_model_id() -> None:
    assert collection_name("org/model", 8) != collection_name("org-model", 8)
    long_a = "a" * 60 + "-one"
    long_b = "a" * 60 + "-two"
    assert collection_name(long_a, 8) != collection_name(long_b, 8)


def test_two_models_never_share_a_collection(tmp_path: Path) -> None:
    first = _build(tmp_path, StubEmbedder(model="org/model"))
    collection_a = first.index.vectors.collection
    second = _build(tmp_path, StubEmbedder(model="org-model"))
    assert second.index.vectors.collection != collection_a

    # A 的集合里必须仍然只有 A 模型写下的点（若撞名，会被 B 覆盖）
    embedder_a = StubEmbedder(model="org/model")
    with VectorStore(
        tmp_path, collection=collection_a, dimensions=embedder_a.dimensions
    ) as store:
        assert store.count() == len(first.index.chunks)
        hits = store.search(
            embedder_a.embed(["load_tasks"]).vectors[0],
            repo_id=first.index.repo_id,
            commit=first.index.commit,
            top_k=5,
        )
    assert hits
    assert {hit.payload["embedding_model"] for hit in hits} == {"org/model"}


def test_upsert_refuses_points_written_by_another_model(tmp_path: Path) -> None:
    chunks = [make_chunk("def a():\n    return 1", path="app/a.py")]
    with VectorStore(
        tmp_path, collection=collection_name("model-one", 8), dimensions=8
    ) as store:
        store.upsert_chunks(
            chunks,
            [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]],
            repo_id=DEFAULT_REPO_ID,
            commit=chunks[0].commit,
            model="model-one",
        )
        with pytest.raises(VectorStoreError):
            store.upsert_chunks(
                chunks,
                [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]],
                repo_id=DEFAULT_REPO_ID,
                commit=chunks[0].commit,
                model="model-two",
            )


# --------------------------------------------------------------------------
# 2. 同版本重建必须替换旧点，不能留下“幽灵点”
# --------------------------------------------------------------------------
def _repo_with_one_big_function(base: Path) -> Path:
    """构造一个默认窗口下只产生 1 个分块、缩小窗口后产生多个分块的仓库。"""
    repo = base / "repo"
    (repo / "app").mkdir(parents=True)
    body = "\n".join(f"    total += {index}" for index in range(100))
    (repo / "app" / "big.py").write_text(
        f"def big():\n    total = 0\n{body}\n    return total\n", encoding="utf-8"
    )
    return repo


def test_rebuild_of_same_version_replaces_old_points(tmp_path: Path) -> None:
    embedder = StubEmbedder()
    repo = _repo_with_one_big_function(tmp_path)
    store_dir = tmp_path / "store"

    first = build_local_index(
        repo, repo_id="big-repo", embedder=embedder, vector_path=store_dir
    )
    second = build_local_index(
        repo,
        repo_id="big-repo",
        embedder=embedder,
        vector_path=store_dir,
        max_chunk_lines=40,
        max_chunk_chars=800,
    )
    assert first.index.commit == second.index.commit, "两次构建必须是同一内容版本"
    assert len(first.index.chunks) == 1
    assert len(second.index.chunks) > 1

    with VectorStore(
        store_dir,
        collection=second.index.vectors.collection,
        dimensions=embedder.dimensions,
    ) as store:
        assert store.count() == len(second.index.chunks), "旧分块的点没有被替换"

    hits = search_vector(second.index, "big total", embedder, top_k=1)
    assert hits, "旧点占满候选会导致假空检索"


# --------------------------------------------------------------------------
# 3. 零权重必须真正移除一路
# --------------------------------------------------------------------------
def _hit(text: str, path: str, start: int, score: float, source: str) -> SearchHit:
    return SearchHit(
        chunk=make_chunk(text, path=path, start_line=start),
        score=score,
        source=source,
    )


KEYWORD_HITS = [
    _hit("def a(): pass", "app/a.py", 1, 9.0, "keyword"),
    _hit("def b(): pass", "app/b.py", 2, 7.0, "keyword"),
]
VECTOR_HITS = [
    _hit("def c(): pass", "app/c.py", 3, 0.9, "vector"),
    _hit("def b(): pass", "app/b.py", 2, 0.8, "vector"),
]


def test_zero_weight_source_is_removed_completely() -> None:
    fused = fuse_rrf(KEYWORD_HITS, VECTOR_HITS, top_k=10, weights={"vector": 0.0})
    assert [hit.chunk.path for hit in fused] == ["app/a.py", "app/b.py"]
    assert all("vector" not in hit.ranks for hit in fused)
    assert all("vector" not in hit.source_scores for hit in fused)


def test_all_weights_zero_returns_nothing() -> None:
    fused = fuse_rrf(KEYWORD_HITS, VECTOR_HITS, top_k=10, weights={"keyword": 0.0, "vector": 0.0})
    assert fused == []


# --------------------------------------------------------------------------
# 4. 查询路径不得创建缺失的向量库或集合
# --------------------------------------------------------------------------
def test_query_rejects_missing_vector_directory(tmp_path: Path) -> None:
    embedder = StubEmbedder()
    build = _build(tmp_path / "store", embedder)
    missing = tmp_path / "absent"
    build.index.vectors = VectorRef(path=str(missing), collection=build.index.vectors.collection)

    with pytest.raises(VectorStoreError):
        search_vector(build.index, "load_tasks", embedder)
    assert not missing.exists(), "查询不应创建缺失的向量目录"


def test_query_rejects_missing_collection_without_creating_it(tmp_path: Path) -> None:
    embedder = StubEmbedder()
    store_dir = tmp_path / "store"
    build = _build(store_dir, embedder)
    missing = "collection_that_does_not_exist"
    build.index.vectors = VectorRef(path=str(store_dir), collection=missing)

    with pytest.raises(VectorStoreError):
        search_vector(build.index, "load_tasks", embedder)

    client = QdrantClient(path=str(store_dir))
    try:
        assert not client.collection_exists(missing), "查询不应创建集合"
    finally:
        client.close()
