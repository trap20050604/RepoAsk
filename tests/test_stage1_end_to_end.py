"""Stage 1 end-to-end tests: local repo -> chunks -> search -> answer -> citations.

These tests run entirely offline. The answerer is the deterministic extractive
baseline, so a passing test proves the pipeline wiring and the citation checks,
not the quality of a language model.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from repoask.errors import IndexVersionMismatch
from repoask.index import ChunkIndex
from repoask.pipeline import ask, ask_local, build_local_index
from repoask.runlog import RunLogger, read_jsonl
from repoask.testing import DEFAULT_REPO_ID

FIXTURES = Path(__file__).parent / "fixtures" / "demo_repo"


@pytest.fixture(scope="module")
def build():
    return build_local_index(FIXTURES, repo_id=DEFAULT_REPO_ID)


def test_index_is_bound_to_one_version_and_keeps_file_texts(build) -> None:
    index = build.index
    assert index.commit_source == "worktree_digest"
    assert index.commit.startswith("worktree-")
    assert index.chunk_count > 10
    assert index.stats["files_indexed"] >= 6
    assert build.file_texts
    assert all(chunk.repo_id == DEFAULT_REPO_ID for chunk in index.chunks)
    assert all(chunk.commit == index.commit for chunk in index.chunks)


def test_worktree_version_changes_when_content_changes(tmp_path: Path) -> None:
    copy = tmp_path / "repo"
    shutil.copytree(FIXTURES, copy)
    first = build_local_index(copy, repo_id=DEFAULT_REPO_ID).index.commit
    target = copy / "app" / "service.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n\ndef added_function():\n    return 1\n",
        encoding="utf-8",
    )
    second = build_local_index(copy, repo_id=DEFAULT_REPO_ID).index.commit
    assert first != second


def test_question_about_a_symbol_gets_source_checked_citations(build) -> None:
    result = ask(build.index, "load_tasks 做了什么？", file_texts=build.file_texts)
    assert result.termination_reason == "answered"
    assert result.evidence
    assert result.citation_report.answer_has_citation
    assert result.citation_report.all_valid
    assert (
        result.citation_report.source_checked_count == result.citation_report.checked_count
    )
    assert "load_tasks" in result.answer_text
    assert result.mode == "offline_extractive"
    # The offline baseline makes no model call, so no usage may be invented.
    assert result.usage is None
    assert result.elapsed_ms is not None and result.elapsed_ms >= 0


def test_off_topic_question_reports_insufficient_evidence(build) -> None:
    # Chosen because no token overlaps the fixture repo; a question that merely
    # looks unrelated can still share a common Chinese bigram (e.g. "时间").
    result = ask(build.index, "火星车的太阳能板功率是多少瓦？", file_texts=build.file_texts)
    assert result.insufficient_evidence is True
    assert result.termination_reason == "no_evidence"
    assert "不足以判断" in result.answer_text
    assert result.citation_report.checked_count == 0
    assert result.evidence == []


def test_empty_question_is_rejected(build) -> None:
    with pytest.raises(ValueError):
        ask(build.index, "   ")


def test_index_roundtrip_preserves_ids_and_rejects_other_versions(build, tmp_path: Path) -> None:
    path = build.index.save(tmp_path / "index.jsonl")
    loaded = ChunkIndex.load(path)
    assert [chunk.chunk_id for chunk in loaded.chunks] == [
        chunk.chunk_id for chunk in build.index.chunks
    ]
    loaded.assert_version(DEFAULT_REPO_ID, build.index.commit)
    with pytest.raises(IndexVersionMismatch):
        loaded.assert_version(DEFAULT_REPO_ID, "0" * 40)
    with pytest.raises(IndexVersionMismatch):
        loaded.assert_version("another-repo", build.index.commit)


def test_run_log_records_every_stage_without_credentials(build, tmp_path: Path) -> None:
    log_path = tmp_path / "run.jsonl"
    with RunLogger(log_path) as logger:
        ask(build.index, "load_tasks 做了什么？", file_texts=build.file_texts, logger=logger)
    entries = read_jsonl(log_path)
    assert [entry["event"] for entry in entries] == [
        "search",
        "context",
        "answer",
        "citation_check",
        "done",
    ]
    assert entries[0]["matches"], "search step should record its matches"
    assert entries[3]["checked"] >= 1
    assert "sk-" not in log_path.read_text(encoding="utf-8")
    # ``elapsed_ms`` is the logger's own monotonic column; a payload field must
    # never overwrite it (that produced 1 ms after 79 ms in an earlier trace).
    stamps = [entry["elapsed_ms"] for entry in entries]
    assert stamps == sorted(stamps), "trace timestamps must be monotonic"
    assert "pipeline_elapsed_ms" in entries[-1]
    assert entries[-1]["pipeline_elapsed_ms"] >= 0


def test_ask_local_builds_and_answers_in_one_call() -> None:
    result = ask_local(FIXTURES, "配置解析器怎么判断内容为空？", repo_id=DEFAULT_REPO_ID)
    assert result.answer_text
    assert result.citation_report.answer_has_citation
    assert result.citation_report.all_valid
