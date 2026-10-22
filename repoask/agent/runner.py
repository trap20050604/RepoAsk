"""Agent 运行入口：组装检索器、工具与图，执行并返回统一的结果对象。"""

from __future__ import annotations

import time
from typing import Callable

from repoask.agent.graph import build_graph, build_result
from repoask.agent.llm import ToolCaller
from repoask.agent.state import AgentBudget
from repoask.agent.tools import RepoaskTools
from repoask.index import ChunkIndex
from repoask.models import AnswerResult
from repoask.retrieve.fusion import fuse_rrf
from repoask.retrieve.keyword import DEFAULT_KIND_WEIGHTS, KeywordIndex, SearchHit
from repoask.runlog import RunLogger

Searcher = Callable[[str, int], list[SearchHit]]


def keyword_searcher(index: ChunkIndex) -> Searcher:
    keyword = KeywordIndex(
        index.chunks,
        repo_id=index.repo_id,
        commit=index.commit,
        kind_weights=DEFAULT_KIND_WEIGHTS,
    )
    return lambda query, top_k: keyword.search(query, top_k=top_k)


def hybrid_searcher(index: ChunkIndex, embedder: object) -> Searcher:
    """关键词 + 向量 + RRF；仅在索引带向量时使用。"""
    from repoask.pipeline import search_vector

    keyword = KeywordIndex(
        index.chunks,
        repo_id=index.repo_id,
        commit=index.commit,
        kind_weights=DEFAULT_KIND_WEIGHTS,
    )

    def search(query: str, top_k: int) -> list[SearchHit]:
        candidate_k = max(top_k * 2, 10)
        return fuse_rrf(
            keyword.search(query, top_k=candidate_k),
            search_vector(index, query, embedder, top_k=candidate_k),
            top_k=top_k,
        )

    return search


def run_agent(
    question: str,
    *,
    index: ChunkIndex,
    file_texts: dict[str, str],
    client: ToolCaller,
    searcher: Searcher | None = None,
    embedder: object | None = None,
    budget: AgentBudget | None = None,
    logger: RunLogger | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AnswerResult:
    """跑一次工具循环，返回带引用校验的 AnswerResult。"""
    question = (question or "").strip()
    if not question:
        raise ValueError("question must not be empty")
    active_budget = budget or AgentBudget()
    active_searcher = searcher
    if active_searcher is None:
        if embedder is not None and index.vectors is not None:
            active_searcher = hybrid_searcher(index, embedder)
        else:
            active_searcher = keyword_searcher(index)

    tools = RepoaskTools(
        repo_id=index.repo_id,
        commit=index.commit,
        file_texts=file_texts,
        searcher=active_searcher,
        budget=active_budget,
    )
    graph = build_graph(
        client=client,
        tools=tools,
        repo_id=index.repo_id,
        commit=index.commit,
        file_texts=file_texts,
        budget=active_budget,
        repo_url=index.repo_url,
        logger=logger,
        clock=clock,
    )
    started = clock()
    if logger:
        logger.record(
            "agent_start",
            question=question,
            repo_id=index.repo_id,
            commit=index.commit,
            budget=active_budget.model_dump(),
        )
    initial: dict = {
        "question": question,
        "repo_id": index.repo_id,
        "commit": index.commit,
        "messages": [],
        "evidence": [],
        "steps": 0,
        "tool_calls": 0,
        "pending": [],
        "stop_reason": None,
        "answer_text": "",
        "notes": [],
        "deadline": started + active_budget.max_seconds,
        "usage_prompt": 0,
        "usage_completion": 0,
        "usage_missing": 0,
        "citation_report": None,
    }
    final = graph.invoke(
        initial,
        config={"recursion_limit": active_budget.max_steps * 2 + 8},
    )
    elapsed_ms = int((clock() - started) * 1000)
    result = build_result(
        question=question,
        state=final,
        model=client.model,
        elapsed_ms=elapsed_ms,
    )
    if logger:
        logger.record(
            "agent_done",
            termination_reason=result.termination_reason,
            evidence=len(result.evidence),
            citations_valid=result.citation_report.valid_count,
            citations_checked=result.citation_report.checked_count,
            # 不能叫 elapsed_ms：那是日志自身的标准列，覆盖后轨迹会变成非单调。
            agent_elapsed_ms=elapsed_ms,
        )
    return result
