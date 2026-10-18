"""OpenAI-compatible chat client with explicit timeout, bounded retries and usage capture.

Two rules encoded here:
  * credentials come only from :class:`repoask.config.Settings`, never from code;
  * a missing ``usage`` block is reported as ``None`` ("unavailable") rather than
    being silently recorded as zero tokens.

Retry classification uses exception *class names* so the same client works with a
duck-typed fake in tests without needing the SDK's exception constructors.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from repoask.config import Settings
from repoask.errors import LLMUnavailable
from repoask.models import Usage

RETRYABLE_ERROR_NAMES = frozenset(
    {"APITimeoutError", "APIConnectionError", "RateLimitError", "InternalServerError"}
)


class LLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    model: str | None = None
    usage: Usage | None = None
    latency_ms: int = 0
    finish_reason: str | None = None
    attempts: int = 1


class ProbeStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "failed", "unavailable", "skipped"]
    detail: str | None = None
    latency_ms: int | None = None


class ProbeReport(BaseModel):
    """Evidence about what the configured endpoint can actually do."""

    model_config = ConfigDict(extra="forbid")

    configured: bool
    base_url: str | None = None
    model: str | None = None
    basic_call: ProbeStep
    usage_reported: ProbeStep
    tool_call: ProbeStep
    streaming: ProbeStep
    notes: list[str] = []


def _extract_usage(completion: Any) -> Usage | None:
    usage = getattr(completion, "usage", None)
    if usage is None:
        return None
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    if prompt_tokens is None and completion_tokens is None and total_tokens is None:
        return Usage(note="provider returned a usage object without token counts")
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def _message_text(completion: Any) -> str:
    choices = getattr(completion, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None) if message is not None else None
    return content or ""


def _finish_reason(completion: Any) -> str | None:
    choices = getattr(completion, "choices", None) or []
    if not choices:
        return None
    reason = getattr(choices[0], "finish_reason", None)
    return str(reason) if reason is not None else None


class ChatClient:
    """Thin wrapper around ``openai.OpenAI`` with our own retry policy."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        if not settings.llm_configured:
            raise LLMUnavailable(
                "LLM_BASE_URL, LLM_API_KEY and LLM_MODEL must all be set to use the chat model"
            )
        self.settings = settings
        self.model = settings.llm_model
        self._last_usage: Usage | None = None
        if client is not None:
            self._client = client
        else:
            from openai import OpenAI  # imported lazily so offline tests need no network

            self._client = OpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout_seconds,
                max_retries=0,  # retries are handled below, visibly
            )

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        extra: dict[str, Any] | None = None,
    ) -> LLMResponse:
        attempts = self.settings.llm_max_retries + 1
        last_error: Exception | None = None
        started = time.perf_counter()
        for attempt in range(1, attempts + 1):
            try:
                completion = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    **(extra or {}),
                )
            except Exception as exc:  # noqa: BLE001 - classified below
                last_error = exc
                if type(exc).__name__ in RETRYABLE_ERROR_NAMES and attempt < attempts:
                    time.sleep(min(0.5 * attempt, 2.0))
                    continue
                raise LLMUnavailable(
                    f"chat completion failed after {attempt} attempt(s): "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            return LLMResponse(
                text=_message_text(completion),
                model=getattr(completion, "model", self.model),
                usage=_extract_usage(completion),
                latency_ms=int((time.perf_counter() - started) * 1000),
                finish_reason=_finish_reason(completion),
                attempts=attempt,
            )
        raise LLMUnavailable(
            f"chat completion failed after {attempts} attempt(s): {last_error}"
        )

    # -- capability probe -------------------------------------------------
    def probe(self) -> ProbeReport:
        """Check basic call, usage reporting, tool calling and streaming.

        Every step is attempted for real. A failing capability is reported as
        failed, never assumed to work because the endpoint says "compatible".
        """
        notes: list[str] = []
        basic = self._probe_basic(notes)
        usage_step = (
            ProbeStep(status="ok", detail="provider returned token usage")
            if basic.status == "ok" and self._last_usage is not None
            else ProbeStep(
                status="failed" if basic.status == "ok" else "skipped",
                detail="provider did not report token usage"
                if basic.status == "ok"
                else "basic call failed",
            )
        )
        tools = self._probe_tools()
        streaming = self._probe_streaming()
        return ProbeReport(
            configured=True,
            base_url=self.settings.llm_base_url,
            model=self.model,
            basic_call=basic,
            usage_reported=usage_step,
            tool_call=tools,
            streaming=streaming,
            notes=notes,
        )

    def _probe_basic(self, notes: list[str]) -> ProbeStep:
        started = time.perf_counter()
        try:
            response = self.complete(
                [{"role": "user", "content": "Reply with the single word: pong"}],
                temperature=0.0,
                extra={"max_tokens": 16},
            )
        except LLMUnavailable as exc:
            return ProbeStep(status="failed", detail=str(exc))
        self._last_usage = response.usage
        if response.usage is None:
            notes.append("usage block missing; token counts will be recorded as unavailable")
        return ProbeStep(
            status="ok",
            detail=f"reply={response.text.strip()[:40]!r}",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _probe_tools(self) -> ProbeStep:
        tool = [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "Echo the given text back",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                },
            }
        ]
        try:
            completion = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "Call the echo tool with text=hi"}],
                tools=tool,
                tool_choice="auto",
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - probe reports, does not raise
            return ProbeStep(status="failed", detail=f"{type(exc).__name__}: {exc}")
        choices = getattr(completion, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        tool_calls = getattr(message, "tool_calls", None) if message is not None else None
        if tool_calls:
            return ProbeStep(status="ok", detail=f"returned {len(tool_calls)} tool call(s)")
        return ProbeStep(status="failed", detail="endpoint returned no tool_calls")

    def _probe_streaming(self) -> ProbeStep:
        started = time.perf_counter()
        try:
            stream = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "Count from 1 to 5."}],
                stream=True,
                temperature=0.0,
            )
            chunks = 0
            text_parts: list[str] = []
            for chunk in stream:
                chunks += 1
                choices = getattr(chunk, "choices", None) or []
                if choices:
                    delta_obj = getattr(choices[0], "delta", None)
                    content = getattr(delta_obj, "content", None) if delta_obj else None
                    if content:
                        text_parts.append(content)
                if chunks >= 200:
                    break
        except Exception as exc:  # noqa: BLE001 - probe reports, does not raise
            return ProbeStep(status="failed", detail=f"{type(exc).__name__}: {exc}")
        if chunks == 0:
            return ProbeStep(status="failed", detail="stream produced no chunks")
        return ProbeStep(
            status="ok",
            detail=f"{chunks} chunks, {len(''.join(text_parts))} chars",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
