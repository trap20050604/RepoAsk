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

Line endings are normalised before parsing: CRLF **and a bare CR** both become LF, matching
Python's universal-newline reading. Without the bare-CR rule an old-Mac file collapses into
one tree-sitter row and every chunk after the first reports line 1 (review finding 7).
Limitation: for a CR-only file, GitHub's blob line anchors may disagree with ours.

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

Definitions are found by descending through **control-flow nodes** as well, keeping the
current scope: `def outer(): if flag: def inner()` yields `outer.inner` (kind
`nested_function`, parent `outer`), and a module-level `if flag: def cond()` yields `cond`.
Without that descent such definitions only existed inside the parent body and could not be
found by symbol (review finding 3). Descent skips strings, comments and expression
statements, where a definition cannot appear.

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
top-level blocks and keeps the blocks that parse cleanly, shifting their line numbers to
absolute values. The broken block stays as `text` and is flagged `has_syntax_error`.
Recovery is one level deep only.

A block starts at the definition's **first decorator**, not at `def`: the scan walks back
over blank lines, comment lines and complete decorator statements, climbing to the top of
a logical statement by bracket balance so a multi-line decorator is recognised as one
unit. Missing this put `@app.route(...)` into the error text block (review finding 4).

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
are reported separately, and `source_checked_count` makes "compared with real source"
visible instead of implying it.

Source text for that comparison is loaded with `pipeline.load_indexed_source_texts`, which
reuses the safe scanner and only reads paths that are both in the index and indexable, so
verification cannot follow a symlink out of the repository or read a skipped cache file
(review finding 2).

A failed citation check triggers at most one repair, adopted only when it is strictly
usable — every citation valid, or an explicit "insufficient evidence" answer with no
citations. Comparing valid *counts* would accept 1/2 → 1/3 as an improvement (review
finding 6).

### 6. Two answerers, one interface

`OfflineExtractiveAnswerer` is deterministic and labelled `offline_extractive`; it exists
so the pipeline is testable and demoable without credentials, and it is not a substitute
for a model answer. `LLMAnswerer` performs one completion, may repair invalid citations
once, and records `usage` as `None` ("unavailable") when the provider omits it.

The system prompt states that repository content is data, not instructions.

### 7. Version binding

Every chunk carries `repo_id` + `commit`; `ChunkIndex.assert_version` refuses cross-version
use; retrieval filters on both.

A git work tree is labelled with its HEAD **only when HEAD describes the bytes that were
indexed**. `inspect_git_worktree` asks git for the porcelain status and keeps only entries
that touch indexable files; any modification or untracked source file means the working
tree no longer matches the commit. In that case the version is a content digest labelled
`worktree_digest`, while `git_head` and `worktree_dirty` preserve the provenance. Otherwise
two different contents could share one commit id and citations could point at lines that do
not exist in that commit (review finding 1). A plain non-git directory behaves the same way.

### 8. The context budget is a real bound

`build_context` charges each block against `len(rendered)`, including the `"\n\n"`
separator, and renders each candidate to the remaining allowance. A block that does not fit
is truncated at a line boundary, keeping its header, its real line numbers and a
`... (N lines omitted by budget)` marker, and the truncation is recorded in
`bundle.truncated` and surfaced in `AnswerResult.notes`. If a block cannot show at least one
code line it is dropped instead of cited, so a citation never points at code the model did
not see. Before this rule the first block was accepted however large it was and separators
were undercounted (review finding 5).

## Failure semantics

- Per-file problems never abort a build: they are recorded in `index.skipped` with a reason.
- Repository-level limits (files, total bytes, elapsed time) raise `IngestLimitExceeded`.
- Ingestion accepts repository-relative paths only: `read_bytes_checked` rejects absolute
  forms even when they resolve inside the root, and symlinks are never followed.
- Chat failures raise `LLMUnavailable` with attempt count; no silent empty answers.
- No evidence produces `termination_reason="no_evidence"` and the answer "不足以判断".
- A citation that fails validation is reported with a machine-readable issue code.
- Context truncation and dropped candidates are always recorded, never silent.

## Not built yet

Vector index and hybrid fusion (stage 2); LangGraph tool loop with step/time budgets
(stage 3); GitHub import, FastAPI, Streamlit (stage 4); question set and controlled
experiments (stage 5). No multi-agent design, knowledge graph or task queue is planned.
