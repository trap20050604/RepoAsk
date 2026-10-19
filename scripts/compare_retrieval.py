"""对照三种检索方式：关键词、向量、RRF 混合。

用途：记录**事实**（名次与候选来源），不产出质量结论。真实 embedding 调用。
输出：控制台表格 +（可选）JSONL 原始记录。

    & 'D:\\anaconda\\envs\\trap\\python.exe' scripts/compare_retrieval.py `
        --index data/index/vector.jsonl `
        --questions eval/stage2_questions.jsonl `
        --out eval/runs/stage2-observation.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repoask.config import Settings  # noqa: E402
from repoask.console import force_utf8_streams  # noqa: E402
from repoask.embed.client import EmbeddingClient  # noqa: E402
from repoask.index import ChunkIndex  # noqa: E402
from repoask.pipeline import search_vector  # noqa: E402
from repoask.retrieve.fusion import fuse_rrf  # noqa: E402
from repoask.retrieve.keyword import DEFAULT_KIND_WEIGHTS, KeywordIndex  # noqa: E402


def load_questions(path: Path) -> list[dict[str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def summarise(hits) -> list[dict[str, object]]:
    return [
        {
            "path": hit.chunk.path,
            "start_line": hit.chunk.start_line,
            "end_line": hit.chunk.end_line,
            "symbol": hit.chunk.symbol,
            "kind": hit.chunk.kind,
            "score": hit.score,
            "ranks": hit.ranks,
            "source_scores": hit.source_scores,
        }
        for hit in hits
    ]


def top1(hits) -> str:
    if not hits:
        return "-"
    hit = hits[0]
    symbol = f" {hit.chunk.symbol}" if hit.chunk.symbol else ""
    return f"{hit.chunk.path}:{hit.chunk.start_line}{symbol}"


def main() -> int:
    force_utf8_streams()
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", required=True)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--out", default=None, help="JSONL 原始记录输出路径")
    args = parser.parse_args()

    index = ChunkIndex.load(args.index)
    if index.vectors is None:
        print("索引没有向量，请先 index --embed", file=sys.stderr)
        return 2
    settings = Settings.load()
    if not settings.embedding_configured:
        print("需要 EMBEDDING_* 配置", file=sys.stderr)
        return 2
    embedder = EmbeddingClient(settings)
    keyword_index = KeywordIndex(
        index.chunks,
        repo_id=index.repo_id,
        commit=index.commit,
        kind_weights=DEFAULT_KIND_WEIGHTS,
    )
    candidate_k = max(args.top_k * 2, 10)
    questions = load_questions(Path(args.questions))

    records: list[dict[str, object]] = []
    print(f"索引 {index.repo_id}@{index.commit}  分块 {index.chunk_count}  "
          f"embedding {index.embedding.model} / {index.embedding.dimensions} 维\n")
    print(f"{'题目':<4} {'关键词 top-1':<42} {'向量 top-1':<42} {'混合 top-1':<42}")
    print("-" * 132)
    for item in questions:
        question = item["question"]
        started = time.perf_counter()
        keyword_hits = keyword_index.search(question, top_k=candidate_k)
        vector_hits = search_vector(index, question, embedder, top_k=candidate_k)
        hybrid_hits = fuse_rrf(keyword_hits, vector_hits, top_k=args.top_k)
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        print(
            f"{item.get('id', '?'):<4} {top1(keyword_hits[: args.top_k]):<42} "
            f"{top1(vector_hits[: args.top_k]):<42} {top1(hybrid_hits):<42}"
        )
        for mode, hits in (
            ("keyword", keyword_hits[: args.top_k]),
            ("vector", vector_hits[: args.top_k]),
            ("hybrid", hybrid_hits),
        ):
            records.append(
                {
                    "question_id": item.get("id"),
                    "question": question,
                    "question_type": item.get("type"),
                    "mode": mode,
                    "repo_id": index.repo_id,
                    "commit": index.commit,
                    "embedding_model": index.embedding.model,
                    "embedding_dimensions": index.embedding.dimensions,
                    "top_k": args.top_k,
                    "candidate_k": candidate_k,
                    "elapsed_ms": elapsed_ms,
                    "candidates": summarise(hits),
                }
            )

    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"\n原始记录已写入 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
