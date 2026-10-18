# RepoAsk architecture

Stable description of how the system is put together. Progress and open issues live
in [handoff.md](handoff.md).

## Purpose

Answer questions about a Python repository at a **fixed commit**, citing file paths and
original line numbers that a program can verify. Code and docs from the analysed
repository are treated as untrusted data, never as instructions.

## Request flow (stage 1)

```
local repo
   │  ingest/local.scan_repository      read-only walk, limits, skip reasons
   ▼
decoded text  ── parse/chunk_source ──▶ chunks (original lines + line ranges)
   │                                            │
   │                                            ▼
   │                                    index.ChunkIndex  (repo_id + commit bound)
   ▼                                            │
file_texts (for citation checks)                ▼
                                    retrieve/keyword.KeywordIndex  (BM25 + prior)
                                                │
                                                ▼
                              retrieve/context.build_context  (dedupe + budget)
                                                │
                            ┌───────────────────┴───────────────────┐
                            ▼                                       ▼
              answer/single_pass.OfflineExtractiveAnswerer   LLMAnswerer (chat model)
                            └───────────────────┬───────────────────┘
                                                ▼
                              cite/verify.verify_citations  ([E#] → real lines)
                                                ▼
                                    AnswerResult + JSONL run trace
```

## Module map

| Module | Responsibility | Notes |
| --- | --- | --- |
| `repoask/models.py` | Chunk, Evidence, CitationReport, AnswerResult | The line-slice invariant is enforced here |
| `repoask/config.py` | Settings from env / `.env` | Secrets never in `repr` or `safe_summary` |
| `repoask/runlog.py` | JSONL tracing | Redacts credential-shaped keys |
| `repoask/ingest/local.py` | Read-only scanning | Limits, no symlinks, no execution |
| `repoask/parse/python_chunker.py` | tree-sitter chunking | Structure + fault tolerance |
| `repoask/parse/text_chunker.py` | Markdown/text chunking | Heading sections, line windows |
| `repoask/retrieve/keyword.py` | BM25 + identifier tokens | CJK bigrams, kind prior |
| `repoask/retrieve/context.py` | Dedupe, budget, rendering | Line numbers rendered as `  37| code` |
| `repoask/answer/single_pass.py` | Baseline and LLM answerers | Same interface for stage 3 |
| `repoask/answer/llm.py` | OpenAI-compatible client | Timeout, bounded retry, usage probe |
| `repoask/cite/urls.py` | GitHub blob URLs | Rejects non-GitHub and traversal paths |
| `repoask/cite/verify.py` | Citation validation | Repo/commit/path/range/content checks |
| `repoask/pipeline.py` | Orchestration | `build_local_index`, `ask`, `ask_local` |

## Decisions that matter

### 1. Line numbers come from the original text, never from a re-printed tree

`lines = text.split("\n")`, so tree-sitter row *i* is list index *i*. A chunk covering
rows `[a, b]` must satisfy `content == "\n".join(lines[a:b+1])`. Tests assert this for
every chunk of every fixture file, including a file with a syntax error.

Consequences: chunk text is always one contiguous slice; enclosing signatures are stored
in a separate `parent_context` field with their own line numbers, so they can be shown
without breaking contiguity.

### 2. Chunking rules

| Node | Chunk | Kind |
| --- | --- | --- |
| module-level function | full body, decorators included | `function` |
| function inside a class | full body, decorators included | `method` |
| function inside a function | full body | `nested_function` |
| class | header region: `class ...` up to the line before its first nested definition | `class` |
| anything left over (imports, docstrings, `if __name__`) | filled in by a coverage pass | `text` |

A class chunk never repeats its methods, so method code is indexed once. Nested functions
*are* duplicated (present in the parent body and as their own chunk) because parent bodies
answer "how does this flow work" questions; `build_context` drops nested overlap at
selection time.

Large blocks are split by line windows with overlap; each part records `part_index`/
`part_count` and carries the definition's signature as its parent context.

### 3. Syntax errors must not cost the whole file

tree-sitter is error tolerant, but one unclosed bracket can make it swallow the rest of
the file into a single `ERROR` node (verified with `app/broken.py`). When the top-level
walk recovers no definitions at all, the chunker re-parses the file as independent
top-level blocks (column-0 `def`/`class`, decorators pulled in) and keeps the blocks that
parse cleanly, shifting their line numbers to absolute values. The broken block stays as
`text` and is flagged `has_syntax_error`. Recovery is one level deep only.

### 4. Retrieval is lexical in stage 1

BM25 over content + identifiers, with symbol and path tokens repeated. Identifier tokens
are split on `_` and camelCase; Chinese runs become character bigrams (no segmenter is
bundled). Consequence: common bigrams such as "时间" can match unrelated comments; this is
a known gap that vector search is meant to close.

`DEFAULT_KIND_WEIGHTS = {"text": 0.6}` down-weights leftover chunks. The prior is applied
*after* the exact-symbol bonus — applied before, a README heading would keep its full
bonus and ignore the prior (this was a real bug caught by a test).

Fusion with vector results will use **RRF over ranks** rather than adding incomparable
scores (stage 2).

### 5. Citation validity ≠ answer correctness

`verify_citations` proves: the id was provided, it belongs to this repo and commit, the
path is relative and safe, the range is valid, the content matches its hash, and — when
source text is supplied — the cited lines equal the real file text. It cannot prove the
evidence supports the conclusion; that needs human review or the stage 5 harness. The two
are reported separately.

### 6. Two answerers, one interface

`OfflineExtractiveAnswerer` is deterministic and labelled `offline_extractive`; it exists
so the pipeline is testable and demoable without credentials, and it is not a substitute
for a model answer. `LLMAnswerer` performs one completion, may repair invalid citations
once, and records `usage` as `None` ("unavailable") when the provider omits it.

The system prompt states that repository content is data, not instructions.

### 7. Version binding

Every chunk carries `repo_id` + `commit`; `ChunkIndex.assert_version` refuses cross-version
use; retrieval filters on both. For a directory that is not a git work tree the version is
a content digest labelled `worktree_digest` — explicitly not a git commit.

## Failure semantics

- Per-file problems never abort a build: they are recorded in `index.skipped` with a reason.
- Repository-level limits (files, total bytes, elapsed time) raise `IngestLimitExceeded`.
- Chat failures raise `LLMUnavailable` with attempt count; no silent empty answers.
- No evidence produces `termination_reason="no_evidence"` and the answer "不足以判断".
- A citation that fails validation is reported with a machine-readable issue code.

## Not built yet

Vector index and hybrid fusion (stage 2); LangGraph tool loop with step/time budgets
(stage 3); GitHub import, FastAPI, Streamlit (stage 4); question set and controlled
experiments (stage 5). No multi-agent design, knowledge graph or task queue is planned.
