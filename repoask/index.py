"""On-disk chunk index, bound to one repository and one commit.

Persistence is JSONL: a header line followed by one line per chunk. That keeps
the file greppable and lets a future stage stream large indexes instead of
loading everything at once.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from repoask.errors import IndexVersionMismatch
from repoask.models import Chunk

SCHEMA_VERSION = 1


class SkippedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    reason: str


class EmbeddingInfo(BaseModel):
    """Recorded even before vector search exists, so a stale index cannot be reused
    with a different embedding model or dimension count."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    model: str
    dimensions: int = Field(ge=0)


class ChunkIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    repo_id: str
    commit: str
    source: Literal["local", "github"] = "local"
    commit_source: Literal["git", "worktree_digest", "explicit"] = "explicit"
    repo_url: str | None = None
    created_at: str
    chunks: list[Chunk] = Field(default_factory=list)
    skipped: list[SkippedFile] = Field(default_factory=list)
    stats: dict[str, float | int] = Field(default_factory=dict)
    embedding: EmbeddingInfo | None = None

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def assert_version(self, repo_id: str, commit: str) -> None:
        """Hard guard against mixing chunks from two versions of a repository."""
        if self.repo_id != repo_id:
            raise IndexVersionMismatch(
                f"index built for repo_id={self.repo_id!r}, requested {repo_id!r}"
            )
        if self.commit != commit:
            raise IndexVersionMismatch(
                f"index built for commit={self.commit!r}, requested {commit!r}"
            )

    def summary(self) -> dict[str, object]:
        kinds: dict[str, int] = {}
        for chunk in self.chunks:
            kinds[chunk.kind] = kinds.get(chunk.kind, 0) + 1
        return {
            "repo_id": self.repo_id,
            "commit": self.commit,
            "commit_source": self.commit_source,
            "repo_url": self.repo_url,
            "chunks": self.chunk_count,
            "files": len({chunk.path for chunk in self.chunks}),
            "kinds": dict(sorted(kinds.items())),
            "skipped": len(self.skipped),
            "created_at": self.created_at,
            "embedding": self.embedding.model_dump() if self.embedding else None,
        }

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        header = self.model_dump(exclude={"chunks"})
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({"type": "header", **header}, ensure_ascii=False) + "\n")
            for chunk in self.chunks:
                payload = {"type": "chunk", "data": chunk.model_dump()}
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "ChunkIndex":
        source = Path(path)
        header: dict[str, object] | None = None
        chunks: list[Chunk] = []
        with source.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("type") == "header":
                    header = {key: value for key, value in record.items() if key != "type"}
                    continue
                if record.get("type") != "chunk":
                    continue
                chunks.append(Chunk.model_validate(record["data"]))
        if header is None:
            raise ValueError(f"index file has no header: {source}")
        if header.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported index schema_version {header.get('schema_version')!r}; "
                f"expected {SCHEMA_VERSION}"
            )
        return cls(chunks=chunks, **header)  # type: ignore[arg-type]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
