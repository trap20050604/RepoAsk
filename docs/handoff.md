# Handoff — current state

Last updated: 2026-10-18 (Asia/Shanghai). Stage: **1 complete (small experiment + minimal
vertical slice)**. Next stage: 2 (vector index, hybrid retrieval, context management).

Current commit: `9ee1115` — "feat: stage 1 vertical slice (chunking, lexical retrieval,
verified citations)". Local repository only; nothing has been pushed.

## Verified in this session

| Check | Command | Result |
| --- | --- | --- |
| Test suite | `python -m pytest` | **75 passed, 1 skipped** in ~2.5 s |
| Chunker output | `python -m repoask.cli chunk --repo-root tests/fixtures/demo_repo --path app/service.py` | 8 chunks: 1 text, 1 class, 4 functions, 2 nested |
| Offline answer | `python -m repoask.cli ask --repo-root tests/fixtures/demo_repo --question "重试逻辑是在哪个函数里实现的？"` | 2/2 citations valid, both compared with source text |
| Index build | `python -m repoask.cli index --repo-root tests/fixtures/demo_repo --out data/index/demo.jsonl` | 25 chunks from 7 files (2 class, 5 function, 4 method, 3 nested_function, 11 text) |
| Query a saved index | `python -m repoask.cli ask --index data/index/demo.jsonl --repo-root tests/fixtures/demo_repo --question "配置解析器怎么判断内容为空？"` | 2/2 citations valid, ConfigParser found |
| No-credential path | `python -m repoask.cli ask --repo-root tests/fixtures/demo_repo --question x --llm` | exits 2 with an explicit "LLM 未配置" message, no crash |
| Environment probe | `python -m repoask.cli probe` | tree-sitter 0.25.2 / tree-sitter-python 0.25.0, pydantic 2.13.5, openai 3.26.1, LLM unconfigured |

The single skipped test is `test_symlinks_are_never_followed`: creating a symlink needs
privileges this shell does not have. It self-skips and is not a passing safety check.

## Environment

- Interpreter `D:\anaconda\envs\trap\python.exe`, Python 3.11.16 (Windows 10.0.26100).
  `python` on PATH is an unrelated 3.8 install; always use the full path.
- Installed for this project, nothing else changed:
  `tree-sitter==0.25.2`, `tree-sitter-python==0.25.0`.
  Note: `tree-sitter-python` has **no 0.25.1 release** (verified against the USTC mirror:
  available versions end at 0.25.0).

## Blockers and unverified items

1. **No chat/embedding credentials.** `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` /
   `EMBEDDING_*` are all unset. Therefore: the `LLMAnswerer` path, the live capability
   probe (basic call, tool call, streaming) and every model-quality statement are
   **unverified**. Only the stubbed client tests ran. Do not report model results yet.
2. **GitHub is not reachable from `git` on this machine.**
   - `git ls-remote https://github.com/trap20050604/RepoAsk` →
     `schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS (0x8009030E)`
   - same with `-c http.sslBackend=openssl` → `Recv failure: Connection was reset`
   - Python `urllib` to `https://api.github.com` returns `HTTP 403 rate limit exceeded`,
     and `https://pypi.org/simple/` returns 200, so general HTTPS egress works; the
     failure is specific to git-over-HTTPS to GitHub.
   - Consequence: the existence of the remote repository is **unconfirmed**, nothing was
     pushed, and stage 4's GitHub import must plan for this (HTTPS archive or API download
     with an explicit fallback) instead of assuming `git clone` works.
3. **This machine has broken directory permissions in two places.**
   - `E:\RepoAsk` denied `WRITE_OWNER`; repaired for the current user. Backup and rollback
     script: `E:\RepoAsk-acl-recovery\acl-backup-8c2feff960ec43ce9005a772a8d68d4a.*`,
     report `acl-report-a938299aaa9a445d9e6e65c992a984f3.jsonl`.
   - `%TEMP%\pytest-of-ncp` denies even reading its ACL, so it cannot be repaired without
     elevation. Workaround in `tests/conftest.py`: pytest's `basetemp` is redirected to
     `.pytest-tmp/` inside the project (git-ignored). Delete the stale directory from
     Explorer when convenient; `pytest` works either way.

## Known limitations (deliberate, documented)

- Retrieval is lexical only; CJK bigrams mean a common word like "时间" can match an
  unrelated comment. The `{"text": 0.6}` prior is tuned on a 25-chunk fixture with 5
  hand-checked questions — a starting prior, not a validated finding.
- Nested functions appear twice in the index (parent body + own chunk); context assembly
  drops the nested overlap.
- Syntax-error recovery is one level deep and heuristic (column-0 `def`/`class`).
- No call graph, no streaming, no tool loop, no API/UI yet.
- `Ask` has no timeout budget in stage 1: single-pass only. Budgets arrive in stage 3.

## Next steps (stage 2)

1. Verify the embedding endpoint for real, record model + dimensions in
   `ChunkIndex.embedding`, and refuse to reuse an index built with different embeddings.
2. Add Qdrant local persistence with payload filters on `repo_id` + `commit`.
3. Fuse keyword and vector results with **RRF over ranks**; keep the per-retriever scores
   visible in the trace for the later comparison.
4. Re-run the 5 hand questions and record whether the kind prior is still justified.

## Where to read code

- `repoask/parse/python_chunker.py` — `chunk_python_source`, `_Emitter._emit_definition`,
  `_recover_top_level_blocks`.
- `repoask/pipeline.py` — `build_local_index`, `ask`.
- `repoask/cite/verify.py` — `verify_citations`.

## Exercise for the project owner

Run `cli chunk` on `app/parser.py`, predict where the `ConfigParser` chunk must end
(before the first member), then confirm with the output. Then break
`tests/fixtures/demo_repo/app/broken.py` differently (e.g. delete a closing quote) and
re-run `pytest tests/test_python_chunker.py` — the line-slice and coverage tests should
still pass. Acceptance: you can explain why `content` always equals
`"\n".join(lines[start-1:end])`.
