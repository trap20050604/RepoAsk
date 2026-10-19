"""Lexical (keyword + symbol) retrieval with a small, explicit BM25.

Why BM25 instead of raw substring matching: term frequency saturation and
document-length normalisation stop a huge ``text`` chunk from outranking a
short function that matches the query exactly.

Known limitation: Chinese text is split into character bigrams because no word
segmenter is bundled. That finds "任务" inside "任务重试" but is not word-level
Chinese retrieval; vector search (stage 2) is expected to cover the gap.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from pydantic import BaseModel, ConfigDict

from repoask.models import Chunk

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[\u4e00-\u9fff]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_CJK_START = "\u4e00"
_CJK_END = "\u9fff"

# Retrieval policy: "text" chunks are leftovers (imports, module docstrings,
# ``if __name__`` glue). They are legitimate evidence for "where is X configured"
# questions but are rarely the answer to "which function implements X".
#
# Measured on tests/fixtures/demo_repo (25 chunks, 5 hand-checked questions,
# scripts/inspect_retrieval.py): 1.0 -> 2/5 top-1, 0.7 -> 3/5, 0.6 -> 4/5,
# 0.5 -> 4/5. README-heading questions stayed correct at every weight because an
# exact heading match keeps its own bonus.
#
# Caveat: this is a 25-chunk fixture with Chinese questions against English
# symbol names, so the margin is often a single docstring bigram. Treat 0.6 as a
# starting prior, not a validated finding; stage 5 re-checks it on the fixed
# question set and stage 2 adds vector retrieval, which is expected to fix the
# semantic gap this prior only papers over.
DEFAULT_KIND_WEIGHTS: dict[str, float] = {"text": 0.6}


def _is_cjk(char: str) -> bool:
    return _CJK_START <= char <= _CJK_END


def tokenize(text: str) -> list[str]:
    """Lowercase tokens plus identifier sub-tokens and CJK bigrams."""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text or ""):
        raw = match.group(0)
        if _is_cjk(raw[0]):
            if len(raw) == 1:
                tokens.append(raw)
            else:
                tokens.extend(raw[index : index + 2] for index in range(len(raw) - 1))
            continue
        lowered = raw.lower()
        tokens.append(lowered)
        if "_" in lowered:
            tokens.extend(part for part in lowered.split("_") if part and part != lowered)
        for part in _CAMEL_RE.findall(raw):
            lowered_part = part.lower()
            if lowered_part and lowered_part != lowered:
                tokens.append(lowered_part)
    return tokens


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk: Chunk
    score: float
    matched_terms: list[str] = []
    # 命中来源：keyword（BM25）或 vector（向量检索）。写进 Evidence 便于对照实验。
    source: str = "keyword"


class KeywordIndex:
    """In-memory BM25 index over chunks, optionally pinned to one version."""

    def __init__(
        self,
        chunks: list[Chunk],
        *,
        repo_id: str | None = None,
        commit: str | None = None,
        k1: float = 1.5,
        b: float = 0.75,
        kind_weights: dict[str, float] | None = None,
    ) -> None:
        self.repo_id = repo_id
        self.commit = commit
        self.k1 = k1
        self.b = b
        self.kind_weights = kind_weights or {}
        if repo_id is not None:
            chunks = [chunk for chunk in chunks if chunk.repo_id == repo_id]
        if commit is not None:
            chunks = [chunk for chunk in chunks if chunk.commit == commit]
        self.chunks: list[Chunk] = chunks
        self._term_counts: list[Counter[str]] = []
        self._doc_freq: Counter[str] = Counter()
        self._lengths: list[int] = []
        for chunk in chunks:
            counts = Counter(self._doc_tokens(chunk))
            self._term_counts.append(counts)
            self._lengths.append(sum(counts.values()))
            for term in counts:
                self._doc_freq[term] += 1
        self._total_docs = len(chunks)
        self._avg_length = (sum(self._lengths) / self._total_docs) if self._total_docs else 0.0

    @staticmethod
    def _doc_tokens(chunk: Chunk) -> list[str]:
        # Symbol and path tokens are repeated to express "these fields matter more"
        # without inventing a second scoring formula.
        tokens = tokenize(chunk.symbol or "") * 3
        tokens += tokenize(chunk.path)
        tokens += tokenize(chunk.content)
        return tokens

    def _idf(self, term: str) -> float:
        df = self._doc_freq.get(term, 0)
        return math.log(1 + (self._total_docs - df + 0.5) / (df + 0.5))

    def search(self, query: str, *, top_k: int = 6) -> list[SearchHit]:
        query_tokens = list(dict.fromkeys(tokenize(query)))
        if not query_tokens or not self.chunks:
            return []
        normalized_query = query.strip().lower()
        hits: list[SearchHit] = []
        for index, counts in enumerate(self._term_counts):
            matched: list[str] = []
            score = 0.0
            length = max(1, self._lengths[index])
            for term in query_tokens:
                tf = counts.get(term, 0)
                if not tf:
                    continue
                matched.append(term)
                denominator = tf + self.k1 * (1 - self.b + self.b * length / max(1e-9, self._avg_length))
                score += self._idf(term) * (tf * (self.k1 + 1)) / denominator
            if score <= 0:
                continue
            symbol = (self.chunks[index].symbol or "").lower()
            if symbol and symbol == normalized_query:
                score += 5.0  # exact symbol query wins over prose matches
            elif symbol and symbol.endswith("." + normalized_query):
                score += 2.5
            # The prior must be applied to the final score, including the symbol
            # bonus. Applied earlier, a heading like "任务流程" would keep its full
            # bonus and silently ignore the "text chunks are less likely" prior.
            score *= self.kind_weights.get(self.chunks[index].kind, 1.0)
            hits.append(
                SearchHit(
                    chunk=self.chunks[index],
                    score=round(score, 6),
                    matched_terms=matched,
                )
            )
        hits.sort(key=lambda hit: (-hit.score, hit.chunk.path, hit.chunk.start_line))
        return hits[:top_k]
