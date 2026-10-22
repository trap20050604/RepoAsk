"""Agent 的状态与预算。

状态字段对应功能要求 D：问题、版本、工具消息、证据、步数、预算、终止原因、答案。
状态用 TypedDict 而不是 Pydantic 模型，是因为 LangGraph 直接对字典做合并；
每个节点返回“自己修改的字段”，未返回的字段保持不变。
"""

from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field

from repoask.models import CitationReport, Evidence

# 终止原因（写进结果与轨迹，便于评测统计失败类型）
TERMINATION_ANSWERED = "answered"
TERMINATION_INSUFFICIENT = "insufficient_evidence"
TERMINATION_NO_EVIDENCE = "no_evidence"
TERMINATION_STEP_BUDGET = "step_budget"
TERMINATION_TOOL_BUDGET = "tool_budget"
TERMINATION_TIME_BUDGET = "time_budget"
TERMINATION_TOOL_ERROR = "tool_error"
TERMINATION_LLM_ERROR = "llm_error"


class AgentBudget(BaseModel):
    """一次 Agent 运行的硬上限。任何一项触发都必须显式结束并说明原因。"""

    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(default=6, ge=1, le=20)
    max_tool_calls: int = Field(default=8, ge=1, le=30)
    max_seconds: float = Field(default=60.0, gt=0)
    max_evidence: int = Field(default=8, ge=1, le=30)
    max_tool_output_chars: int = Field(default=4000, ge=200, le=20000)


class AgentState(TypedDict, total=False):
    question: str
    repo_id: str
    commit: str
    # 对话消息：system / user / assistant（含 tool_calls）/ tool
    messages: list[dict]
    evidence: list[Evidence]
    steps: int
    tool_calls: int
    pending: list[dict]
    stop_reason: str | None
    termination_reason: str | None
    answer_text: str
    notes: list[str]
    deadline: float
    usage_prompt: int
    usage_completion: int
    usage_missing: int
    citation_report: CitationReport | None
