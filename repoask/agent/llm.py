"""Agent 用的工具调用接口。

只定义数据形状与协议：具体实现放在 ``repoask.answer.llm.ChatClient``，
测试用脚本化假客户端实现同一协议，从而完全离线。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from repoask.agent.tools import ToolCall
from repoask.models import Usage


class ToolChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = ""
    tool_calls: list[ToolCall] = []
    usage: Usage | None = None
    model: str | None = None
    finish_reason: str | None = None
    attempts: int = 1


class ToolCaller(Protocol):
    model: str | None

    def complete_with_tools(
        self, messages: list[dict], *, tools: list[dict] | None = None
    ) -> ToolChatResponse: ...
