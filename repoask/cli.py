"""Command line entry point for inspecting the pipeline by hand.

    python -m repoask.cli chunk --repo-root tests/fixtures/demo_repo --path app/service.py
    python -m repoask.cli index --repo-root tests/fixtures/demo_repo --out data/index/demo.jsonl
    python -m repoask.cli ask   --repo-root tests/fixtures/demo_repo --question "重试逻辑在哪里？"
    python -m repoask.cli probe

``chunk`` is the quickest way to see exactly what the indexer produces:
symbol name, kind, original line range and the untouched source lines.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from repoask import __version__
from repoask.answer.single_pass import LLMAnswerer, OfflineExtractiveAnswerer
from repoask.cite.verify import describe_report
from repoask.config import Settings
from repoask.errors import RepoAskError
from repoask.index import ChunkIndex
from repoask.parse import chunk_source, decode_source, language_for_path
from repoask.pipeline import ask, build_local_index
from repoask.retrieve.context import ContextBudget
from repoask.runlog import RunLogger


def _cmd_chunk(args: argparse.Namespace) -> int:
    root = Path(args.repo_root).resolve()
    target = (root / args.path).resolve()
    if not target.is_relative_to(root):
        print(f"拒绝访问仓库外路径: {args.path}", file=sys.stderr)
        return 2
    if not target.is_file():
        print(f"文件不存在: {target}", file=sys.stderr)
        return 2
    language = language_for_path(args.path)
    if language is None:
        print(f"不支持的文件类型: {args.path}", file=sys.stderr)
        return 2
    text, encoding = decode_source(target.read_bytes())
    chunks = chunk_source(
        text,
        repo_id=args.repo_id,
        commit=args.commit,
        path=args.path.replace("\\", "/"),
        encoding=encoding,
        max_chunk_lines=args.max_chunk_lines,
        max_chunk_chars=args.max_chunk_chars,
    )
    print(f"文件 {args.path}（编码 {encoding}，{len(text.split(chr(10)))} 行）"
          f" -> {len(chunks)} 个分块\n")
    for chunk in chunks:
        symbol = chunk.symbol or "-"
        part = f" [{chunk.part_index}/{chunk.part_count}]" if chunk.part_count else ""
        print(
            f"#{chunk.chunk_id.split(':')[-1]}  {chunk.kind:<16} {symbol:<32} "
            f"行 {chunk.start_line}-{chunk.end_line}{part}"
            f"{'  (语法错误)' if chunk.has_syntax_error else ''}"
        )
        if chunk.parent_context is not None:
            print(f"    父级: 行 {chunk.parent_context.start_line}-"
                  f"{chunk.parent_context.end_line}")
        for offset, line in enumerate(chunk.content.split("\n")):
            print(f"    {chunk.start_line + offset:>4}| {line}")
        print()
    return 0


def _cmd_index(args: argparse.Namespace) -> int:
    build = build_local_index(
        args.repo_root,
        repo_id=args.repo_id,
        commit=args.commit,
        repo_url=args.repo_url,
    )
    target = build.index.save(args.out)
    summary = build.index.summary()
    print(f"索引已写入 {target}")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    if build.index.skipped:
        print("  跳过样例（前 10 条）:")
        for item in build.index.skipped[:10]:
            print(f"    {item.path}: {item.reason}")
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    settings = Settings.load()
    if args.llm:
        if not settings.llm_configured:
            print(
                "LLM 未配置：请在 .env 或环境变量中设置 LLM_BASE_URL / "
                "LLM_API_KEY / LLM_MODEL。",
                file=sys.stderr,
            )
            return 2
        from repoask.answer.llm import ChatClient

        answerer = LLMAnswerer(ChatClient(settings))
    elif args.offline:
        answerer = OfflineExtractiveAnswerer()
    else:
        answerer = OfflineExtractiveAnswerer()

    budget = ContextBudget(
        max_chars=args.max_chars,
        max_items=args.top_k,
    )
    logger = RunLogger(args.log) if args.log else None
    try:
        if args.index:
            index = ChunkIndex.load(args.index)
            file_texts = _load_file_texts(args.repo_root) if args.repo_root else None
            result = ask(
                index,
                args.question,
                answerer=answerer,
                top_k=args.top_k,
                budget=budget,
                file_texts=file_texts,
                logger=logger,
            )
            if logger:
                logger.record("index", **index.summary())
        else:
            if not args.repo_root:
                print("需要 --repo-root 或 --index 之一", file=sys.stderr)
                return 2
            from repoask.pipeline import ask_local

            result = ask_local(
                args.repo_root,
                args.question,
                answerer=answerer,
                repo_id=args.repo_id,
                commit=args.commit,
                repo_url=args.repo_url,
                top_k=args.top_k,
                budget=budget,
                logger=logger,
            )
    except RepoAskError as exc:
        print(f"运行失败: {exc}", file=sys.stderr)
        return 1
    finally:
        if logger:
            logger.close()

    print(f"问题: {result.question}")
    print(f"回答（模式 {result.mode}, 终止原因 {result.termination_reason}）:\n")
    print(result.answer_text)
    print()
    print(f"引用检查: {describe_report(result.citation_report)}")
    for check in result.citation_report.checks:
        status = "OK " if check.valid else "BAD"
        target = f"{check.path}:{check.start_line}-{check.end_line}"
        print(f"  [{status}] {check.evidence_id}: {target}")
        if check.url:
            print(f"        {check.url}")
        for issue in check.issues:
            print(f"        ! {issue.code}: {issue.message}")
    if result.evidence:
        print("\n证据:")
        for item in result.evidence:
            print(f"  [{item.evidence_id}] {item.label}  score={item.score}")
    if result.notes:
        print("\n说明:")
        for note in result.notes:
            print(f"  - {note}")
    usage = result.usage
    print(
        f"\n耗时: {result.elapsed_ms} ms; "
        f"usage: {'unavailable' if usage is None else usage.model_dump()}"
    )
    return 0


def _load_file_texts(root: str) -> dict[str, str]:
    base = Path(root).resolve()
    texts: dict[str, str] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        if any(part.startswith(".") for part in rel.split("/")):
            continue
        if language_for_path(rel) is None:
            continue
        try:
            texts[rel] = decode_source(path.read_bytes())[0]
        except OSError:
            continue
    return texts


def _cmd_probe(_: argparse.Namespace) -> int:
    from importlib.metadata import version

    settings = Settings.load()
    print(f"repoask {__version__}; python {sys.version.split()[0]}")
    for package in ("tree-sitter", "tree-sitter-python", "pydantic", "openai"):
        try:
            print(f"  {package} {version(package)}")
        except Exception:  # noqa: BLE001 - a missing package is reported, not raised
            print(f"  {package} <未安装>")
    print("\n配置（已脱敏）:")
    for key, value in settings.safe_summary().items():
        print(f"  {key}: {value}")
    if not settings.llm_configured:
        print("\nLLM 未配置：跳过在线探针。离线基线仍可使用。")
        return 0
    from repoask.answer.llm import ChatClient

    report = ChatClient(settings).probe()
    print("\n在线能力探针:")
    for name in ("basic_call", "usage_reported", "tool_call", "streaming"):
        step = getattr(report, name)
        print(f"  {name}: {step.status} {step.detail or ''}")
    for note in report.notes:
        print(f"  note: {note}")
    return 0 if report.basic_call.status == "ok" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repoask", description="RepoAsk 命令行工具")
    parser.add_argument("--version", action="version", version=f"repoask {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    chunk = sub.add_parser("chunk", help="查看单个文件的结构分块结果")
    chunk.add_argument("--repo-root", required=True)
    chunk.add_argument("--path", required=True, help="仓库内相对路径")
    chunk.add_argument("--repo-id", default="demo")
    chunk.add_argument("--commit", default="0" * 40)
    chunk.add_argument("--max-chunk-lines", type=int, default=120)
    chunk.add_argument("--max-chunk-chars", type=int, default=6000)
    chunk.set_defaults(func=_cmd_chunk)

    index = sub.add_parser("index", help="构建并保存索引")
    index.add_argument("--repo-root", required=True)
    index.add_argument("--out", default="data/index/local.jsonl")
    index.add_argument("--repo-id", default=None)
    index.add_argument("--commit", default=None)
    index.add_argument("--repo-url", default=None)
    index.set_defaults(func=_cmd_index)

    ask_cmd = sub.add_parser("ask", help="针对索引或本地仓库提问")
    ask_cmd.add_argument("--repo-root", default=None)
    ask_cmd.add_argument("--index", default=None)
    ask_cmd.add_argument("--question", required=True)
    ask_cmd.add_argument("--top-k", type=int, default=6)
    ask_cmd.add_argument("--max-chars", type=int, default=8000)
    ask_cmd.add_argument("--repo-id", default=None)
    ask_cmd.add_argument("--commit", default=None)
    ask_cmd.add_argument("--repo-url", default=None)
    ask_cmd.add_argument("--log", default=None, help="JSONL 轨迹输出路径")
    mode = ask_cmd.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="使用离线抽取式基线（默认）")
    mode.add_argument("--llm", action="store_true", help="使用已配置的聊天模型")
    ask_cmd.set_defaults(func=_cmd_ask)

    probe = sub.add_parser("probe", help="检查环境、配置与在线能力")
    probe.set_defaults(func=_cmd_probe)
    return parser


def _force_utf8_streams() -> None:
    """Kept as a thin alias so existing call sites stay readable."""
    from repoask.console import force_utf8_streams

    force_utf8_streams()


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BrokenPipeError:  # e.g. output piped into `head`
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
