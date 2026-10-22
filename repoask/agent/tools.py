"""工具：search_code 与 read_file。

三条硬约束：
1. 参数必须校验（类型、范围、路径）；非法参数返回明确的错误，不伪装成功。
2. 输出必须截断（行数/字符数上限），避免一次工具调用把上下文吃光。
3. ``read_file`` 只读索引里存在的文件，且路径必须是仓库相对路径；
   越界、绝对路径、未索引文件一律返回错误。

工具在返回文本的同时登记 Evidence（带 E 编号），这样模型引用的每个 [E#]
都能被程序映射回真实文件与行号。
"""

from __future__ import annotations

import json
from typing import Callable, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from repoask.agent.state import AgentBudget
from repoask.models import Chunk, Evidence, content_sha256
from repoask.retrieve.keyword import SearchHit

SEARCH_TOOL = "search_code"
READ_TOOL = "read_file"


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, object] = {}


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    name: str
    ok: bool
    content: str
    error: str | None = None
    evidence_ids: list[str] = []


def tool_schemas() -> list[dict]:
    """OpenAI function-calling 格式的工具描述。"""
    return [
        {
            "type": "function",
            "function": {
                "name": SEARCH_TOOL,
                "description": (
                    "在固定 commit 的仓库里检索代码。返回证据编号、路径与原始行号。"
                    "需要定位实现、符号、调用点时先用它。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索词或问题片段"},
                        "top_k": {
                            "type": "integer",
                            "description": "返回条数，1-10，默认 5",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": READ_TOOL,
                "description": (
                    "读取仓库中某个已索引文件的一段代码。用于查看完整实现。"
                    "路径必须是仓库相对路径，例如 app/service.py。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "仓库相对路径"},
                        "start_line": {"type": "integer", "description": "起始行，1 开始"},
                        "end_line": {"type": "integer", "description": "结束行，含"},
                    },
                    "required": ["path"],
                },
            },
        },
    ]


class RepoaskTools:
    """工具执行器；持有 evidence 列表，保证 [E#] 与内容一一对应。"""

    def __init__(
        self,
        *,
        repo_id: str,
        commit: str,
        file_texts: dict[str, str],
        searcher: Callable[[str, int], list[SearchHit]],
        budget: AgentBudget,
    ) -> None:
        self.repo_id = repo_id
        self.commit = commit
        self.file_texts = file_texts
        self.searcher = searcher
        self.budget = budget
        self.evidence: list[Evidence] = []
        self._seen: set[str] = set()

    # -- evidence ---------------------------------------------------------
    def _register(
        self,
        *,
        path: str,
        start_line: int,
        end_line: int,
        content: str,
        symbol: str | None,
        source: str,
        score: float | None = None,
    ) -> str | None:
        key = f"{path}:{start_line}-{end_line}:{content_sha256(content)[:12]}"
        if key in self._seen:
            for item in self.evidence:
                if (
                    item.path == path
                    and item.start_line == start_line
                    and item.end_line == end_line
                ):
                    return item.evidence_id
            return None
        if len(self.evidence) >= self.budget.max_evidence:
            return None
        self._seen.add(key)
        evidence_id = f"E{len(self.evidence) + 1}"
        self.evidence.append(
            Evidence(
                evidence_id=evidence_id,
                repo_id=self.repo_id,
                commit=self.commit,
                path=path,
                symbol=symbol,
                start_line=start_line,
                end_line=end_line,
                content=content,
                content_hash=content_sha256(content),
                source=source,
                score=score,
            )
        )
        return evidence_id

    # -- tools ------------------------------------------------------------
    def search_code(self, query: str, top_k: int = 5) -> ToolResult:
        hits = self.searcher(query, top_k)
        if not hits:
            return ToolResult(
                call_id="",
                name=SEARCH_TOOL,
                ok=True,
                content=f"没有检索到与 {query!r} 相关且属于本版本的代码。",
            )
        lines: list[str] = []
        added: list[str] = []
        for hit in hits:
            chunk = hit.chunk
            evidence_id = self._register(
                path=chunk.path,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                content=chunk.content,
                symbol=chunk.symbol,
                source=SEARCH_TOOL,
                score=hit.score,
            )
            if evidence_id is None:
                continue
            added.append(evidence_id)
            symbol = f" symbol={chunk.symbol}" if chunk.symbol else ""
            preview = _preview(chunk.content, self.budget.max_tool_output_chars // 4)
            lines.append(
                f"[{evidence_id}] {chunk.path}:{chunk.start_line}-{chunk.end_line}"
                f"{symbol} score={hit.score:.4f}\n{preview}"
            )
        if not lines:
            return ToolResult(
                call_id="",
                name=SEARCH_TOOL,
                ok=True,
                content="命中结果已达到证据条数上限，未新增证据。",
            )
        return ToolResult(
            call_id="",
            name=SEARCH_TOOL,
            ok=True,
            content=_truncate("\n\n".join(lines), self.budget.max_tool_output_chars),
            evidence_ids=added,
        )

    def read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> ToolResult:
        normalized = (path or "").replace("\\", "/").strip()
        if normalized.startswith("/") or ".." in normalized.split("/"):
            return self._error(READ_TOOL, f"拒绝读取非法路径：{path!r}")
        if normalized not in self.file_texts:
            return self._error(READ_TOOL, f"文件未索引或不存在：{path!r}")
        lines = self.file_texts[normalized].split("\n")
        total = len(lines)
        start = 1 if start_line is None else int(start_line)
        end = total if end_line is None else int(end_line)
        if start < 1 or end < start:
            return self._error(READ_TOOL, f"行号范围非法：{start}-{end}")
        start = min(start, total)
        end = min(end, total)
        # 单次最多 200 行，避免一次读文件吃光上下文
        capped_end = min(end, start + 199)
        body = lines[start - 1 : capped_end]
        content = "\n".join(body)
        evidence_id = self._register(
            path=normalized,
            start_line=start,
            end_line=capped_end,
            content=content,
            symbol=None,
            source=READ_TOOL,
        )
        numbered = "\n".join(
            f"{start + offset:>5}| {text}" for offset, text in enumerate(body)
        )
        suffix = ""
        if capped_end < end:
            suffix = f"\n...（已截断：请求 {start}-{end}，实际展示 {start}-{capped_end}）"
        prefix = f"[{evidence_id}] {normalized}:{start}-{capped_end}\n" if evidence_id else ""
        return ToolResult(
            call_id="",
            name=READ_TOOL,
            ok=True,
            content=_truncate(
                prefix + numbered + suffix, self.budget.max_tool_output_chars
            ),
            evidence_ids=[evidence_id] if evidence_id else [],
        )

    def execute(self, call: ToolCall) -> ToolResult:
        """执行一次工具调用；任何异常都转成明确的失败结果。"""
        try:
            if call.name == SEARCH_TOOL:
                arguments = _validate(call.arguments, required={"query": str}, optional={"top_k": int})
                query = str(arguments["query"]).strip()
                if not query:
                    return self._error(call.name, "query 不能为空")
                top_k = int(arguments.get("top_k", 5))
                if not 1 <= top_k <= 10:
                    return self._error(call.name, f"top_k 必须在 1-10 之间，收到 {top_k}")
                result = self.search_code(query, top_k)
            elif call.name == READ_TOOL:
                arguments = _validate(
                    call.arguments,
                    required={"path": str},
                    optional={"start_line": int, "end_line": int},
                )
                result = self.read_file(
                    str(arguments["path"]),
                    arguments.get("start_line"),
                    arguments.get("end_line"),
                )
            else:
                return self._error(call.name, f"未知工具：{call.name!r}")
        except (ValidationError, ValueError, TypeError) as exc:
            return self._error(call.name, f"参数不合法：{exc}")
        except Exception as exc:  # noqa: BLE001 - 工具错误必须显式返回给模型
            return self._error(call.name, f"{type(exc).__name__}: {exc}")
        return result.model_copy(update={"call_id": call.id})

    def _error(self, name: str, message: str) -> ToolResult:
        return ToolResult(
            call_id="",
            name=name,
            ok=False,
            content=f"ERROR: {message}",
            error=message,
        )


def _validate(
    arguments: dict[str, object],
    *,
    required: dict[str, type],
    optional: dict[str, type],
) -> dict[str, object]:
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是对象")
    cleaned: dict[str, object] = {}
    for key, expected in required.items():
        if key not in arguments:
            raise ValueError(f"缺少参数 {key!r}")
        value = arguments[key]
        # int 是 bool 的子类，单独排除，避免 True 被当成 top_k
        if expected is int and isinstance(value, bool):
            raise ValueError(f"参数 {key!r} 必须是整数")
        if not isinstance(value, expected):
            raise ValueError(f"参数 {key!r} 必须是 {expected.__name__}")
        cleaned[key] = value
    for key, expected in optional.items():
        if key not in arguments or arguments[key] is None:
            continue
        value = arguments[key]
        if expected is int and isinstance(value, bool):
            raise ValueError(f"参数 {key!r} 必须是整数")
        if not isinstance(value, expected):
            raise ValueError(f"参数 {key!r} 必须是 {expected.__name__}")
        cleaned[key] = value
    unknown = set(arguments) - set(required) - set(optional)
    if unknown:
        raise ValueError(f"未知参数：{sorted(unknown)}")
    return cleaned


def _preview(content: str, limit: int) -> str:
    lines = content.split("\n")[:8]
    text = "\n".join(lines)
    return _truncate(text, limit)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...（输出已截断，共 {len(text)} 字符）"


def parse_arguments(raw: str) -> dict[str, object]:
    """解析模型返回的 JSON 参数；解析失败时保留原文供工具层报错。"""
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"_raw": raw}
    if not isinstance(parsed, dict):
        return {"_raw": parsed}
    return parsed
