"""Dev helper: show what keyword retrieval ranks for a question.

    python scripts/inspect_retrieval.py --repo-root tests/fixtures/demo_repo \
        --question "重试逻辑是在哪个函数里实现的？" --b 0.75

Used to compare BM25 settings before changing defaults; results are printed, not
stored, so this is a debugging tool rather than an experiment record.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repoask.pipeline import build_local_index  # noqa: E402
from repoask.retrieve.keyword import KeywordIndex, tokenize  # noqa: E402
from repoask.console import force_utf8_streams  # noqa: E402


def main() -> int:
    force_utf8_streams()
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--b", type=float, default=0.75)
    parser.add_argument("--k1", type=float, default=1.5)
    parser.add_argument(
        "--text-weight",
        type=float,
        default=1.0,
        help="multiplicative prior for kind=text chunks (1.0 = neutral)",
    )
    args = parser.parse_args()

    build = build_local_index(args.repo_root)
    search = KeywordIndex(
        build.index.chunks,
        repo_id=build.index.repo_id,
        commit=build.index.commit,
        k1=args.k1,
        b=args.b,
        kind_weights={"text": args.text_weight},
    )
    print(f"query tokens: {tokenize(args.question)}")
    print(f"chunks: {len(search.chunks)}  b={args.b} k1={args.k1} text_weight={args.text_weight}\n")
    for rank, hit in enumerate(search.search(args.question, top_k=args.top_k), start=1):
        chunk = hit.chunk
        print(
            f"{rank:>2}. {hit.score:8.4f}  {chunk.kind:<16} "
            f"{chunk.path}:{chunk.start_line}-{chunk.end_line} "
            f"{chunk.symbol or '-'}  terms={hit.matched_terms}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
