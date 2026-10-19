"""向量存储与向量检索的行为测试（全部离线，使用确定性桩向量）。

这些测试证明的是**接线与约束**，不是语义质量：
它们不调用真实 embedding 服务，也不对检索效果下结论。
真实调用的结果记录在 docs/reports/stage-2.md。
"""

from __future__ import annotations

import hashlib
import math
import time
from pathlib import Path

import pytest

from repoask.embed.client import EmbeddingResponse
from repoask.errors import VectorStoreError
from repoask.pipeline import ask, build_local_index, search_vector
from repoask.retrieve.keyword import tokenize
from repoask.testing import DEFAULT_REPO_ID, make_chunk
from repoask.vector.store import VectorStore, collection_name

FIXTURES = Path(__file__).parent / "fixtures" / "demo_repo"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


class StubEmbedder:
    """把 token 哈希到固定维度的确定性向量：共享 token 越多，相似度越高。

    它不是语义模型，只是让“向量检索的接线”可以在无网络、无密钥下被测试。
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


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    vector_dir = tmp_path_factory.mktemp("vectors")
    embedder = StubEmbedder()
    build = build_local_index(
        FIXTURES,
        repo_id=DEFAULT_REPO_ID,
        embedder=embedder,
        vector_path=vector_dir,
        embedding_provider="stub",
    )
    return build, embedder, vector_dir


def test_build_records_embedding_metadata_and_vectors(built) -> None:
    build, embedder, _ = built
    index = build.index
    assert index.embedding is not None
    assert index.embedding.model == embedder.model
    assert index.embedding.dimensions == embedder.dimensions
    assert index.embedding.provider == "stub"
    assert index.vectors is not None
    assert index.vectors.collection == collection_name(embedder.model, embedder.dimensions)
    assert index.stats["vectors"] == len(index.chunks)
    with VectorStore(
        index.vectors.path, collection=index.vectors.collection, dimensions=embedder.dimensions
    ) as store:
        assert store.count() == len(index.chunks)


def test_search_never_returns_another_version(built) -> None:
    build, embedder, _ = built
    index = build.index
    foreign = make_chunk(
        "def foreign():\n    return 1", repo_id=DEFAULT_REPO_ID, commit="f" * 40
    )
    with VectorStore(
        index.vectors.path, collection=index.vectors.collection, dimensions=embedder.dimensions
    ) as store:
        store.upsert_chunks(
            [foreign],
            [embedder.embed([foreign.content]).vectors[0]],
            repo_id=foreign.repo_id,
            commit=foreign.commit,
            model=embedder.model,
        )
        try:
            hits = store.search(
                embedder.embed(["foreign"]).vectors[0],
                repo_id=index.repo_id,
                commit=index.commit,
                top_k=50,
            )
            payloads = [hit.payload for hit in hits]
            assert payloads, "应至少返回本版本的命中"
            assert all(item["commit"] == index.commit for item in payloads)
            assert all(item["repo_id"] == index.repo_id for item in payloads)
        finally:
            store.delete_version(repo_id=foreign.repo_id, commit=foreign.commit)


def test_upsert_refuses_foreign_version(built) -> None:
    build, embedder, _ = built
    index = build.index
    foreign = make_chunk("def x():\n    return 1", commit="e" * 40)
    with VectorStore(
        index.vectors.path, collection=index.vectors.collection, dimensions=embedder.dimensions
    ) as store:
        with pytest.raises(VectorStoreError):
            store.upsert_chunks(
                [foreign],
                [embedder.embed([foreign.content]).vectors[0]],
                repo_id=index.repo_id,
                commit=index.commit,
                model=embedder.model,
            )


def test_dimension_mismatch_is_rejected(built) -> None:
    build, embedder, _ = built
    index = build.index
    with pytest.raises(VectorStoreError):
        VectorStore(
            index.vectors.path,
            collection=index.vectors.collection,
            dimensions=embedder.dimensions + 1,
        )


def test_different_model_uses_a_different_collection(built, tmp_path: Path) -> None:
    build, embedder, vector_dir = built
    other = StubEmbedder(model="stub-embedding-v2")
    second = build_local_index(
        FIXTURES,
        repo_id=DEFAULT_REPO_ID,
        embedder=other,
        vector_path=vector_dir,
    )
    assert second.index.vectors is not None
    assert second.index.vectors.collection != build.index.vectors.collection
    # 旧集合没有被新模型污染
    with VectorStore(
        build.index.vectors.path,
        collection=build.index.vectors.collection,
        dimensions=embedder.dimensions,
    ) as store:
        assert store.count() == len(build.index.chunks)


def test_delete_version_removes_only_that_version(built) -> None:
    build, embedder, tmp_path = built
    # 用独立目录，避免影响其他用例
    index = build.index
    foreign = make_chunk("def keep():\n    return 1", commit="d" * 40)
    with VectorStore(
        tmp_path,
        collection=collection_name(embedder.model, embedder.dimensions),
        dimensions=embedder.dimensions,
    ) as store:
        store.upsert_chunks(
            [foreign],
            [embedder.embed([foreign.content]).vectors[0]],
            repo_id=foreign.repo_id,
            commit=foreign.commit,
            model=embedder.model,
        )
        before = store.count()
        store.delete_version(repo_id=foreign.repo_id, commit=foreign.commit)
        assert store.count() == before - 1


def test_search_vector_maps_hits_back_to_indexed_chunks(built) -> None:
    build, embedder, _ = built
    hits = search_vector(build.index, "load_tasks normalize", embedder, top_k=5)
    assert hits, "向量检索应有命中"
    known = {chunk.chunk_id for chunk in build.index.chunks}
    assert all(hit.chunk.chunk_id in known for hit in hits)
    assert all(hit.source == "vector" for hit in hits)
    # 桩向量只保证“共享 token 更相似”，不保证排序质量；这里只验证命中确实映射
    # 回了索引中的分块，且目标符号出现在候选集合里。
    assert {"load_tasks", "load_tasks.normalize"} & {hit.chunk.symbol for hit in hits}


def test_search_vector_rejects_embedding_model_mismatch(built) -> None:
    build, _, _ = built
    with pytest.raises(VectorStoreError):
        search_vector(build.index, "load_tasks", StubEmbedder(model="another-model"))


def test_search_vector_requires_a_vector_index(built) -> None:
    _, embedder, _ = built
    without_vectors = build_local_index(FIXTURES, repo_id=DEFAULT_REPO_ID)
    assert without_vectors.index.vectors is None
    with pytest.raises(VectorStoreError):
        search_vector(without_vectors.index, "load_tasks", embedder)


def test_ask_accepts_vector_hits_and_labels_evidence(built) -> None:
    build, embedder, _ = built
    hits = search_vector(build.index, "load_tasks normalize", embedder, top_k=3)
    result = ask(
        build.index,
        "load_tasks normalize",
        hits=hits,
        file_texts=build.file_texts,
    )
    assert result.evidence
    assert all(item.source == "vector" for item in result.evidence)
    assert result.citation_report.all_valid


def test_ask_with_fused_hits_labels_evidence_hybrid(built) -> None:
    from repoask.retrieve.fusion import fuse_rrf
    from repoask.retrieve.keyword import DEFAULT_KIND_WEIGHTS, KeywordIndex

    build, embedder, _ = built
    keyword_hits = KeywordIndex(
        build.index.chunks,
        repo_id=build.index.repo_id,
        commit=build.index.commit,
        kind_weights=DEFAULT_KIND_WEIGHTS,
    ).search("load_tasks", top_k=5)
    vector_hits = search_vector(build.index, "load_tasks", embedder, top_k=5)
    fused = fuse_rrf(keyword_hits, vector_hits, top_k=3)
    result = ask(build.index, "load_tasks", hits=fused, file_texts=build.file_texts)
    assert result.evidence
    assert all(item.source == "hybrid" for item in result.evidence)
    assert result.citation_report.all_valid
