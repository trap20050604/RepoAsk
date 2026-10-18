# Handoff — current state

Last updated: 2026-10-18 (Asia/Shanghai). Stage: **1 complete (small experiment + minimal
vertical slice)**. Next stage: 2 (vector index, hybrid retrieval, context management).

Current commit: `62687b8` — **pushed to `origin/main` on 2026-10-18** (the remote repository
was empty, so the first push created `main`). The stage-1 feature commit was `9ee1115`;
later commits added the embedding client, the corrected git diagnosis, the IDE-file cleanup
and this verification record.

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
  `tree-sitter==0.25.2`, `tree-sitter-python==0.25.0`, `qdrant-client==1.19.1`
  (plus its own dependencies: h2, hpack, hyperframe, portalocker, pywin32).
  Note: `tree-sitter-python` has **no 0.25.1 release** (verified against the USTC mirror:
  available versions end at 0.25.0).

## Verified model and storage capabilities (2026-10-18, real calls)

| Capability | Endpoint / model | Measured result |
| --- | --- | --- |
| Chat basic call | `deepseek-chat` @ `api.deepseek.com/v1` | ok, reply `pong` |
| Chat usage reporting | same | ok, token usage returned |
| Chat **tool calling** | same | ok, 1 tool call returned |
| Chat streaming | same | ok, 16 chunks / 14 chars |
| Embedding | `qwen3.7-text-embedding-flash` @ DashScope compatible-mode | ok, **1024 dimensions** (matches config), 1906 ms, 23 prompt tokens |
| Qdrant local mode | `qdrant-client==1.19.1`, `path=` persistence | create / upsert / payload filter / reopen all pass |

Two API facts to remember from the Qdrant probe: local mode has **no `search` method**
(use `query_points`), and `create_payload_index` is a documented no-op locally (it emits a
warning), so filtering correctness must be tested by result inspection, not by index
existence.

## Blockers and unverified items

1. **Credentials are now configured and verified (2026-10-18).** Chat and embedding were
   both probed with real calls (table above). Still unverified by design: model *answer
   quality* (no evaluation yet), the LangGraph tool loop integration (stage 3), and
   embedding cost/latency at repository scale.
2. **git 可用——此前的"git 不可用"结论是错的，已更正（2026-10-18 16:40）。**
   - 早先 `git ls-remote https://github.com/trap20050604/RepoAsk` 失败于
     `schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS (0x8009030E)`，
     换成 `-c http.sslBackend=openssl` 则变成 `Recv failure: Connection was reset`。
   - 真实原因是**当时进程运行在沙箱受限令牌下**：Windows schannel 在该令牌中无法获取 TLS
     凭据，openssl 后端的连接也被拦断。这不是机器或 git 安装的故障。
   - 沙箱放宽为完全访问后复测：
     `git clone --depth 1 --single-branch https://github.com/pallets/flask` **成功，2 秒**，
     `HEAD=d086db856be187255b8ec61ef409357393020f32`，与 `git ls-remote` 一致；
     `git ls-remote` 返回 `exit 0`。结论：**阶段 4 可以正常使用 `git clone`**。
     教训：环境受限时得到的"某工具坏了"结论，必须在权限变化后复测再写进文档。
   - 仍然成立的两条事实：
     - 远端仓库 `https://github.com/trap20050604/RepoAsk` **是空仓库**（页面含
       "This repository is empty"，`ls-remote` 无 refs）。首次推送会创建 `main`。
     - 无 token 的 GitHub API 会被限流（403，共享出口 IP `3.112.217.125`）。clone 不需要
       token；若阶段 4 要用 API 取元数据，需要只读 token 作为可选增强。
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

1. ~~Verify the embedding endpoint for real~~ **done**: 1024 dims confirmed; the client
   already fails loudly on a dimension mismatch. Remaining: persist model + dimensions in
   `ChunkIndex.embedding` and refuse to reuse an index built with different embeddings.
2. Write `repoask/vector/store.py` around `QdrantClient(path=...)`: collection per embed
   model, deterministic point ids, payload filters on `repo_id` + `commit`, and a
   `delete_version(repo_id, commit)` for re-imports. Use `query_points` (local mode has no
   `search`).
3. Fuse keyword and vector results with **RRF over ranks**; keep each retriever's own score
   in the trace so the stage-5 comparison can show both sides.
4. Re-run the 5 hand questions and record whether the `{"text": 0.6}` prior is still
   justified once vectors are in play.

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
