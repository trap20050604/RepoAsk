"""NO_PROXY 兼容层的测试。

这些用例复现一个真实故障：代理客户端把 NO_PROXY 写成含 ``[::1]`` 的形式，
httpx 在构造 Client 时抛 ``InvalidURL: Invalid port``，于是聊天与 embedding
客户端都无法创建。
"""

from __future__ import annotations

import httpx
import pytest

from repoask.answer.llm import ChatClient
from repoask.config import Settings
from repoask.embed.client import EmbeddingClient
from repoask.httpcompat import sanitize_no_proxy

BROKEN_NO_PROXY = "localhost,127.0.0.1,::1,[::1]"


def test_sanitize_removes_bracketed_ipv6_and_keeps_the_rest(monkeypatch) -> None:
    monkeypatch.setenv("NO_PROXY", BROKEN_NO_PROXY)
    removed = sanitize_no_proxy()
    assert removed == ["[::1]"]
    import os

    assert os.environ["NO_PROXY"].split(",") == ["localhost", "127.0.0.1", "::1"]


def test_sanitize_is_idempotent(monkeypatch) -> None:
    monkeypatch.setenv("NO_PROXY", "localhost,::1")
    assert sanitize_no_proxy() == []
    assert sanitize_no_proxy() == []


def test_httpx_can_construct_after_sanitizing(monkeypatch) -> None:
    monkeypatch.setenv("NO_PROXY", BROKEN_NO_PROXY)
    with pytest.raises(httpx.InvalidURL):
        httpx.Client()
    sanitize_no_proxy()
    client = httpx.Client()
    client.close()


def test_embedding_client_builds_with_broken_no_proxy(monkeypatch) -> None:
    monkeypatch.setenv("NO_PROXY", BROKEN_NO_PROXY)
    settings = Settings(
        embedding_base_url="https://example.invalid/v1",
        embedding_api_key="test-key-not-real",
        embedding_model="test-embedding",
        embedding_dimensions=8,
    )
    client = EmbeddingClient(settings)
    assert client.model == "test-embedding"
    assert client.sanitized_no_proxy == ["[::1]"]


def test_chat_client_builds_with_broken_no_proxy(monkeypatch) -> None:
    monkeypatch.setenv("NO_PROXY", BROKEN_NO_PROXY)
    settings = Settings(
        llm_base_url="https://example.invalid/v1",
        llm_api_key="test-key-not-real",
        llm_model="test-chat",
    )
    client = ChatClient(settings)
    assert client.model == "test-chat"
    assert client.sanitized_no_proxy == ["[::1]"]
