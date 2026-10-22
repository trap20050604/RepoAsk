"""Core data models shared by ingestion, retrieval, answering and citation checks.

Design rule that everything else depends on:
a chunk's ``content`` is *always* exactly the original source lines
``source_lines[start_line-1 : end_line]`` joined with "\\n". We never
re-format code and never derive line numbers from a re-printed tree.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ChunkKind = Literal["function", "method", "nested_function", "class", "text"]
AnswerMode = Literal["offline_extractive", "llm", "agent"]


def content_sha256(text: str) -> str:
    """Stable content hash used to bind a chunk to its exact bytes."""
    return sha256(text.encode("utf-8")).hexdigest()


class ParentContext(BaseModel):
    """Enclosing signature kept alongside a chunk, with its own line numbers.

    Stored separately (never merged into ``content``) so that a chunk's text
    stays a contiguous slice of the original file.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str | None = None
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content: str

    @model_validator(mode="after")
    def _range_valid(self) -> "ParentContext":
        if self.end_line < self.start_line:
            raise ValueError("parent_context end_line must be >= start_line")
        return self


class ChunkMetadata(BaseModel):
    """Metadata required to identify a chunk's exact version."""

    model_config = ConfigDict(extra="forbid")

    repo_id: str = Field(min_length=1)
    commit: str = Field(min_length=1)
    path: str = Field(min_length=1)
    symbol: str | None = None
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content_hash: str = Field(min_length=16)

    @model_validator(mode="after")
    def _range_valid(self) -> "ChunkMetadata":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


class Chunk(ChunkMetadata):
    """A retrievable unit of source code or text."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    kind: ChunkKind
    language: str = "python"
    content: str
    parent_symbol: str | None = None
    parent_context: ParentContext | None = None
    part_index: int | None = Field(default=None, ge=1)
    part_count: int | None = Field(default=None, ge=1)
    has_syntax_error: bool = False
    encoding: str = "utf-8"

    @model_validator(mode="after")
    def _hash_matches_content(self) -> "Chunk":
        # The hash is derived from content so metadata can never drift from text.
        expected = content_sha256(self.content)
        if self.content_hash and self.content_hash != expected:
            raise ValueError(
                f"content_hash does not match content for chunk {self.chunk_id}"
            )
        object.__setattr__(self, "content_hash", expected)
        if self.part_count is not None and self.part_index is None:
            raise ValueError("part_count requires part_index")
        return self

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1

    def citation_label(self) -> str:
        symbol = f" ({self.symbol})" if self.symbol else ""
        return f"{self.path}:{self.start_line}-{self.end_line}{symbol}"


class Evidence(BaseModel):
    """A chunk handed to the answerer under a short id such as ``E1``."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(pattern=r"^E\d+$")
    repo_id: str
    commit: str
    path: str
    symbol: str | None = None
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content: str
    content_hash: str
    source: str = "keyword"
    score: float | None = None
    parent_context: ParentContext | None = None
    url: str | None = None

    @property
    def label(self) -> str:
        symbol = f" ({self.symbol})" if self.symbol else ""
        return f"{self.path}:{self.start_line}-{self.end_line}{symbol}"


class CitationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class CitationCheck(BaseModel):
    """Result of validating one ``[E#]`` marker against the provided evidence."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    valid: bool
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    url: str | None = None
    # True only when the cited lines were compared with real file content.
    source_checked: bool = False
    issues: list[CitationIssue] = Field(default_factory=list)


class CitationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checks: list[CitationCheck] = Field(default_factory=list)
    cited_ids: list[str] = Field(default_factory=list)
    unknown_ids: list[str] = Field(default_factory=list)
    uncited_evidence_ids: list[str] = Field(default_factory=list)
    answer_has_citation: bool = False

    @property
    def valid_count(self) -> int:
        return sum(1 for c in self.checks if c.valid)

    @property
    def checked_count(self) -> int:
        return len(self.checks)

    @property
    def source_checked_count(self) -> int:
        return sum(1 for c in self.checks if c.source_checked)

    @property
    def valid_rate(self) -> float | None:
        """Share of cited ids that passed validation; None when nothing was cited."""
        if self.checked_count == 0:
            return None
        return self.valid_count / self.checked_count

    @property
    def all_valid(self) -> bool:
        return self.checked_count > 0 and self.valid_count == self.checked_count


class Usage(BaseModel):
    """Token accounting. ``None`` means the provider did not report it."""

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    note: str | None = None


class AnswerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    answer_text: str
    mode: AnswerMode
    model: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    citation_report: CitationReport = Field(default_factory=CitationReport)
    termination_reason: str
    elapsed_ms: int | None = None
    usage: Usage | None = None
    notes: list[str] = Field(default_factory=list)
    insufficient_evidence: bool = False
