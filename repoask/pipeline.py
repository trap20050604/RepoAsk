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
from repoask.errors import ParseError, VectorStoreError
from repoask.index import (
    ChunkIndex,
    EmbeddingInfo,
    SkippedFile,
    VectorRef,
    utc_now_iso,
)
from repoask.ingest.local import (
    IngestLimits,
    inspect_git_worktree,
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
from repoask.retrieve.keyword import DEFAULT_KIND_WEIGHTS, KeywordIndex, SearchHit
from repoask.runlog import RunLogger
from repoask.vector.store import VectorStore, collection_name


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
    embedder: object | None = None,
    vector_path: str | Path | None = None,
    embedding_provider: str | None = None,
) -> LocalBuild:
    """扫描、解码并把本地仓库分块成版本绑定的索引。

    传入 ``embedder``（实现 ``embed(texts) -> EmbeddingResponse``）时，
    同时写入向量并记录 embedding 模型与维度；此时必须给出 ``vector_path``。
    """
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

    git_state = inspect_git_worktree(scan.root)
    git_head = git_state.head if git_state is not None else None
    worktree_dirty = bool(git_state.dirty) if git_state is not None else False
    if commit:
        resolved_commit, commit_source = commit, "explicit"
    elif git_state is not None and not git_state.dirty:
        resolved_commit, commit_source = git_state.head, "git"
    else:
        # Either not a git work tree, or HEAD does not describe the bytes we just
        # read (modified/untracked indexable files). Labelling this as HEAD would
        # let two different contents share one commit id, so use a content digest
        # and keep HEAD only as provenance.
        resolved_commit = compute_worktree_commit(file_texts)
        commit_source = "worktree_digest"

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
        git_head=git_head,
        worktree_dirty=worktree_dirty,
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
    if embedder is not None:
        _attach_vectors(
            index,
            embedder,
            vector_path=vector_path,
            embedding_provider=embedding_provider,
        )
    return LocalBuild(index=index, file_texts=file_texts, skipped=skipped)


def _attach_vectors(
    index: ChunkIndex,
    embedder: object,
    *,
    vector_path: str | Path | None,
    embedding_provider: str | None,
) -> None:
    """把分块向量写入 Qdrant，并把模型/维度/集合位置写回索引。"""
    if not index.chunks:
        return
    if vector_path is None:
        raise ValueError("提供 embedder 时必须同时给出 vector_path")
    response = embedder.embed([chunk.content for chunk in index.chunks])  # type: ignore[attr-defined]
    model = response.model or "unknown"
    collection = collection_name(model, response.dimensions)
    with VectorStore(
        vector_path, collection=collection, dimensions=response.dimensions
    ) as store:
        # 同一版本重新分块（窗口变小）会产生新的 chunk_id，旧点不会被覆盖。
        # 旧点会占据查询候选并被“映射回索引”这一步丢弃，表现为假空结果，
        # 因此写入前先清掉该版本的全部旧点：同一版本只保留一份当前分块。
        store.delete_version(repo_id=index.repo_id, commit=index.commit)
        store.upsert_chunks(
            index.chunks,
            response.vectors,
            repo_id=index.repo_id,
            commit=index.commit,
            model=model,
        )
        stored = store.count()
    index.embedding = EmbeddingInfo(
        provider=embedding_provider, model=model, dimensions=response.dimensions
    )
    index.vectors = VectorRef(path=str(Path(vector_path).resolve()), collection=collection)
    index.stats["vectors"] = stored
    index.stats["embedding_ms"] = response.latency_ms
    if response.prompt_tokens is not None:
        index.stats["embedding_tokens"] = response.prompt_tokens


def search_vector(
    index: ChunkIndex,
    question: str,
    embedder: object,
    *,
    vector_path: str | Path | None = None,
    top_k: int = 6,
) -> list[SearchHit]:
    """用问题向量检索，并把命中映射回索引里的分块。

    索引与当前 embedder 的模型不一致时直接报错：不同模型的向量空间不可比。
    """
    if index.vectors is None:
        raise VectorStoreError("该索引没有向量，请先用 --embed 重建索引")
    question = (question or "").strip()
    if not question:
        raise ValueError("question must not be empty")
    response = embedder.embed([question])  # type: ignore[attr-defined]
    model = response.model or "unknown"
    if index.embedding is not None and index.embedding.model != model:
        raise VectorStoreError(
            f"索引由 {index.embedding.model!r} 构建，当前 embedder 是 {model!r}，"
            "向量空间不同，请重建索引"
        )
    store_path = vector_path or index.vectors.path
    # 查询只读：create=False，缺目录/缺集合直接报错，不静默建空库。
    with VectorStore(
        store_path,
        collection=index.vectors.collection,
        dimensions=response.dimensions,
        create=False,
    ) as store:
        # 多取一些候选：历史索引可能残留不在当前分块里的点，它们会在下面被
        # 映射丢弃，若只取 top_k 就会表现为“命中变少”甚至假空。
        raw_hits = store.search(
            response.vectors[0],
            repo_id=index.repo_id,
            commit=index.commit,
            top_k=max(top_k * 3, top_k + 10),
        )
    by_id = {chunk.chunk_id: chunk for chunk in index.chunks}
    hits: list[SearchHit] = []
    for raw in raw_hits:
        chunk_id = raw.payload.get("chunk_id")
        chunk = by_id.get(str(chunk_id)) if chunk_id is not None else None
        if chunk is None:
            continue
        hits.append(
            SearchHit(chunk=chunk, score=raw.score, matched_terms=[], source="vector")
        )
        if len(hits) >= top_k:
            break
    return hits


def load_indexed_source_texts(
    root: str | Path,
    index: ChunkIndex,
    *,
    limits: IngestLimits | None = None,
) -> dict[str, str]:
    """Load source text for citation checks, under the same safety boundary.

    Only paths that are both *in the index* and *scanned safely* are read, so
    verification cannot reach a symlink target outside the repository or a cache
    directory that ingestion deliberately skips. Files added after indexing are
    ignored; index paths missing from the scan stay unverified (and are reported
    as ``source_unavailable`` by the citation check).
    """
    scan = scan_repository(root, limits)
    available = {item.rel_path for item in scan.files}
    wanted = sorted({chunk.path for chunk in index.chunks} & available)
    texts: dict[str, str] = {}
    for rel_path in wanted:
        raw = read_bytes_checked(scan.root, rel_path)
        texts[rel_path], _ = decode_source(raw)
    return texts


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
    hits: list[SearchHit] | None = None,
) -> AnswerResult:
    """Answer one question against a fixed index, with verified citations.

    ``hits`` 用于外部传入候选（例如向量检索结果）；不传时执行关键词检索。
    这样关键词、向量、混合三条路径共用同一套上下文与引用校验。
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("question must not be empty")
    started = time.perf_counter()
    active_answerer: Answerer = answerer or OfflineExtractiveAnswerer()
    notes: list[str] = []

    if hits is None:
        search = KeywordIndex(
            index.chunks,
            repo_id=index.repo_id,
            commit=index.commit,
            kind_weights=DEFAULT_KIND_WEIGHTS,
        )
        hits = search.search(question, top_k=top_k)
    else:
        hits = list(hits)
    if logger:
        logger.record(
            "search",
            question=question,
            top_k=top_k,
            source=hits[0].source if hits else "none",
            matches=[
                {
                    "path": hit.chunk.path,
                    "start_line": hit.chunk.start_line,
                    "end_line": hit.chunk.end_line,
                    "symbol": hit.chunk.symbol,
                    "score": hit.score,
                    "terms": hit.matched_terms,
                    "source": hit.source,
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
        # A repair may only be adopted when it is strictly usable: every citation
        # valid, or an explicit "insufficient evidence" answer without citations.
        # Comparing counts alone would accept 1/2 -> 1/3 as an "improvement".
        revised_usable = revised_report.all_valid or (
            revised.insufficient_evidence and not revised_report.answer_has_citation
        )
        if revised_usable:
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
    if bundle.truncated:
        for truncated in bundle.truncated:
            notes.append(
                f"证据 {truncated.evidence_id} 因预算只展示前 {truncated.shown_lines} 行，"
                f"另有 {truncated.omitted_lines} 行未进入上下文。"
            )
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
            # NOT ``elapsed_ms``: that name is the logger's own column (time since
            # the run started) and passing the pipeline duration under the same
            # key silently overwrote it, producing non-monotonic traces.
            pipeline_elapsed_ms=elapsed_ms,
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
