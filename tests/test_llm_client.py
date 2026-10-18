"""Tests for the chat client and the citation-repair path, using stubbed clients.

Nothing here contacts a model: every response is a local fake. Passing tests
prove our retry/timeout/usage/repair logic, and say nothing about model quality.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from repoask.answer.llm import ChatClient
from repoask.answer.single_pass import AnswerDraft, LLMAnswerer
from repoask.config import Settings
from repoask.errors import LLMUnavailable
from repoask.models import Evidence
from repoask.pipeline import ask, build_local_index
from repoask.testing import DEFAULT_REPO_ID
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "demo_repo"


class APITimeoutError(Exception):
    """Name matches the class name the client treats as retryable."""


class AuthenticationError(Exception):
    """Name matches a non-retryable failure."""


def completion(text: str, *, usage: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        model="fake-model",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text, tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        if usage
        else None,
    )


class FakeCompletions:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(responses, *, max_retries: int = 2) -> tuple[ChatClient, FakeCompletions]:
    completions = FakeCompletions(responses)
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    settings = Settings(
        llm_base_url="https://example.invalid/v1",
        llm_api_key="test-key-not-real",
        llm_model="fake-model",
        llm_max_retries=max_retries,
        llm_timeout_seconds=5,
    )
    return ChatClient(settings, client=fake), completions


def evidence_from_fixture() -> tuple[list[Evidence], dict[str, str]]:
    from repoask.retrieve.context import build_context
    from repoask.retrieve.keyword import KeywordIndex, SearchHit

    build = build_local_index(FIXTURES, repo_id=DEFAULT_REPO_ID)
    search = KeywordIndex(
        build.index.chunks, repo_id=build.index.repo_id, commit=build.index.commit
    )
    hits = search.search("load_tasks", top_k=2)
    bundle = build_context(
        hits, repo_id=build.index.repo_id, commit=build.index.commit, repo_url=None
    )
    return bundle.evidence, build.file_texts


def test_complete_returns_text_model_and_usage() -> None:
    client, _ = make_client([completion("pong")])
    response = client.complete([{"role": "user", "content": "ping"}])
    assert response.text == "pong"
    assert response.model == "fake-model"
    assert response.usage is not None
    assert response.usage.total_tokens == 15
    assert response.attempts == 1


def test_missing_usage_is_none_not_zero() -> None:
    client, _ = make_client([completion("pong", usage=False)])
    response = client.complete([{"role": "user", "content": "ping"}])
    assert response.usage is None


def test_retryable_error_is_retried_then_succeeds() -> None:
    client, completions = make_client([APITimeoutError("timed out"), completion("pong")])
    response = client.complete([{"role": "user", "content": "ping"}])
    assert response.attempts == 2
    assert len(completions.calls) == 2


def test_non_retryable_error_fails_immediately() -> None:
    client, completions = make_client([AuthenticationError("bad key")])
    with pytest.raises(LLMUnavailable):
        client.complete([{"role": "user", "content": "ping"}])
    assert len(completions.calls) == 1


def test_retries_are_bounded() -> None:
    client, completions = make_client(
        [APITimeoutError("1"), APITimeoutError("2"), APITimeoutError("3")]
    )
    with pytest.raises(LLMUnavailable):
        client.complete([{"role": "user", "content": "ping"}])
    assert len(completions.calls) == 3  # 1 attempt + 2 retries


def test_unconfigured_settings_raise_instead_of_calling_out() -> None:
    with pytest.raises(LLMUnavailable):
        ChatClient(Settings())


def test_llm_answerer_without_evidence_does_not_call_the_model() -> None:
    client, completions = make_client([completion("should not be used")])
    draft = LLMAnswerer(client).answer("问题", [], "")
    assert draft.insufficient_evidence is True
    assert draft.termination_reason == "no_evidence"
    assert completions.calls == []


def test_llm_answerer_returns_model_text_and_usage() -> None:
    client, _ = make_client([completion("根据 [E1]，load_tasks 会加载任务。")])
    evidence, _ = evidence_from_fixture()
    draft = LLMAnswerer(client).answer("load_tasks 做了什么？", evidence, "context")
    assert draft.mode == "llm"
    assert draft.usage is not None
    assert "[E1]" in draft.answer_text
    assert draft.termination_reason == "answered"


class StubAnswerer:
    """Deterministic stand-in that produces a bad citation first, then a good one."""

    name = "stub"

    def __init__(self) -> None:
        self.revise_calls = 0

    def answer(self, question: str, evidence: list[Evidence], rendered: str) -> AnswerDraft:
        return AnswerDraft(
            answer_text="答案引用了不存在的证据 [E9]。",
            mode="llm",
            model="stub-model",
            termination_reason="answered",
        )

    def revise(
        self,
        question: str,
        evidence: list[Evidence],
        rendered: str,
        previous_answer: str,
        problems: list[str],
    ) -> AnswerDraft:
        self.revise_calls += 1
        assert problems, "the pipeline must pass the citation problems to the reviser"
        first = evidence[0].evidence_id
        return AnswerDraft(
            answer_text=f"修正后的答案，见 [{first}]。",
            mode="llm",
            model="stub-model",
            termination_reason="answered",
        )


def test_pipeline_repairs_one_invalid_citation_and_reverifies() -> None:
    build = build_local_index(FIXTURES, repo_id=DEFAULT_REPO_ID)
    stub = StubAnswerer()
    result = ask(
        build.index,
        "load_tasks 做了什么？",
        answerer=stub,
        file_texts=build.file_texts,
    )
    assert stub.revise_calls == 1
    assert result.citation_report.all_valid
    assert any("修正" in note for note in result.notes)
