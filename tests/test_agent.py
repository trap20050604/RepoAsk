"""阶段 3 Agent 的离线测试：脚本化模型客户端 + 本地夹具，不调用真实接口。

覆盖：工具调用闭环、引用校验、步数/工具次数/时限预算、工具错误显式返回、
参数校验、输出截断、轨迹事件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoask.agent.llm import ToolChatResponse
from repoask.agent.runner import hybrid_searcher, run_agent
from repoask.agent.state import AgentBudget
from repoask.agent.tools import READ_TOOL, SEARCH_TOOL, RepoaskTools, ToolCall
from repoask.models import Usage
from repoask.pipeline import build_local_index
from repoask.runlog import RunLogger, read_jsonl
from repoask.testing import DEFAULT_REPO_ID, StubEmbedder

FIXTURES = Path(__file__).parent / "fixtures" / "demo_repo"


def usage() -> Usage:
    return Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12)


def tool_response(name: str, arguments: dict, *, call_id: str = "call_1") -> ToolChatResponse:
    return ToolChatResponse(
        text="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        usage=usage(),
        model="scripted-model",
        finish_reason="tool_calls",
    )


def final_response(text: str) -> ToolChatResponse:
    return ToolChatResponse(
        text=text,
        tool_calls=[],
        usage=usage(),
        model="scripted-model",
        finish_reason="stop",
    )


class ScriptedClient:
    """按脚本依次返回响应；记录每次收到的消息，便于断言工具结果已回填。"""

    model = "scripted-model"

    def __init__(self, responses: list[ToolChatResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def complete_with_tools(self, messages: list[dict], *, tools=None) -> ToolChatResponse:
        self.calls.append(list(messages))
        if not self.responses:
            raise AssertionError("脚本响应已用尽，模型被调用次数超过预期")
        return self.responses.pop(0)


@pytest.fixture(scope="module")
def build():
    return build_local_index(FIXTURES, repo_id=DEFAULT_REPO_ID)


def test_agent_answers_after_one_search_tool_call(build) -> None:
    client = ScriptedClient(
        [
            tool_response(SEARCH_TOOL, {"query": "load_tasks", "top_k": 3}),
            final_response("load_tasks 负责加载任务，见 [E1]。"),
        ]
    )
    result = run_agent(
        "load_tasks 做了什么？",
        index=build.index,
        file_texts=build.file_texts,
        client=client,
    )
    assert result.mode == "agent"
    assert result.termination_reason == "answered"
    assert result.evidence
    assert result.citation_report.answer_has_citation
    assert result.citation_report.all_valid
    # 第二次模型调用必须看到工具结果
    tool_messages = [m for m in client.calls[1] if m.get("role") == "tool"]
    assert tool_messages and "E1" in tool_messages[0]["content"]
    assert result.usage is not None and result.usage.total_tokens == 24


def test_agent_stops_at_step_budget_with_explicit_message(build) -> None:
    client = ScriptedClient(
        [
            tool_response(SEARCH_TOOL, {"query": "load_tasks"}),
            tool_response(SEARCH_TOOL, {"query": "retry"}),
            final_response("不应被调用"),
        ]
    )
    result = run_agent(
        "重试逻辑在哪？",
        index=build.index,
        file_texts=build.file_texts,
        client=client,
        budget=AgentBudget(max_steps=2, max_tool_calls=8),
    )
    assert result.termination_reason == "step_budget"
    assert "步数预算" in result.answer_text
    assert "不足以判断" in result.answer_text
    assert len(client.calls) == 2


def test_agent_stops_at_tool_budget_when_one_turn_requests_too_many(build) -> None:
    two_calls = ToolChatResponse(
        text="",
        tool_calls=[
            ToolCall(id="c1", name=SEARCH_TOOL, arguments={"query": "load_tasks"}),
            ToolCall(id="c2", name=SEARCH_TOOL, arguments={"query": "retry"}),
        ],
        usage=usage(),
        model="scripted-model",
        finish_reason="tool_calls",
    )
    client = ScriptedClient([two_calls, final_response("不应被调用")])
    result = run_agent(
        "两个问题",
        index=build.index,
        file_texts=build.file_texts,
        client=client,
        budget=AgentBudget(max_steps=6, max_tool_calls=1),
    )
    assert result.termination_reason == "tool_budget"
    assert "工具调用次数上限" in result.answer_text
    # 只执行了一次工具调用，第二次被预算拦住
    assert any("还剩 1 个调用未执行" in note for note in result.notes)


def test_tool_error_is_returned_to_the_model(build) -> None:
    client = ScriptedClient(
        [
            tool_response(READ_TOOL, {"path": "../../etc/passwd"}),
            final_response("不足以判断：读取失败。"),
        ]
    )
    result = run_agent(
        "读一个越界文件",
        index=build.index,
        file_texts=build.file_texts,
        client=client,
    )
    tool_messages = [m for m in client.calls[1] if m.get("role") == "tool"]
    assert tool_messages and tool_messages[0]["content"].startswith("ERROR:")
    assert any("工具 read_file 返回错误" in note for note in result.notes)
    assert result.termination_reason == "insufficient_evidence"


def test_time_budget_terminates_before_model_call(build) -> None:
    ticks = [0.0, 1000.0, 1000.0]
    clock = lambda: ticks.pop(0)  # noqa: E731
    client = ScriptedClient([final_response("不应被调用")])
    result = run_agent(
        "任意问题",
        index=build.index,
        file_texts=build.file_texts,
        client=client,
        budget=AgentBudget(max_seconds=10),
        clock=clock,
    )
    assert result.termination_reason == "time_budget"
    assert "总时限" in result.answer_text
    assert client.calls == []


def test_read_file_registers_evidence_and_truncates(build) -> None:
    # 用一个 300 行的合成文件，确保触发 200 行上限
    long_text = "\n".join(f"line_{index} = {index}" for index in range(300))
    tools = RepoaskTools(
        repo_id=build.index.repo_id,
        commit=build.index.commit,
        file_texts={"big.py": long_text},
        searcher=lambda query, top_k: [],
        budget=AgentBudget(max_tool_output_chars=6000),
    )
    result = tools.read_file("big.py", 1, 300)
    assert result.ok
    assert result.evidence_ids == ["E1"]
    assert "已截断" in result.content
    evidence = tools.evidence[0]
    assert evidence.path == "big.py"
    assert evidence.start_line == 1
    assert evidence.end_line == 200, "单次读取上限 200 行"


def test_tool_argument_validation_is_explicit(build) -> None:
    tools = RepoaskTools(
        repo_id=build.index.repo_id,
        commit=build.index.commit,
        file_texts=build.file_texts,
        searcher=lambda query, top_k: [],
        budget=AgentBudget(),
    )
    missing = tools.execute(ToolCall(id="c1", name=SEARCH_TOOL, arguments={}))
    assert not missing.ok and "缺少参数" in (missing.error or "")
    bad_range = tools.execute(
        ToolCall(id="c2", name=SEARCH_TOOL, arguments={"query": "x", "top_k": 99})
    )
    assert not bad_range.ok and "1-10" in (bad_range.error or "")
    unknown = tools.execute(ToolCall(id="c3", name="delete_repo", arguments={}))
    assert not unknown.ok and "未知工具" in (unknown.error or "")
    absolute = tools.execute(ToolCall(id="c4", name=READ_TOOL, arguments={"path": "E:/x.py"}))
    assert not absolute.ok and "未索引" in (absolute.error or "")


def test_agent_trace_records_each_phase(build, tmp_path: Path) -> None:
    log_path = tmp_path / "agent.jsonl"
    client = ScriptedClient(
        [
            tool_response(SEARCH_TOOL, {"query": "load_tasks"}),
            final_response("见 [E1]。"),
        ]
    )
    with RunLogger(log_path) as logger:
        run_agent(
            "load_tasks 做了什么？",
            index=build.index,
            file_texts=build.file_texts,
            client=client,
            logger=logger,
        )
    events = [entry["event"] for entry in read_jsonl(log_path)]
    for expected in (
        "agent_start",
        "agent_retrieve",
        "agent_model_call",
        "agent_tool_call",
        "agent_finalize",
        "agent_done",
    ):
        assert expected in events, f"缺少事件 {expected}: {events}"

    entries = read_jsonl(log_path)
    stamps = [entry["elapsed_ms"] for entry in entries]
    assert stamps == sorted(stamps), "轨迹的 elapsed_ms 必须单调不减"
    done = next(entry for entry in entries if entry["event"] == "agent_done")
    assert "agent_elapsed_ms" in done


def test_hybrid_searcher_returns_fused_hits(tmp_path: Path) -> None:
    embedder = StubEmbedder()
    build = build_local_index(
        FIXTURES,
        repo_id=DEFAULT_REPO_ID,
        embedder=embedder,
        vector_path=tmp_path / "vectors",
    )
    searcher = hybrid_searcher(build.index, embedder)
    hits = searcher("load_tasks normalize", 3)
    assert hits
    assert all(hit.source == "hybrid" for hit in hits)
