"""Embedding client for OpenAI-compatible endpoints.

Kept separate from the chat client on purpose: the two providers are configured
independently, and the embedding model name plus its real output dimension are
stored in the index so an index built with one model can never be queried with
another.

The configured dimension is treated as a claim to be checked, not as fact: if the
endpoint returns a different vector length, the call fails loudly.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict

from repoask.config import Settings
from repoask.errors import EmbeddingUnavailable
from repoask.httpcompat import sanitize_no_proxy

RETRYABLE_ERROR_NAMES = frozenset(
    {"APITimeoutError", "APIConnectionError", "RateLimitError", "InternalServerError"}
)

DEFAULT_BATCH_SIZE = 16


class EmbeddingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vectors: list[list[float]]
    model: str | None = None
    dimensions: int
    prompt_tokens: int | None = None
    latency_ms: int
    attempts: int = 1


class EmbeddingProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    model: str | None = None
    configured_dimensions: int
    returned_dimensions: int | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    detail: str | None = None


def _vector_of(item: Any) -> list[float]:
    embedding = getattr(item, "embedding", None)
    if embedding is None and isinstance(item, dict):
        embedding = item.get("embedding")
    if embedding is None:
        raise EmbeddingUnavailable("embedding response item has no vector")
    return [float(value) for value in embedding]


class EmbeddingClient:
    def __init__(
        self,
        settings: Settings,
        client: Any | None = None,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if not settings.embedding_configured:
            raise EmbeddingUnavailable(
                "EMBEDDING_BASE_URL, EMBEDDING_API_KEY and EMBEDDING_MODEL must all be set"
            )
        self.settings = settings
        self.model = settings.embedding_model
        self.batch_size = max(1, batch_size)
        if client is not None:
            self._client = client
            self.sanitized_no_proxy: list[str] = []
        else:
            from openai import OpenAI  # lazy import: offline tests need no SDK call

            # 某些代理客户端把 NO_PROXY 写成含 [::1] 的形式，httpx 解析会抛
            # InvalidURL，导致客户端根本建不起来；先做一次安全清理。
            self.sanitized_no_proxy = sanitize_no_proxy()
            self._client = OpenAI(
                api_key=settings.embedding_api_key,
                base_url=settings.embedding_base_url,
                timeout=settings.llm_timeout_seconds,
                max_retries=0,
            )

    def _embed_batch(self, texts: list[str]) -> tuple[list[list[float]], int | None, int, str | None]:
        attempts = self.settings.llm_max_retries + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.embeddings.create(model=self.model, input=texts)
            except Exception as exc:  # noqa: BLE001 - classified below
                last_error = exc
                if type(exc).__name__ in RETRYABLE_ERROR_NAMES and attempt < attempts:
                    time.sleep(min(0.5 * attempt, 2.0))
                    continue
                raise EmbeddingUnavailable(
                    f"embedding call failed after {attempt} attempt(s): "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            data = list(getattr(response, "data", []) or [])
            if len(data) != len(texts):
                raise EmbeddingUnavailable(
                    f"endpoint returned {len(data)} vectors for {len(texts)} inputs"
                )
            vectors = [_vector_of(item) for item in data]
            usage = getattr(response, "usage", None)
            tokens = getattr(usage, "prompt_tokens", None) if usage is not None else None
            model = getattr(response, "model", self.model)
            return vectors, tokens, attempt, model
        raise EmbeddingUnavailable(f"embedding call failed after {attempts} attempts: {last_error}")

    def embed(self, texts: list[str]) -> EmbeddingResponse:
        if not texts:
            raise ValueError("texts must not be empty")
        started = time.perf_counter()
        vectors: list[list[float]] = []
        tokens: int | None = None
        attempts = 0
        model: str | None = self.model
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            batch_vectors, batch_tokens, batch_attempts, model = self._embed_batch(batch)
            vectors.extend(batch_vectors)
            attempts = max(attempts, batch_attempts)
            if batch_tokens is not None:
                tokens = (tokens or 0) + batch_tokens
        dimensions = len(vectors[0])
        for vector in vectors:
            if len(vector) != dimensions:
                raise EmbeddingUnavailable("endpoint returned vectors of inconsistent length")
        configured = self.settings.embedding_dimensions
        if configured and dimensions != configured:
            raise EmbeddingUnavailable(
                f"embedding model {self.model!r} returned {dimensions} dimensions but "
                f"EMBEDDING_DIMENSIONS={configured}; fix the config or rebuild the index"
            )
        return EmbeddingResponse(
            vectors=vectors,
            model=model,
            dimensions=dimensions,
            prompt_tokens=tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
            attempts=attempts,
        )

    def probe(self) -> EmbeddingProbe:
        """One real call: reports the true dimension instead of trusting config."""
        started = time.perf_counter()
        try:
            response = self.embed(["def load_tasks(names): return names"])
        except EmbeddingUnavailable as exc:
            return EmbeddingProbe(
                ok=False,
                model=self.settings.embedding_model,
                configured_dimensions=self.settings.embedding_dimensions,
                detail=str(exc),
            )
        return EmbeddingProbe(
            ok=True,
            model=response.model,
            configured_dimensions=self.settings.embedding_dimensions,
            returned_dimensions=response.dimensions,
            latency_ms=response.latency_ms
            if response.latency_ms
            else int((time.perf_counter() - started) * 1000),
            prompt_tokens=response.prompt_tokens,
            detail=(
                f"batch of 1, attempts={response.attempts}"
                + (
                    "；已从 NO_PROXY 移除 "
                    + ", ".join(self.sanitized_no_proxy)
                    if self.sanitized_no_proxy
                    else ""
                )
            ),
        )
