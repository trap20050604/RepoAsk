"""Tests for citation validation and GitHub blob URL construction.

These tests pin the boundary between "program can prove the citation points at
real evidence" and "a human must judge whether the evidence supports the claim".
"""

from __future__ import annotations

import pytest

from repoask.cite.urls import (
    InvalidPath,
    InvalidRepoUrl,
    github_blob_url,
    normalize_repo_url,
    parse_github_repo_url,
    validate_relative_path,
)
from repoask.cite.verify import describe_report, extract_citation_ids, verify_citations
from repoask.models import Evidence, content_sha256
from repoask.testing import DEFAULT_COMMIT, DEFAULT_REPO_ID, make_chunk, make_evidence

PATH = "app/module.py"
CONTENT = "def load_tasks():\n    return 1"
REPO_URL = "https://github.com/trap20050604/RepoAsk"


def file_text_with(content: str, start_line: int, total_lines: int = 60) -> dict[str, str]:
    lines = [f"# filler {index}" for index in range(total_lines)]
    body = content.split("\n")
    lines[start_line - 1 : start_line - 1 + len(body)] = body
    return {PATH: "\n".join(lines)}


def test_extract_citation_ids_keeps_first_appearance_order() -> None:
    assert extract_citation_ids("见 [E2] 和 [E1]，再看 [E2]。") == ["E2", "E1"]
    assert extract_citation_ids("没有引用") == []


def test_valid_citation_is_verified_against_real_source_lines() -> None:
    chunk = make_chunk(CONTENT, path=PATH, start_line=37)
    evidence = make_evidence(chunk)
    report = verify_citations(
        "结论见 [E1]。",
        [evidence],
        repo_id=DEFAULT_REPO_ID,
        commit=DEFAULT_COMMIT,
        repo_url=REPO_URL,
        file_texts=file_text_with(CONTENT, 37),
    )
    assert report.all_valid
    assert report.valid_rate == 1.0
    assert report.source_checked_count == 1
    check = report.checks[0]
    assert check.url is not None and "#L37-L38" in check.url
    assert report.uncited_evidence_ids == []


def test_unknown_evidence_id_is_rejected() -> None:
    chunk = make_chunk(CONTENT, path=PATH, start_line=37)
    report = verify_citations(
        "结论见 [E7]。",
        [make_evidence(chunk)],
        repo_id=DEFAULT_REPO_ID,
        commit=DEFAULT_COMMIT,
    )
    assert report.unknown_ids == ["E7"]
    assert report.all_valid is False
    assert report.checks[0].issues[0].code == "unknown_evidence"


def test_evidence_from_another_commit_is_rejected() -> None:
    foreign = make_chunk(CONTENT, path=PATH, start_line=37, commit="b" * 40)
    report = verify_citations(
        "见 [E1]。",
        [make_evidence(foreign)],
        repo_id=DEFAULT_REPO_ID,
        commit=DEFAULT_COMMIT,
    )
    codes = {issue.code for issue in report.checks[0].issues}
    assert "wrong_commit" in codes
    assert report.all_valid is False


def test_tampered_content_fails_hash_check() -> None:
    chunk = make_chunk(CONTENT, path=PATH, start_line=37)
    evidence = make_evidence(chunk)
    tampered = evidence.model_copy(update={"content": "def load_tasks():\n    return 2"})
    report = verify_citations(
        "见 [E1]。", [tampered], repo_id=DEFAULT_REPO_ID, commit=DEFAULT_COMMIT
    )
    assert {issue.code for issue in report.checks[0].issues} == {"hash_mismatch"}


def test_stale_line_numbers_are_caught_against_the_file() -> None:
    """Same content, different real location: the hash agrees, the file does not."""
    chunk = make_chunk(CONTENT, path=PATH, start_line=37)
    evidence = make_evidence(chunk)
    stale = file_text_with("# moved elsewhere", 37)  # lines 37-38 no longer match
    report = verify_citations(
        "见 [E1]。",
        [evidence],
        repo_id=DEFAULT_REPO_ID,
        commit=DEFAULT_COMMIT,
        file_texts=stale,
    )
    assert {issue.code for issue in report.checks[0].issues} == {"content_mismatch"}
    assert report.checks[0].source_checked is False


def test_range_past_end_of_file_is_caught() -> None:
    chunk = make_chunk(CONTENT, path=PATH, start_line=37)
    report = verify_citations(
        "见 [E1]。",
        [make_evidence(chunk)],
        repo_id=DEFAULT_REPO_ID,
        commit=DEFAULT_COMMIT,
        file_texts={PATH: "only one line"},
    )
    assert {issue.code for issue in report.checks[0].issues} == {"range_out_of_file"}


def test_unsafe_path_is_rejected() -> None:
    chunk = make_chunk(CONTENT, path=PATH, start_line=1)
    evidence = make_evidence(chunk)
    traversal = evidence.model_copy(update={"path": "../secret.py"})
    report = verify_citations(
        "见 [E1]。", [traversal], repo_id=DEFAULT_REPO_ID, commit=DEFAULT_COMMIT
    )
    assert "unsafe_path" in {issue.code for issue in report.checks[0].issues}


def test_answer_without_citation_is_flagged_not_silently_accepted() -> None:
    chunk = make_chunk(CONTENT, path=PATH, start_line=1)
    report = verify_citations(
        "这是一个没有任何引用的回答。",
        [make_evidence(chunk)],
        repo_id=DEFAULT_REPO_ID,
        commit=DEFAULT_COMMIT,
    )
    assert report.answer_has_citation is False
    assert report.valid_rate is None
    assert report.all_valid is False
    assert report.uncited_evidence_ids == ["E1"]


def test_answer_with_no_evidence_has_nothing_to_check() -> None:
    report = verify_citations(
        "不足以判断。", [], repo_id=DEFAULT_REPO_ID, commit=DEFAULT_COMMIT
    )
    assert report.checked_count == 0
    assert report.answer_has_citation is False
    assert describe_report(report) == "没有引用标记"


def test_github_blob_url_contains_commit_and_line_anchor() -> None:
    url = github_blob_url(REPO_URL, DEFAULT_COMMIT, PATH, 12, 20)
    assert url == (
        f"https://github.com/trap20050604/RepoAsk/blob/{DEFAULT_COMMIT}/{PATH}#L12-L20"
    )
    single = github_blob_url(REPO_URL, DEFAULT_COMMIT, PATH, 7, 7)
    assert single.endswith("#L7")


def test_repository_url_validation_rejects_non_github_and_ssh() -> None:
    assert parse_github_repo_url(REPO_URL + ".git") == ("trap20050604", "RepoAsk")
    assert normalize_repo_url(REPO_URL + "/") == REPO_URL
    for bad in (
        "http://github.com/a/b",
        "https://gitlab.com/a/b",
        "git@github.com:a/b.git",
        "https://github.com/a",
        "https://github.com/a/b/tree/main",
    ):
        with pytest.raises(InvalidRepoUrl):
            parse_github_repo_url(bad)


def test_path_validation_blocks_absolute_and_traversal() -> None:
    assert validate_relative_path("app\\service.py") == "app/service.py"
    for bad in ("/etc/passwd", "C:/Windows/win.ini", "app/../../secret.py", ""):
        with pytest.raises(InvalidPath):
            validate_relative_path(bad)
