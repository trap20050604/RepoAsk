# RepoAsk

Ask source-level questions about **one fixed commit** of a public repository and get
answers whose citations a program can verify: file path, original line numbers and a
GitHub blob link pinned to that commit.

> Status: **stage 1 of 6** — local repository → structure chunking → lexical search →
> single-pass answer → verified line citations. Vector search, the LangGraph tool loop,
> the GitHub import API and the evaluation harness are not built yet.
> See [docs/handoff.md](docs/handoff.md) for what is actually verified.

## What works today

- **Structure chunking** of Python via tree-sitter: functions, methods, classes,
  nested functions, decorators, Chinese comments, and files with syntax errors.
  Every chunk keeps the untouched original source plus `start_line`/`end_line`.
- **Safe local ingestion**: read-only, size/time limits, symlinks never followed,
  binaries and caches skipped with a recorded reason.
- **Lexical + symbol retrieval** (BM25) with a hard repository/commit filter.
- **Budgeted context** with de-duplication and real line numbers rendered beside code.
- **Single-pass answering** with two modes: a deterministic offline baseline and an
  OpenAI-compatible chat model.
- **Citation verification**: `[E#]` markers are resolved to real evidence, checked
  against repo/commit/path/line range, and compared with the source text.
- **JSONL run traces** with credential redaction.
- 75 tests, no network or credentials needed.

## Quickstart

```powershell
# Windows, conda env "trap"
& 'D:\anaconda\envs\trap\python.exe' -m pytest

# See exactly what the chunker produces (symbol, kind, original lines, line range)
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli chunk `
    --repo-root tests/fixtures/demo_repo --path app/service.py

# Ask a question offline (no model, no key)
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli ask `
    --repo-root tests/fixtures/demo_repo --question "重试逻辑是在哪个函数里实现的？"

# Build and save an index, then query it
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli index `
    --repo-root tests/fixtures/demo_repo --out data/index/demo.jsonl

# Environment and capability probe (prints no secrets)
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli probe
```

Use `--llm` to answer with the configured chat model (requires `LLM_BASE_URL`,
`LLM_API_KEY`, `LLM_MODEL`; copy `.env.example` to `.env`). Without them the offline
baseline runs and is clearly labelled `offline_extractive`.

## Layout

```
repoask/
  parse/       tree-sitter Python chunker, Markdown/text fallback
  ingest/      read-only local repository scanning with limits
  retrieve/    BM25 keyword/symbol search, context assembly
  answer/      offline extractive baseline, OpenAI-compatible client
  cite/        GitHub blob URLs, citation verification
  index.py     version-bound chunk index (JSONL)
  pipeline.py  build_local_index / ask / ask_local
  cli.py       chunk | index | ask | probe
tests/         behaviour tests + small fixture repository
scripts/       retrieval inspection helper
docs/          architecture.md, handoff.md
```

## Honest limitations

- Retrieval is lexical only. Chinese question wording is matched against English
  symbol names through docstrings and comments; a semantic gap remains until vector
  search lands (stage 2). See the measured note in `retrieve/keyword.py`.
- No call graph: the project never claims to resolve dynamic dispatch or `getattr`
  indirection.
- GitHub import, FastAPI and Streamlit are not implemented yet.
- Stage 1 has no streaming, no tool loop and no multi-step budget enforcement.
