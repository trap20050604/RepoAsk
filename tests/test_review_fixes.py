"""Reproduction tests for the independent review of commit 35caa13.

Each test states the defect in behavioural terms. They were written before the
fixes so that a failure proves the defect and a pass proves the fix.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from repoask.errors import UnsafePathError
from repoask.ingest.local import read_bytes_checked
from repoask.parse import decode_source
from repoask.parse.python_chunker import chunk_python_source
from repoask.pipeline import ask, build_local_index
from repoask.ingest.local import scan_repository
from repoask.testing import DEFAULT_REPO_ID

REPO_ID = "demo-repo"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")


def commit_all(repo: Path, message: str = "init") -> str:
    git(repo, "add", "-A")
    git(
        repo,
        "-c",
        "user.name=RepoAsk Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        message,
    )
    return git(repo, "rev-parse", "HEAD")


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return path


def chunks_of(text: str, **kwargs):
    return chunk_python_source(
        text, repo_id=REPO_ID, commit=COMMIT, path="app/x.py", **kwargs
    )


def symbols(chunks) -> dict:
    return {chunk.symbol: chunk for chunk in chunks if chunk.symbol}


# --------------------------------------------------------------------------
# 1. P1 working tree must not masquerade as a git commit
# --------------------------------------------------------------------------
def test_dirty_worktree_is_not_labelled_as_git_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_git_repo(repo)
    write(repo / "a.py", "def a():\n    return 1\n")
    head = commit_all(repo)

    clean = build_local_index(repo, repo_id="r")
    assert clean.index.commit == head
    assert clean.index.commit_source == "git"

    # A tracked file is modified but not committed: the scanned bytes are no
    # longer the bytes of HEAD, so HEAD must not be used as the version label.
    write(repo / "a.py", "def a():\n    return 2\n")
    dirty = build_local_index(repo, repo_id="r")
    assert dirty.index.commit != head
    assert dirty.index.commit_source == "worktree_digest"
    assert dirty.index.git_head == head
    assert dirty.index.worktree_dirty is True


def test_untracked_source_also_invalidates_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_git_repo(repo)
    write(repo / "a.py", "def a():\n    return 1\n")
    head = commit_all(repo)

    write(repo / "b.py", "def b():\n    return 2\n")  # never committed
    build = build_local_index(repo, repo_id="r")
    assert build.index.commit != head
    assert build.index.git_head == head
    assert build.index.worktree_dirty is True


# --------------------------------------------------------------------------
# 2. P1 index source verification must obey the import safety boundary
# --------------------------------------------------------------------------
def test_index_source_loading_ignores_symlinks_and_unsupported_files(tmp_path: Path) -> None:
    from repoask.pipeline import load_indexed_source_texts

    repo = tmp_path / "repo"
    write(repo / "a.py", "def a():\n    return 1\n")
    outside = tmp_path / "secret.py"
    write(outside, "def secret():\n    return 1\n")
    link = repo / "link.py"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks requires privileges on this machine")

    build = build_local_index(repo, repo_id="r")
    texts = load_indexed_source_texts(repo, build.index)
    assert "a.py" in texts
    assert "link.py" not in texts
    assert "secret" not in "\n".join(texts.values())


def test_index_source_loading_only_reads_indexed_paths(tmp_path: Path) -> None:
    from repoask.pipeline import load_indexed_source_texts

    repo = tmp_path / "repo"
    write(repo / "a.py", "def a():\n    return 1\n")
    build = build_local_index(repo, repo_id="r")
    # A file added after indexing must not be pulled in by the verifier.
    write(repo / "late.py", "def late():\n    return 2\n")
    texts = load_indexed_source_texts(repo, build.index)
    assert set(texts) == {"a.py"}


# --------------------------------------------------------------------------
# 3. P2 definitions nested inside control flow must be chunked
# --------------------------------------------------------------------------
def test_function_inside_if_inside_function_is_chunked() -> None:
    text = (
        "def outer():\n"
        "    if True:\n"
        "        def inner():\n"
        "            return 1\n"
        "    return inner\n"
    )
    found = symbols(chunks_of(text))
    assert "outer" in found
    assert "outer.inner" in found, "nested definition inside if-statement was dropped"
    inner = found["outer.inner"]
    assert inner.kind == "nested_function"
    assert inner.parent_symbol == "outer"
    assert inner.content == "        def inner():\n            return 1"


def test_module_level_conditional_definition_is_chunked() -> None:
    text = "if True:\n    def cond():\n        return 2\n"
    found = symbols(chunks_of(text))
    assert "cond" in found
    assert found["cond"].kind == "function"
    assert found["cond"].parent_symbol is None


def test_method_inside_if_inside_class_is_chunked() -> None:
    text = "class A:\n    if True:\n        def m(self):\n            return 1\n"
    found = symbols(chunks_of(text))
    assert "A.m" in found
    assert found["A.m"].kind == "method"
    assert found["A.m"].parent_symbol == "A"


# --------------------------------------------------------------------------
# 4. P2 syntax-error recovery must keep decorators
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "decorator_block",
    [
        "@decorate\n",
        "@decorate(\n    option=True,\n)\n",
        "@decorate\n\n",
        "@decorate\n# a note\n",
        "@decorate\n\n# a note\n",
    ],
    ids=["single", "multi-line", "blank", "comment", "blank+comment"],
)
def test_recovery_keeps_decorators(decorator_block: str) -> None:
    broken = '"""docstring"""\n\n\ndef broken(a, b:\n    return a + b\n\n\n'
    text = broken + decorator_block + "def later_function() -> int:\n    return 1\n"
    chunks = chunks_of(text)
    found = symbols(chunks)
    assert "later_function" in found, "healthy function after the broken block was lost"
    later = found["later_function"]
    expected_start = text.split("\n").index(decorator_block.split("\n")[0]) + 1
    assert later.start_line == expected_start, "recovered function must start at its decorator"
    assert later.content.split("\n")[0].startswith("@decorate")
    assert later.has_syntax_error is False


# --------------------------------------------------------------------------
# 5. P2 the context budget must actually bound the rendered context
# --------------------------------------------------------------------------
def test_context_budget_bounds_first_evidence_and_records_truncation() -> None:
    from repoask.retrieve.context import ContextBudget, build_context
    from repoask.retrieve.keyword import SearchHit
    from repoask.testing import make_chunk

    big = make_chunk("\n".join(f"value_{index} = {index}" for index in range(120)), start_line=1)
    bundle = build_context(
        [SearchHit(chunk=big, score=1.0)],
        repo_id=DEFAULT_REPO_ID,
        commit=COMMIT,
        budget=ContextBudget(max_chars=200, max_items=4),
    )
    assert len(bundle.rendered) <= 200, "rendered context exceeded max_chars"
    assert bundle.evidence, "a truncated-but-cited evidence should still be usable"
    assert bundle.truncated, "truncation must be recorded, not silent"
    assert bundle.truncated[0].omitted_lines > 0
    assert "omitted by budget" in bundle.rendered


def test_context_used_chars_counts_separators_exactly() -> None:
    from repoask.retrieve.context import ContextBudget, build_context
    from repoask.retrieve.keyword import SearchHit
    from repoask.testing import make_chunk

    first = make_chunk("def a():\n    return 1", start_line=1)
    second = make_chunk("def b():\n    return 2", start_line=20)
    bundle = build_context(
        [SearchHit(chunk=first, score=2.0), SearchHit(chunk=second, score=1.0)],
        repo_id=DEFAULT_REPO_ID,
        commit=COMMIT,
        budget=ContextBudget(max_chars=4000, max_items=4),
    )
    assert len(bundle.evidence) == 2
    assert bundle.used_chars == len(bundle.rendered)


def test_context_drops_evidence_that_cannot_show_any_code_line() -> None:
    """A block with only a header and "omitted" marker must not be cited."""
    from repoask.retrieve.context import ContextBudget, build_context
    from repoask.retrieve.keyword import SearchHit
    from repoask.testing import make_chunk

    long_line = "x = " + "1" * 400
    chunk = make_chunk("\n".join([long_line, long_line]), start_line=1)
    bundle = build_context(
        [SearchHit(chunk=chunk, score=1.0)],
        repo_id=DEFAULT_REPO_ID,
        commit=COMMIT,
        budget=ContextBudget(max_chars=200, max_items=4),
    )
    assert bundle.evidence == []
    assert bundle.rendered == ""
    assert bundle.dropped and bundle.dropped[0].reason == "budget"


# --------------------------------------------------------------------------
# 6. P2 a citation repair must not make the answer worse
# --------------------------------------------------------------------------
def test_revision_with_more_invalid_citations_is_not_adopted() -> None:
    from repoask.answer.single_pass import AnswerDraft
    from repoask.models import Evidence

    class WorseOnRevise:
        name = "stub"

        def answer(self, question: str, evidence: list[Evidence], rendered: str) -> AnswerDraft:
            return AnswerDraft(
                answer_text="初答见 [E1]，另有 [E9]。",
                mode="llm",
                model="stub",
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
            return AnswerDraft(
                answer_text="修正为 [E1] [E8] [E7]。",
                mode="llm",
                model="stub",
                termination_reason="answered",
            )

    build = build_local_index(
        Path(__file__).parent / "fixtures" / "demo_repo", repo_id=REPO_ID
    )
    result = ask(build.index, "load_tasks 做了什么？", answerer=WorseOnRevise(),
                 file_texts=build.file_texts)
    assert result.answer_text == "初答见 [E1]，另有 [E9]。"
    assert result.citation_report.checked_count == 2
    assert any("保留第一次回答" in note for note in result.notes)


# --------------------------------------------------------------------------
# 7. P2 bare CR line endings must not collapse the file onto one line
# --------------------------------------------------------------------------
def test_bare_cr_line_endings_keep_true_line_numbers() -> None:
    raw = b"def a():\r    return 1\r\r\ndef b():\r    return 2\r"
    text, encoding = decode_source(raw)
    chunks = chunk_python_source(text, repo_id=REPO_ID, commit=COMMIT, path="cr.py")
    found = symbols(chunks)
    assert found["a"].start_line == 1
    assert found["b"].start_line == 4
    assert found["b"].content == "def b():\n    return 2"
    lines = text.split("\n")
    for chunk in chunks:
        assert chunk.content == "\n".join(lines[chunk.start_line - 1 : chunk.end_line])


# --------------------------------------------------------------------------
# 8. P2 absolute paths must be rejected even when they point inside the root
# --------------------------------------------------------------------------
def test_absolute_path_inside_root_is_rejected(tmp_path: Path) -> None:
    target = write(tmp_path / "a.py", "def a():\n    return 1\n")
    with pytest.raises(UnsafePathError):
        read_bytes_checked(tmp_path, str(target))
    with pytest.raises(UnsafePathError):
        read_bytes_checked(tmp_path, str(target).replace("\\", "/"))
    with pytest.raises(UnsafePathError):
        read_bytes_checked(tmp_path, target.as_posix())
    # the relative form still works
    assert read_bytes_checked(tmp_path, "a.py") == b"def a():\n    return 1\n"
