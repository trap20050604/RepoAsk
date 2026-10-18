"""Answering strategies (offline baseline now, LLM single pass, tool loop later)."""

from repoask.answer.llm import ChatClient, LLMResponse, ProbeReport, ProbeStep
from repoask.answer.single_pass import (
    INSUFFICIENT_MARKER,
    LLMAnswerer,
    OfflineExtractiveAnswerer,
    AnswerDraft,
    build_user_prompt,
)

__all__ = [
    "INSUFFICIENT_MARKER",
    "AnswerDraft",
    "ChatClient",
    "LLMAnswerer",
    "LLMResponse",
    "OfflineExtractiveAnswerer",
    "ProbeReport",
    "ProbeStep",
    "build_user_prompt",
]
