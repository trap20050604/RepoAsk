"""Tests for lexical retrieval, version isolation and context assembly."""

from __future__ import annotations

from pathlib import Path

import pytest

from repoask.models import ParentContext
from repoask.pipeline import build_local_index
from repoask.retrieve.context import ContextBudget, build_context, render_evidence_block
from repoask.retrieve.keyword import DEFAULT_KIND_WEIGHTS, KeywordIndex, SearchHit, tokenize
from repoask.testing import DEFAULT_COMMIT, DEFAULT_REPO_ID, make_chunk, make_evidence

FIXTURES = Path(__file__).parent / "fixtures" / "demo_repo"


@pytest.fixture(scope="module")
def build():
    return build_local_index(FIXTURES, repo_id=DEFAULT_REPO_ID)


def test_tokenizer_splits_identifiers_paths_and_cjk() -> None:
    assert {"load_tasks", "load", "tasks"} <= set(tokenize("load_tasks"))
    assert {"loadtasks", "load", "tasks"} <= set(tokenize("loadTasks"))
    assert "重试" in tokenize("重试逻辑")
    assert "service" in tokenize("app/service.py")


def test_exact_symbol_query_ranks_that_function_first(build) -> None:
    index = build.index
    search = KeywordIndex(
        index.chunks,
        repo_id=index.repo_id,
        commit=index.commit,
        kind_weights=DEFAULT_KIND_WEIGHTS,
    )
    hits = search.search("load_tasks", top_k=3)
    assert hits, "expected at least one hit"
    assert hits[0].chunk.symbol == "load_tasks"


def test_version_guard_keeps_hits_inside_one_commit(build) -> None:
    index = build.index
    foreign = make_chunk("def load_tasks():\n    return 1\n", commit="f" * 40)
    search = KeywordIndex(
        list(index.chunks) + [foreign], repo_id=index.repo_id, commit=index.commit
    )
    hits = search.search("load_tasks", top_k=10)
    assert hits
    assert all(hit.chunk.commit == index.commit for hit in hits)


def test_kind_prior_scales_text_chunk_scores(build) -> None:
    index = build.index
    neutral = KeywordIndex(index.chunks, repo_id=index.repo_id, commit=index.commit)
    weighted = KeywordIndex(
        index.chunks,
        repo_id=index.repo_id,
        commit=index.commit,
        kind_weights={"text": 0.5},
    )
    neutral_scores = {hit.chunk.chunk_id: hit.score for hit in neutral.search("配置", top_k=20)}
    weighted_scores = {hit.chunk.chunk_id: hit.score for hit in weighted.search("配置", top_k=20)}
    text_ids = {chunk.chunk_id for chunk in index.chunks if chunk.kind == "text"}
    shared = text_ids & neutral_scores.keys() & weighted_scores.keys()
    assert shared, "expected at least one text chunk in both result sets"
    for chunk_id in shared:
        assert weighted_scores[chunk_id] == pytest.approx(neutral_scores[chunk_id] * 0.5, abs=1e-4)


def test_context_assigns_sequential_ids_and_urls() -> None:
    chunk = make_chunk("def load_tasks():\n    return 1", symbol="load_tasks", start_line=37)
    bundle = build_context(
        [SearchHit(chunk=chunk, score=3.0)],
        repo_id=DEFAULT_REPO_ID,
        commit=DEFAULT_COMMIT,
        repo_url="https://github.com/trap20050604/RepoAsk",
        budget=ContextBudget(max_chars=4000, max_items=4),
    )
    assert [item.evidence_id for item in bundle.evidence] == ["E1"]
    assert bundle.evidence[0].url is not None
    assert bundle.evidence[0].url.endswith("/blob/" + DEFAULT_COMMIT + "/app/module.py#L37-L38")
    assert "调用" not in bundle.rendered  # sanity: nothing invented


def test_context_deduplicates_identical_content() -> None:
    content = "def a():\n    return 1\n"
    first = make_chunk(content, path="app/a.py", start_line=1)
    second = make_chunk(content, path="app/b.py", start_line=10)
    bundle = build_context(
        [SearchHit(chunk=first, score=2.0), SearchHit(chunk=second, score=1.0)],
        repo_id=DEFAULT_REPO_ID,
        commit=DEFAULT_COMMIT,
    )
    assert [item.path for item in bundle.evidence] == ["app/a.py"]
    assert [dropped.reason for dropped in bundle.dropped] == ["duplicate_content"]


def test_context_drops_nested_overlap() -> None:
    outer = make_chunk("\n".join(f"line {index}" for index in range(21)), start_line=10)
    inner = make_chunk("\n".join(f"line {index}" for index in range(6)), start_line=15)
    bundle = build_context(
        [SearchHit(chunk=outer, score=2.0), SearchHit(chunk=inner, score=1.5)],
        repo_id=DEFAULT_REPO_ID,
        commit=DEFAULT_COMMIT,
    )
    assert len(bundle.evidence) == 1
    assert bundle.dropped[0].reason == "nested_overlap"


def test_context_drops_foreign_version() -> None:
    foreign = make_chunk("def x():\n    return 1\n", commit="e" * 40)
    bundle = build_context(
        [SearchHit(chunk=foreign, score=2.0)],
        repo_id=DEFAULT_REPO_ID,
        commit=DEFAULT_COMMIT,
    )
    assert bundle.evidence == []
    assert bundle.dropped[0].reason == "version_mismatch"


def test_context_respects_budget_but_always_keeps_one_item() -> None:
    big = make_chunk("\n".join(f"x = {index}" for index in range(60)), start_line=1)
    other = make_chunk("\n".join(f"y = {index}" for index in range(60)), start_line=100)
    bundle = build_context(
        [SearchHit(chunk=big, score=2.0), SearchHit(chunk=other, score=1.0)],
        repo_id=DEFAULT_REPO_ID,
        commit=DEFAULT_COMMIT,
        budget=ContextBudget(max_chars=200, max_items=5),
    )
    assert len(bundle.evidence) == 1
    assert bundle.dropped[0].reason == "budget"


def test_parent_context_is_rendered_separately_with_its_own_lines() -> None:
    parent = ParentContext(
        symbol="ConfigParser", start_line=8, end_line=8, content="class ConfigParser:"
    )
    chunk = make_chunk(
        "    def parse(self):\n        return {}",
        symbol="ConfigParser.parse",
        kind="method",
        start_line=26,
        parent_context=parent,
    )
    evidence = make_evidence(chunk)
    rendered = render_evidence_block(evidence)
    assert "class ConfigParser:" in rendered
    assert f"{8:>5}| class ConfigParser:" in rendered
    assert f"{26:>5}|     def parse(self):" in rendered
    assert "context only" in rendered
