"""Context assembly: de-duplication, line-numbered rendering, length budget.

The rendered context always shows real file line numbers next to the code, so
the model can cite a range the program can verify. Parent signatures are shown
as a separate labelled block with their own line numbers - they are never spliced
into a chunk's text, which would break the "content is a contiguous slice"
invariant.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from repoask.cite.urls import github_blob_url
from repoask.models import Chunk, Evidence
from repoask.retrieve.keyword import SearchHit


class ContextBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_chars: int = Field(default=8000, ge=200)
    max_items: int = Field(default=6, ge=1)
    include_parent_context: bool = True


class DroppedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    path: str
    start_line: int
    end_line: int
    reason: str


class ContextBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence: list[Evidence] = Field(default_factory=list)
    rendered: str = ""
    dropped: list[DroppedCandidate] = Field(default_factory=list)
    used_chars: int = 0

    @property
    def evidence_ids(self) -> list[str]:
        return [item.evidence_id for item in self.evidence]


def _contains(outer: Chunk, inner: Chunk) -> bool:
    return outer.start_line <= inner.start_line and inner.end_line <= outer.end_line


def render_evidence_block(evidence: Evidence) -> str:
    """Render one evidence block with original line numbers on the left."""
    header = (
        f"[{evidence.evidence_id}] {evidence.path}:"
        f"{evidence.start_line}-{evidence.end_line}"
    )
    if evidence.symbol:
        header += f"  symbol={evidence.symbol}"
    if evidence.url:
        header += f"  url={evidence.url}"
    lines = [header]
    if evidence.parent_context is not None:
        parent = evidence.parent_context
        lines.append(
            f"  (enclosing definition, context only: "
            f"{evidence.path}:{parent.start_line}-{parent.end_line})"
        )
        for offset, text in enumerate(parent.content.split("\n")):
            lines.append(f"{parent.start_line + offset:>5}| {text}")
    for offset, text in enumerate(evidence.content.split("\n")):
        lines.append(f"{evidence.start_line + offset:>5}| {text}")
    return "\n".join(lines)


def render_context(evidence: list[Evidence]) -> str:
    return "\n\n".join(render_evidence_block(item) for item in evidence)


def build_context(
    hits: list[SearchHit],
    *,
    repo_id: str,
    commit: str,
    repo_url: str | None = None,
    budget: ContextBudget | None = None,
    evidence_prefix: str = "E",
) -> ContextBundle:
    """Turn ranked hits into de-duplicated, budgeted evidence with stable ids."""
    budget = budget or ContextBudget()
    bundle = ContextBundle()
    selected: list[Chunk] = []

    for hit in hits:
        chunk = hit.chunk
        if chunk.repo_id != repo_id or chunk.commit != commit:
            bundle.dropped.append(
                DroppedCandidate(
                    chunk_id=chunk.chunk_id,
                    path=chunk.path,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    reason="version_mismatch",
                )
            )
            continue
        if any(selected_chunk.content_hash == chunk.content_hash for selected_chunk in selected):
            bundle.dropped.append(
                DroppedCandidate(
                    chunk_id=chunk.chunk_id,
                    path=chunk.path,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    reason="duplicate_content",
                )
            )
            continue
        # Nested functions/classes repeat their parent's lines. Keep the first
        # (higher ranked) side only, which is what makes the budget go further.
        if any(
            other.path == chunk.path and (_contains(other, chunk) or _contains(chunk, other))
            for other in selected
        ):
            bundle.dropped.append(
                DroppedCandidate(
                    chunk_id=chunk.chunk_id,
                    path=chunk.path,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    reason="nested_overlap",
                )
            )
            continue
        if len(bundle.evidence) >= budget.max_items:
            bundle.dropped.append(
                DroppedCandidate(
                    chunk_id=chunk.chunk_id,
                    path=chunk.path,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    reason="max_items",
                )
            )
            continue

        url: str | None = None
        if repo_url:
            try:
                url = github_blob_url(repo_url, commit, chunk.path, chunk.start_line, chunk.end_line)
            except ValueError:
                url = None

        candidate = Evidence(
            evidence_id=f"{evidence_prefix}{len(bundle.evidence) + 1}",
            repo_id=chunk.repo_id,
            commit=chunk.commit,
            path=chunk.path,
            symbol=chunk.symbol,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            content=chunk.content,
            content_hash=chunk.content_hash,
            source="keyword",
            score=hit.score,
            parent_context=chunk.parent_context if budget.include_parent_context else None,
            url=url,
        )
        block = render_evidence_block(candidate)
        addition = len(block) + (2 if bundle.rendered else 0)
        if bundle.used_chars + addition > budget.max_chars and bundle.evidence:
            bundle.dropped.append(
                DroppedCandidate(
                    chunk_id=chunk.chunk_id,
                    path=chunk.path,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    reason="budget",
                )
            )
            continue
        selected.append(chunk)
        bundle.evidence.append(candidate)
        bundle.used_chars += addition

    bundle.rendered = render_context(bundle.evidence)
    return bundle
