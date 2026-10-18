"""Tests for configuration loading, secret handling and JSONL tracing."""

from __future__ import annotations

from pathlib import Path

import pytest

from repoask.config import Settings
from repoask.errors import ConfigError
from repoask.runlog import RunLogger, read_jsonl

ENV_LINES = [
    "LLM_BASE_URL=https://example.invalid/v1",
    "LLM_API_KEY=sk-placeholder-not-real",
    "LLM_MODEL=chat-model-placeholder",
    "EMBEDDING_MODEL=embed-model-placeholder",
    "EMBEDDING_DIMENSIONS=8",
    "REPOASK_TOP_K=4",
]


def write_env(tmp_path: Path, lines: list[str]) -> Path:
    target = tmp_path / ".env"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY",
        "EMBEDDING_DIMENSIONS",
        "REPOASK_TOP_K",
    ):
        monkeypatch.delenv(name, raising=False)


def test_env_file_is_loaded_and_chat_is_separate_from_embeddings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_env(monkeypatch)
    settings = Settings.load(env_file=write_env(tmp_path, ENV_LINES))
    assert settings.llm_configured is True
    assert settings.llm_model == "chat-model-placeholder"
    # An embedding model name alone is not a configured embedding endpoint.
    assert settings.embedding_configured is False
    assert settings.embedding_dimensions == 8
    assert settings.top_k == 4


def test_process_environment_overrides_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_env(monkeypatch)
    env_file = write_env(tmp_path, ENV_LINES)
    monkeypatch.setenv("LLM_MODEL", "override-model")
    settings = Settings.load(env_file=env_file)
    assert settings.llm_model == "override-model"


def test_secret_never_appears_in_repr_or_safe_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_env(monkeypatch)
    settings = Settings.load(env_file=write_env(tmp_path, ENV_LINES))
    assert "sk-placeholder-not-real" not in repr(settings)
    summary = settings.safe_summary()
    assert summary["llm_api_key"] == "set"
    assert "sk-placeholder-not-real" not in str(summary)


def test_missing_configuration_is_reported_not_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_env(monkeypatch)
    settings = Settings.load(env_file=Path("definitely-missing.env"))
    assert settings.llm_configured is False
    assert settings.embedding_configured is False


def test_non_integer_dimensions_raise_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_env(monkeypatch)
    env_file = write_env(tmp_path, ["EMBEDDING_DIMENSIONS=not-a-number"])
    with pytest.raises(ConfigError):
        Settings.load(env_file=env_file)


def test_run_logger_redacts_credentials_and_truncates_long_values(tmp_path: Path) -> None:
    log_path = tmp_path / "run.jsonl"
    with RunLogger(log_path, run_id="fixed-run") as logger:
        logger.record("call", api_key="secret-value", token="tok-value", note="x" * 5000)
        logger.record("done", elapsed_ms=12)
    entries = read_jsonl(log_path)
    assert len(entries) == 2
    assert entries[0]["api_key"] == "<redacted>"
    assert entries[0]["token"] == "<redacted>"
    assert len(entries[0]["note"]) < 5000
    assert "truncated" in entries[0]["note"]
    assert all(entry["run_id"] == "fixed-run" for entry in entries)
    assert entries[1]["event"] == "done"


def test_closed_logger_refuses_further_writes(tmp_path: Path) -> None:
    logger = RunLogger(tmp_path / "run.jsonl")
    logger.close()
    with pytest.raises(RuntimeError):
        logger.record("late")
