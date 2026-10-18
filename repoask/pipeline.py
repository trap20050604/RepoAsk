"""End-to-end orchestration: build an index from a local repo, then answer.

Stage 1 flow (all steps are real, nothing is mocked):
    scan files -> decode -> chunk -> lexical search -> budgeted context
    -> single-pass answer -> citation verification -> JSONL trace
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from repoask.answer.single_pass import (
    AnswerDraft,
    Answerer,
    OfflineExtractiveAnswerer,
)
from repoask.cite.urls import normalize_repo_url
from repoask.cite.verify import verify_citations
from repoask.errors import ParseError
from repoask.index import ChunkIndex, EmbeddingInfo, SkippedFile, utc_now_iso
from repoask.ingest.local import (
    IngestLimits,
    read_bytes_checked,
    resolve_git_commit,
    scan_repository,
)
from repoask.models import AnswerResult, Chunk, Evidence
from repoask.parse import (
    DEFAULT_MAX_CHUNK_CHARS,
    DEFAULT_MAX_CHUNK_LINES,
    DEFAULT_OVERLAP_LINES,
    chunk_source,
    decode_source,
)
from repoask.retrieve.context import ContextBudget, build_context
from repoask.retrieve.keyword import DEFAULT_KIND_WEIGHTS, KeywordIndex
from repoask.runlog import RunLogger


@dataclass
class LocalBuild:
    index: ChunkIndex
    file_texts: dict[str, str] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def compute_worktree_commit(file_texts: dict[str, str]) -> str:
    """Deterministic version id for a directory that is not a git work tree.

    Not a git commit: it is a content digest, and it is labelled as such in the
    index (``commit_source="worktree_digest"``) so nobody mistakes it for one.
    """
    hasher = sha256()
    for path in sorted(file_texts):
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(sha256(file_texts[path].encode("utf-8")).digest())
    return f"worktree-{hasher.hexdigest()[:16]}"


def build_local_index(
    root: str | Path,
    *,
    repo_id: str | None = None,
    commit: str | None = None,
    repo_url: str | None = None,
    limits: IngestLimits | None = None,
    max_chunk_lines: int = DEFAULT_MAX_CHUNK_LINES,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
    embedding: EmbeddingInfo | None = None,
) -> LocalBuild:
    """Scan, decode and chunk a local repository into a version-bound index."""
    started = time.perf_counter()
    scan = scan_repository(root, limits)
    resolved_repo_id = repo_id or scan.root.name
    normalized_url = normalize_repo_url(repo_url) if repo_url else None

    file_texts: dict[str, str] = {}
    encodings: dict[str, str] = {}
    for scanned in scan.files:
        raw = read_bytes_checked(scan.root, scanned.rel_path)
        text, encoding = decode_source(raw)
        file_texts[scanned.rel_path] = text
        encodings[scanned.rel_path] = encoding

    if commit:
        resolved_commit, commit_source = commit, "explicit"
    else:
        git_commit = resolve_git_commit(scan.root)
        if git_commit:
            resolved_commit, commit_source = git_commit, "git"
        else:
            resolved_commit, commit_source = compute_worktree_commit(file_texts), "worktree_digest"

    chunks: list[Chunk] = []
    skipped: list[tuple[str, str]] = list(scan.skipped)
    indexed_files: set[str] = set()
    for scanned in scan.files:
        try:
            file_chunks = chunk_source(
                file_texts[scanned.rel_path],
                repo_id=resolved_repo_id,
                commit=resolved_commit,
                path=scanned.rel_path,
                encoding=encodings[scanned.rel_path],
                language=scanned.language,
                max_chunk_lines=max_chunk_lines,
                max_chunk_chars=max_chunk_chars,
                overlap_lines=overlap_lines,
            )
        except ParseError as exc:
            skipped.append((scanned.rel_path, f"parse_error: {exc}"))
            continue
        if not file_chunks:
            skipped.append((scanned.rel_path, "no_chunks"))
            continue
        chunks.extend(file_chunks)
        indexed_files.add(scanned.rel_path)

    chunks.sort(key=lambda chunk: (chunk.path, chunk.start_line, chunk.end_line))
    index = ChunkIndex(
        repo_id=resolved_repo_id,
        commit=resolved_commit,
        source="local",
        commit_source=commit_source,  # type: ignore[arg-type]
        repo_url=normalized_url,
        created_at=utc_now_iso(),
        chunks=chunks,
        skipped=[SkippedFile(path=path, reason=reason) for path, reason in skipped],
        embedding=embedding,
        stats={
            "files_scanned": len(scan.files),
            "files_indexed": len(indexed_files),
            "files_skipped": len(skipped),
            "chunks": len(chunks),
            "total_bytes": scan.total_bytes,
            "scan_ms": scan.elapsed_ms,
            "build_ms": int((time.perf_counter() - started) * 1000),
            "max_chunk_lines": max_chunk_lines,
            "max_chunk_chars": max_chunk_chars,
            "overlap_lines": overlap_lines,
        },
    )
    return LocalBuild(index=index, file_texts=file_texts, skipped=skipped)


def _citation_problems(report) -> list[str]:
    problems: list[str] = []
    for check in report.checks:
        if check.valid:
            continue
        for issue in check.issues:
            problems.append(f"[{check.evidence_id}] {issue.code}: {issue.message}")
    return problems


def ask(
    index: ChunkIndex,
    question: str,
    *,
    answerer: Answerer | None = None,
    top_k: int = 6,
    budget: ContextBudget | None = None,
    file_texts: dict[str, str] | None = None,
    logger: RunLogger | None = None,
) -> AnswerResult:
    """Answer one question against a fixed index, with verified citations."""
    question = (question or "").strip()
    if not question:
        raise ValueError("question must not be empty")
    started = time.perf_counter()
    active_answerer: Answerer = answerer or OfflineExtractiveAnswerer()
    notes: list[str] = []

    search = KeywordIndex(
        index.chunks,
        repo_id=index.repo_id,
        commit=index.commit,
        kind_weights=DEFAULT_KIND_WEIGHTS,
    )
    hits = search.search(question, top_k=top_k)
    if logger:
        logger.record(
            "search",
            question=question,
            top_k=top_k,
            matches=[
                {
                    "path": hit.chunk.path,
                    "start_line": hit.chunk.start_line,
                    "end_line": hit.chunk.end_line,
                    "symbol": hit.chunk.symbol,
                    "score": hit.score,
                    "terms": hit.matched_terms,
                }
                for hit in hits
            ],
        )

    bundle = build_context(
        hits,
        repo_id=index.repo_id,
        commit=index.commit,
        repo_url=index.repo_url,
        budget=budget or ContextBudget(),
    )
    if logger:
        logger.record(
            "context",
            evidence=[
                {
                    "id": item.evidence_id,
                    "path": item.path,
                    "start_line": item.start_line,
                    "end_line": item.end_line,
                    "symbol": item.symbol,
                }
                for item in bundle.evidence
            ],
            used_chars=bundle.used_chars,
            dropped=[dropped.model_dump() for dropped in bundle.dropped],
        )

    draft: AnswerDraft = active_answerer.answer(question, bundle.evidence, bundle.rendered)
    report = verify_citations(
        draft.answer_text,
        bundle.evidence,
        repo_id=index.repo_id,
        commit=index.commit,
        repo_url=index.repo_url,
        file_texts=file_texts,
    )

    revise = getattr(active_answerer, "revise", None)
    if report.checked_count and not report.all_valid and callable(revise):
        problems = _citation_problems(report)
        revised: AnswerDraft = revise(
            question, bundle.evidence, bundle.rendered, draft.answer_text, problems
        )
        revised_report = verify_citations(
            revised.answer_text,
            bundle.evidence,
            repo_id=index.repo_id,
            commit=index.commit,
            repo_url=index.repo_url,
            file_texts=file_texts,
        )
        if revised_report.valid_count >= report.valid_count:
            draft = revised
            report = revised_report
            notes.append("引用检查失败后执行了一次修正。")
        else:
            notes.append("引用修正没有改善结果，保留第一次回答。")

    if not report.answer_has_citation:
        notes.append("回答没有引用标记，无法把结论映射到具体代码行。")
    elif not report.all_valid:
        notes.append(
            f"有 {report.checked_count - report.valid_count} 条引用未通过程序检查，"
            "答案的可追溯性受限。"
        )
    if report.uncited_evidence_ids:
        notes.append(f"未使用的证据：{', '.join(report.uncited_evidence_ids)}")
    if bundle.dropped:
        reasons = sorted({dropped.reason for dropped in bundle.dropped})
        notes.append(f"上下文丢弃了 {len(bundle.dropped)} 条候选（原因：{', '.join(reasons)}）")
    notes.extend(draft.notes)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if logger:
        logger.record(
            "answer",
            mode=draft.mode,
            model=draft.model,
            termination_reason=draft.termination_reason,
            insufficient_evidence=draft.insufficient_evidence,
            answer_chars=len(draft.answer_text),
        )
        logger.record(
            "citation_check",
            cited=report.cited_ids,
            valid=report.valid_count,
            checked=report.checked_count,
            source_checked=report.source_checked_count,
            unknown=report.unknown_ids,
            issues=[problem for problem in _citation_problems(report)],
        )
        logger.record(
            "done",
            elapsed_ms=elapsed_ms,
            usage=draft.usage.model_dump() if draft.usage else "unavailable",
        )

    return AnswerResult(
        question=question,
        answer_text=draft.answer_text,
        mode=draft.mode,
        model=draft.model,
        evidence=bundle.evidence,
        citation_report=report,
        termination_reason=draft.termination_reason,
        elapsed_ms=elapsed_ms,
        usage=draft.usage,
        notes=notes,
        insufficient_evidence=draft.insufficient_evidence,
    )


def ask_local(
    root: str | Path,
    question: str,
    *,
    answerer: Answerer | None = None,
    repo_id: str | None = None,
    commit: str | None = None,
    repo_url: str | None = None,
    top_k: int = 6,
    budget: ContextBudget | None = None,
    logger: RunLogger | None = None,
) -> AnswerResult:
    """Convenience wrapper: build a fresh local index and answer in one call."""
    build = build_local_index(root, repo_id=repo_id, commit=commit, repo_url=repo_url)
    if logger:
        logger.record("index", **build.index.summary())
    return ask(
        build.index,
        question,
        answerer=answerer,
        top_k=top_k,
        budget=budget,
        file_texts=build.file_texts,
        logger=logger,
    )


def evidence_by_id(evidence: list[Evidence]) -> dict[str, Evidence]:
    return {item.evidence_id: item for item in evidence}
