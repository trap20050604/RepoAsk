"""Configuration loaded from environment variables (optionally a local ``.env``).

Secrets never have defaults, are never written to logs, and never appear in
``repr``/``safe_summary`` output.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from repoask.errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal ``.env`` reader: ``KEY=VALUE``, ``#`` comments, optional quotes.

    Kept dependency-free on purpose; it does not support inline comments after
    a value, nor ``export`` prefixes.
    """
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Chat model
    llm_base_url: str | None = None
    llm_api_key: str | None = Field(default=None, repr=False)
    llm_model: str | None = None
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = Field(default=2, ge=0, le=5)

    # Embedding model, configured independently from the chat model
    embedding_provider: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = Field(default=None, repr=False)
    embedding_model: str | None = None
    embedding_dimensions: int = 0

    # Retrieval / context budgets
    top_k: int = Field(default=6, ge=1, le=50)
    context_char_budget: int = Field(default=8000, ge=500, le=200_000)

    @classmethod
    def load(cls, env_file: Path | None = None, use_process_env: bool = True) -> "Settings":
        """Build settings from a ``.env`` file plus the process environment.

        Process environment wins over the file so a one-off override does not
        require editing ``.env``.
        """
        file_values: dict[str, str] = {}
        candidate = env_file or (PROJECT_ROOT / ".env")
        if candidate.is_file():
            file_values = _parse_env_file(candidate)

        def get(name: str, default: str | None = None) -> str | None:
            if use_process_env and name in os.environ and os.environ[name] != "":
                return os.environ[name]
            return file_values.get(name, default)

        raw_dims = get("EMBEDDING_DIMENSIONS", "0")
        try:
            dims = int(raw_dims or 0)
        except ValueError as exc:
            raise ConfigError("EMBEDDING_DIMENSIONS must be an integer") from exc

        timeout_raw = get("LLM_TIMEOUT_SECONDS", "60")
        retries_raw = get("LLM_MAX_RETRIES", "2")
        top_k_raw = get("REPOASK_TOP_K", "6")
        budget_raw = get("REPOASK_CONTEXT_CHAR_BUDGET", "8000")
        try:
            timeout = float(timeout_raw or 60)
            retries = int(retries_raw or 2)
            top_k = int(top_k_raw or 6)
            budget = int(budget_raw or 8000)
        except ValueError as exc:
            raise ConfigError(
                "LLM_TIMEOUT_SECONDS, LLM_MAX_RETRIES, REPOASK_TOP_K and "
                "REPOASK_CONTEXT_CHAR_BUDGET must be numeric"
            ) from exc

        return cls(
            llm_base_url=get("LLM_BASE_URL"),
            llm_api_key=get("LLM_API_KEY"),
            llm_model=get("LLM_MODEL"),
            llm_timeout_seconds=timeout,
            llm_max_retries=retries,
            embedding_provider=get("EMBEDDING_PROVIDER"),
            embedding_base_url=get("EMBEDDING_BASE_URL"),
            embedding_api_key=get("EMBEDDING_API_KEY"),
            embedding_model=get("EMBEDDING_MODEL"),
            embedding_dimensions=dims,
            top_k=top_k,
            context_char_budget=budget,
        )

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def embedding_configured(self) -> bool:
        return bool(self.embedding_base_url and self.embedding_api_key and self.embedding_model)

    def safe_summary(self) -> dict[str, object]:
        """Everything a log or a health endpoint may show: no secret values."""
        return {
            "llm_configured": self.llm_configured,
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "llm_api_key": "set" if self.llm_api_key else "unset",
            "llm_timeout_seconds": self.llm_timeout_seconds,
            "llm_max_retries": self.llm_max_retries,
            "embedding_configured": self.embedding_configured,
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "embedding_api_key": "set" if self.embedding_api_key else "unset",
            "top_k": self.top_k,
            "context_char_budget": self.context_char_budget,
        }
