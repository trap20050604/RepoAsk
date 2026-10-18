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


class TruncatedEvidence(BaseModel):
    """An evidence block that had to be shortened to fit the character budget."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    path: str
    start_line: int
    end_line: int
    shown_lines: int
    omitted_lines: int


class ContextBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence: list[Evidence] = Field(default_factory=list)
    rendered: str = ""
    dropped: list[DroppedCandidate] = Field(default_factory=list)
    truncated: list[TruncatedEvidence] = Field(default_factory=list)
    used_chars: int = 0

    @property
    def evidence_ids(self) -> list[str]:
        return [item.evidence_id for item in self.evidence]


def _contains(outer: Chunk, inner: Chunk) -> bool:
    return outer.start_line <= inner.start_line and inner.end_line <= outer.end_line


def _block_parts(evidence: Evidence) -> tuple[list[str], list[str]]:
    """Split an evidence block into the fixed header lines and the numbered code.

    Separating them lets the budgeted renderer drop trailing code lines while
    keeping the header (id, path, line range) and every shown line number intact.
    """
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
    code = [
        f"{evidence.start_line + offset:>5}| {text}"
        for offset, text in enumerate(evidence.content.split("\n"))
    ]
    return lines, code


def render_evidence_block(evidence: Evidence) -> str:
    """Render one evidence block with original line numbers on the left."""
    header, code = _block_parts(evidence)
    return "\n".join(header + code)


def render_evidence_block_fitted(
    evidence: Evidence, max_chars: int
) -> tuple[str | None, int, int]:
    """Render one block no longer than ``max_chars``.

    Returns ``(text, shown_lines, omitted_lines)``, or ``(None, 0, total)`` when
    even the header plus the truncation marker cannot fit, in which case the
    caller must drop the candidate instead of exceeding the budget.
    """
    header, code = _block_parts(evidence)
    full = "\n".join(header + code)
    if len(full) <= max_chars:
        return full, len(code), 0

    kept: list[str] = []
    for line in code:
        kept.append(line)
        remaining = len(code) - len(kept)
        marker = f"... ({remaining} lines omitted by budget)" if remaining else ""
        candidate = "\n".join(header + kept + ([marker] if marker else []))
        if len(candidate) > max_chars:
            kept.pop()
            break

    remaining = len(code) - len(kept)
    if remaining:
        marker = f"... ({remaining} lines omitted by budget)"
        text = "\n".join(header + kept + [marker])
    else:  # pragma: no cover - only reachable with a huge header
        text = "\n".join(header + kept)
    if len(text) > max_chars:
        return None, 0, len(code)
    return text, len(kept), remaining


def render_evidence_block(evidence: Evidence) -> str:
    """Render one evidence block with original line numbers on the left."""
    header, code = _block_parts(evidence)
    return "\n".join(header + code)


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
        # Charge the block against what is already rendered, including the "\n\n"
        # separator. The remaining allowance is what this block may occupy, so a
        # single oversized first block is truncated instead of silently accepted.
        separator = 2 if bundle.rendered else 0
        available = budget.max_chars - len(bundle.rendered) - separator
        if available <= 0:
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
        text, shown, omitted = render_evidence_block_fitted(candidate, available)
        # A block reduced to "header + N lines omitted" shows no code at all, so
        # citing it would point at lines the model never saw. Drop it instead.
        if text is None or (omitted and shown == 0):
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
        if omitted:
            bundle.truncated.append(
                TruncatedEvidence(
                    evidence_id=candidate.evidence_id,
                    path=candidate.path,
                    start_line=candidate.start_line,
                    end_line=candidate.end_line,
                    shown_lines=shown,
                    omitted_lines=omitted,
                )
            )
        selected.append(chunk)
        bundle.evidence.append(candidate)
        bundle.rendered = f"{bundle.rendered}\n\n{text}" if bundle.rendered else text

    bundle.used_chars = len(bundle.rendered)
    return bundle
