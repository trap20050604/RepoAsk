# 阶段 1 交付报告（模块九：教学与交接）

| 项 | 值 |
| --- | --- |
| 报告范围 | 阶段 1（本地仓库最小闭环）+ 独立审查第 1 轮的 8 项修复 |
| 审查基线 | `35caa13` |
| 功能修复提交 | `1015470` |
| 当前提交 | `0394bb9`（撰写时；本次修复与本文档另起提交，见 `git log -1`） |
| 撰写日期 | 2026-10-19（Asia/Shanghai） |
| 验证环境 | Windows 10.0.26100，`D:\anaconda\envs\trap\python.exe`（Python 3.11.16） |
| 测试结果 | **94 个用例：92 passed，2 skipped**（跳过项均为“创建符号链接需要权限”） |
| 一句话结论 | 阶段 1 的“解析 → 检索 → 单次回答 → 行号引用 → 引用校验”闭环已真实跑通并可离线复现；向量检索、Agent 工具循环、GitHub 导入、API/UI 尚未实现 |

**文档分工（避免重复）**

| 文档 | 回答什么问题 | 更新频率 |
| --- | --- | --- |
| 本文 `docs/reports/stage-N.md` | 这个阶段交付了什么、怎么验证、怎么学 | 每阶段一份 |
| `docs/architecture.md` | 系统稳定的设计决策与不变量 | 架构变化时 |
| `docs/handoff.md` | 当前状态、测试、已知问题、提交号、下一步 | 每次提交 |

`docs/learning-guide.zh-CN.md` 是你自己的学习手册（未跟踪），本报告不重复它的内容，只做交付与验收视角的补充。

---

## 1. 当前你能做什么

全部命令在仓库根目录 `E:\RepoAsk` 下执行；`& '...'` 是 PowerShell 的调用写法。

### 1.1 查看结构分块（不需要任何密钥）

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli chunk `
    --repo-root tests/fixtures/demo_repo --path app/service.py
```

你能看到：文件编码、总行数、分块数，以及每个分块的 **kind**（`function`/`method`/`class`/`nested_function`/`text`）、**符号全名**、**原始行号区间**、**父级签名行号**，以及**逐行原样代码**（左侧是真实行号）。这是检查“我到底索引了什么”的最快方式。

### 1.2 离线问答（不需要密钥、不联网）

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli ask `
    --repo-root tests/fixtures/demo_repo --question "重试逻辑是在哪个函数里实现的？"
```

你能看到：回答（标注 `offline_extractive`，明确不是模型生成）、逐条引用检查结果（OK/BAD + 原因码）、证据列表与分数、说明项（未使用证据、被丢弃候选、裁剪）、耗时与 usage（离线为 `unavailable`）。

### 1.3 建立并复用索引

```powershell
# 建索引：commit_source 会显示版本是怎么确定的
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli index `
    --repo-root tests/fixtures/demo_repo --out data/index/demo.jsonl

# 复用索引提问；带 --repo-root 时会把引用与真实源码逐字核对
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli ask `
    --index data/index/demo.jsonl --repo-root tests/fixtures/demo_repo `
    --question "配置解析器怎么判断内容为空？"
```

### 1.4 用真实模型回答（需要 `.env` 配置）

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli ask `
    --repo-root tests/fixtures/demo_repo --question "load_tasks 做了什么？" --llm
```

已实测该路径可用（`deepseek-chat`：基础调用、usage、工具调用、流式均通过），但**回答质量尚未进入评测**，不能对外声称效果。

### 1.5 环境能力自检

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli probe
```

会打印依赖版本、脱敏配置、**真实**的 embedding 维度与聊天模型四项能力状态；任一失败都会以非零退出码结束。

### 1.6 运行测试

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m pytest
```

94 个用例约 4 秒跑完，全程不需要网络与密钥。

---

## 2. 本阶段修改了哪些模块

`repoask/` 共 27 个 Python 模块（含 `__init__`），本阶段新增/修改如下。

| 模块 | 职责 | 关键函数 / 类 | 对应测试 |
| --- | --- | --- | --- |
| `parse/python_chunker.py` | tree-sitter 结构分块、大块切分、语法错误恢复、编码与换行处理 | `chunk_python_source`、`_Emitter._emit_definition`、`_decorator_group_start`、`_recover_top_level_blocks`、`decode_python_source` | `test_python_chunker.py`(14)、`test_review_fixes.py`(18) |
| `parse/text_chunker.py` | README 按标题分节、配置/纯文本按行切分 | `chunk_markdown_source`、`chunk_plain_text_source` | `test_text_chunker.py`(5) |
| `parse/common.py` | 行窗口、裁剪、是否需要切分 | `split_windows`、`trim_bounds` | 同上 |
| `ingest/local.py` | 只读安全扫描、上限、git 工作树状态、路径校验 | `scan_repository`、`inspect_git_worktree`、`read_bytes_checked`、`is_indexable_path` | `test_ingest_local.py`(10) |
| `models.py` | 全链路数据模型与不变量 | `Chunk`、`Evidence`、`CitationReport`、`AnswerResult` | 间接覆盖（各测试构造/断言） |
| `index.py` | 版本绑定的索引与 JSONL 持久化 | `ChunkIndex`、`assert_version`、`save/load` | `test_stage1_end_to_end.py`(8) |
| `retrieve/keyword.py` | BM25 词法检索、标识符切词、中文二元组、kind 先验 | `KeywordIndex`、`tokenize` | `test_retrieval.py`(10) |
| `retrieve/context.py` | 去重、预算计费、带行号渲染、裁剪 | `build_context`、`render_evidence_block_fitted` | `test_retrieval.py`、`test_review_fixes.py` |
| `answer/single_pass.py` | 离线抽取基线 + LLM 单次回答（同一接口） | `OfflineExtractiveAnswerer`、`LLMAnswerer` | `test_llm_client.py`(9)、e2e |
| `answer/llm.py` | OpenAI 兼容客户端：超时、有界重试、usage、能力探针 | `ChatClient.complete`、`ChatClient.probe` | `test_llm_client.py` |
| `embed/client.py` | Embedding 客户端（阶段 2 前置，已真实调用验证） | `EmbeddingClient.embed`、`probe` | 未纳入常规测试（需网络密钥）；由 `cli probe` 人工验证 |
| `cite/urls.py` | GitHub blob 链接与输入校验 | `github_blob_url`、`validate_relative_path` | `test_citation_verify.py`(13) |
| `cite/verify.py` | 引用校验（ID/仓库/commit/路径/行号/内容） | `verify_citations` | `test_citation_verify.py` |
| `pipeline.py` | 编排：建索引、提问、安全加载核对源码 | `build_local_index`、`load_indexed_source_texts`、`ask` | `test_stage1_end_to_end.py` |
| `runlog.py` | JSONL 轨迹与脱敏 | `RunLogger` | `test_config_and_logging.py`(7) |
| `config.py` | 环境变量 / `.env` 配置，密钥不外泄 | `Settings`、`safe_summary` | `test_config_and_logging.py` |
| `cli.py` | 命令行：`chunk` / `index` / `ask` / `probe` | `main`、`_cmd_ask` | 人工验证（见 §5） |
| `repoask/console.py` | Windows 控制台 UTF-8 输出 | `force_utf8_streams` | 间接覆盖 |
| `testing.py` | 测试用构造工厂 | `make_chunk`、`make_evidence` | — |

**明确没有改动的部分**：`docs/learning-guide.zh-CN.md`（你的文件）、`.env`、`.idea/`、`data/` 下的运行产物。

审查第 1 轮修复的就位文件（供 B 只复查差异）：`ingest/local.py`、`parse/python_chunker.py`、`retrieve/context.py`、`pipeline.py`、`index.py`、`cli.py`，以及新增的 `tests/test_review_fixes.py`。

本次撰写报告时另外修掉一个**新发现的小缺陷**：`done` 轨迹事件原本用 `elapsed_ms` 作为业务字段，覆盖了日志自身的标准耗时列，导致轨迹出现“1 ms 在 79 ms 之后”的非单调记录。现改为 `pipeline_elapsed_ms`，并加断言防止回归（`test_run_log_records_every_stage_without_credentials`）。

---

## 3. 一次请求怎样经过这些模块

### 3.1 路径 A：`ask --repo-root`（一次建索引 + 回答）

```
CLI._cmd_ask
  └─ pipeline.ask_local
       ├─ pipeline.build_local_index
       │    ├─ ingest.scan_repository        读目录 → [ScannedFile(rel_path, size, language)]
       │    ├─ ingest.read_bytes_checked     逐文件读字节（相对路径校验、根目录边界）
       │    ├─ parse.decode_source           字节 → 文本（BOM/CRLF/裸CR 归一化，记录编码）
       │    ├─ ingest.inspect_git_worktree   HEAD 是否与工作树一致 → commit / worktree_digest
       │    ├─ parse.chunk_source            文本 → [Chunk]（Python 结构块 / Markdown 节 / 文本窗口）
       │    └─ ChunkIndex(...)               绑定 repo_id + commit，记录 stats/skipped
       ├─ retrieve.KeywordIndex             建 BM25 倒排（只保留同 repo+commit）
       ├─ KeywordIndex.search               → [SearchHit(chunk, score, matched_terms)]
       ├─ retrieve.build_context            去重 → 预算 → 渲染（带真实行号）→ [Evidence E1..En]
       ├─ answerer.answer                   离线抽取式 或 LLM 单次
       ├─ cite.verify_citations             逐条核对 [E#] → CitationReport
       ├─ （可选）一次引用修正              仅当结果严格可用才采纳
       └─ RunLogger.record                  search/context/answer/citation_check/done
```

数据形态在这一路上是：**字节 → 文本 → Chunk → Evidence → 回答文本 → 校验报告 → 结果对象 + JSONL**。

### 3.2 路径 B：`index` 之后 `ask --index`

1. `index` 把 `ChunkIndex` 写成 JSONL：首行 `{"type":"header",...}`，其后每行一个 `{"type":"chunk","data":{...}}`。
2. `ask --index` 用 `ChunkIndex.load` 读回，**不重新分块**；`assert_version` 保证仓库与 commit 未被偷换。
3. 若同时给了 `--repo-root`，`pipeline.load_indexed_source_texts` 会**重新安全扫描**该目录，只读“既在索引中、又被扫描接受”的路径，用于把引用与真实源码逐字比对（`source_checked_count` 会显示比对了几条）。
4. 不给 `--repo-root` 时仍会校验引用的内部一致性（hash、范围、仓库、commit），但**无法证明行号对应真实文件**，这一点在结果里以 `source_checked_count=0` 如实体现。

### 3.3 一次真实运行的轨迹（`--log`）

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli ask `
    --repo-root tests/fixtures/demo_repo --question "load_tasks 做了什么？" `
    --log data/runs/report-trace.jsonl
```

实际产生的事件（`run-20261019T103427Z-6968b814`）：

| 事件 | 距运行开始 | 记录了什么 |
| --- | --- | --- |
| `index` | +81 ms | chunks=25、commit=worktree-…、commit_source、git_head、worktree_dirty、kinds、skipped |
| `search` | +82 ms | question、top_k、每个命中的 path/行号/符号/分数/命中词 |
| `context` | +82 ms | 入选证据 id/路径/行号/符号、used_chars、被丢弃候选及原因 |
| `answer` | +83 ms | mode、model、termination_reason、insufficient_evidence、答案长度 |
| `citation_check` | +83 ms | 引用 id、valid/checked/source_checked、未知 id、问题列表 |
| `done` | +83 ms | `pipeline_elapsed_ms`、usage（缺失记 `unavailable`） |

轨迹的价值：**失败与丢弃同样被记录**，不会出现“只留成功样本”的假象。

### 3.4 失败会怎样

| 情况 | 行为 |
| --- | --- |
| 单个文件语法错误 | 不中断；出错区域成为 `text` 块并标 `has_syntax_error`，可解析的其余块照常索引（含块级恢复） |
| 单个文件无法解析 | 记入 `index.skipped`，原因 `parse_error: ...` |
| 文件过大/二进制/缓存/符号链接 | 跳过并记录原因（`file_too_large`、`binary_content`、`ignored_directory`、`symlink_not_followed`） |
| 超过文件数/总字节/时间上限 | 抛 `IngestLimitExceeded`，明确失败而不是给出残缺索引 |
| 路径越界 / 绝对路径 | 抛 `UnsafePathError`（即使绝对路径落在根目录内也拒绝） |
| 问题与仓库无关 | `termination_reason="no_evidence"`，答案“不足以判断”，`checked_count=0` |
| 引用无效 | 记录机器可读原因码（`unknown_evidence`/`wrong_commit`/`content_mismatch`/`unsafe_path`…） |
| 预算不足 | 裁剪并记录；不足一行代码的证据直接丢弃，绝不引用模型没看到的代码 |
| 模型未配置/调用失败 | `LLMUnavailable`（含尝试次数），CLI 退出码 2，不静默降级 |

---

## 4. 关键取舍与已知限制

| 取舍 | 为什么这样选 | 代价 | 何时重估 |
| --- | --- | --- | --- |
| 分块以“定义”为单位，父函数保留完整函数体，嵌套函数再单独成块 | 父块能回答“这个流程怎么走”，子块能按符号精确命中 | 同一段代码可能被索引两次 | 已用 `nested_overlap` 去重控制；若上下文仍紧张可改为“只留最深块” |
| 类块只到第一个成员之前，方法单独成块 | 避免类块把全部方法复制一遍 | 类体中原生的语句会落到 `text` 块 | 若类级配置/常量检索变差，可给类体单独一个 `class_body` 块 |
| 语法错误用“顶层块重新解析”兜底，只做一层 | 一个未闭合括号会让整棵树变成 ERROR；完全放弃会丢失健康函数 | 启发式对极端排版可能失手 | 出现真实反例时再补（目前 5 种装饰器/空行/注释形态均通过） |
| 检索阶段只用 BM25（阶段 1） | 先建立可离线复现的闭环，避免一上来就依赖模型 | 中文问句与英文符号名有语义鸿沟（“重试”→`with_retry` 只能靠注释命中） | **阶段 2 用向量 + RRF 融合解决** |
| `{"text": 0.6}` 的 kind 先验 | 剩余块（imports/docstring/`if __name__`）很少是“哪个函数”的答案 | 是在 25 块夹具、5 道手检题上的经验值，样本太小 | 阶段 5 用固定题库重测；阶段 2 加入向量后复评 |
| 空行/注释/多行装饰器都算进定义块起始 | 装饰器是语义的一部分（路由、重试策略），丢了就答错 | 块起始行比 `def` 行更早，和“函数定义行”直觉不同 | 无需重估，属正确行为 |
| 行尾统一归一化（CRLF、裸 CR → LF） | 与 Python 的 universal newline 一致；否则裸 CR 文件会被当成一行 | 极端情况下 GitHub 行锚点与本项目行号可能不一致 | 若接入 GitHub 行锚点校验，再按原文件字节复核 |
| 版本用“内容摘要”兜底 | 脏工作树下若仍标 HEAD，会让两份不同内容共享同一 commit，引用指向不存在的行 | commit 形态不是 git sha，肉眼不可对照 | 阶段 4 的 GitHub 导入永远是干净克隆，会走真实 commit |
| 引用校验只做“可追溯性”，不做“证据是否支撑结论” | 程序无法证明语义支持，只有人能判定 | 需要人工评分（阶段 5） | 阶段 5 建人工评分表 |
| 引用修正只采纳“全部有效”或“明确不足以判断” | 按数量比较会把 1/2 → 1/3 当成进步 | 修正可能被放弃，答案可追溯性较低 | 若后续有更细的评分函数再放宽 |
| 上下文超预算时裁剪而不是截断整段丢弃 | 保留真实行号与头部，模型至少能看到一部分 | 模型看到的行少于引用范围（已在说明中标注） | 阶段 2 引入更细的分块/预算策略后复评 |
| 单进程、无任务队列 | 阶段 1 聚焦闭环，避免过早引入分布式复杂度 | 长任务会阻塞；重启后状态不保留 | 阶段 4 用 FastAPI + 简单任务管理，并说明限制 |
| 离线抽取式基线保留在仓库里 | 无密钥也能测试与演示；且能证明链路而非模型 | 容易被误读为“产品答案” | 始终以 `mode=offline_extractive` 标注，评测时单列 |

### 已知限制（必须如实告知）

1. **检索是词法的**：中文按二元组切分，`时间` 之类常用词可能误命中无关注释；语义匹配要等阶段 2。
2. **无调用图**：不做跨文件调用关系推断，动态调用、`getattr`、装饰器注入一律不解析。
3. **模型回答质量未评测**：只验证了接口能力（能调用、能工具调用、能流式、有 usage），没有准确率数据。
4. **`file_texts=None` 时的信任边界**：只能证明证据内部自洽（hash/范围/版本），不能证明行号对应真实文件；`source_checked_count=0` 会显示这一点，但不会硬性失败。
5. **裁剪后的引用**：引用范围是完整块，模型只看到前 N 行。
6. **GitHub 导入、FastAPI、Streamlit、题库与对照实验**：均未实现。
7. **并发**：`Language` 对象共享、`Parser` 每次新建；单进程使用安全，多线程服务化时需复核。

---

## 5. 实际验证命令和结果

### 5.1 测试

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m pytest
# 92 passed, 2 skipped in 3.96s
```

| 测试文件 | 用例数 | 覆盖内容 |
| --- | --- | --- |
| `test_python_chunker.py` | 14 | 行号不变式、覆盖完整、装饰器、嵌套、类边界、语法错误恢复、切分、编码 |
| `test_review_fixes.py` | 18 | 审查 8 项缺陷的回归 |
| `test_ingest_local.py` | 10 | 跳过规则、上限、符号链接、路径越界 |
| `test_citation_verify.py` | 13 | 引用校验各失败分支、URL 构造、输入校验 |
| `test_retrieval.py` | 10 | 分词、符号命中、版本隔离、去重、预算、渲染 |
| `test_llm_client.py` | 9 | 重试、超时、usage 缺失、引用修正采纳规则（桩，无网络） |
| `test_stage1_end_to_end.py` | 8 | 端到端、版本绑定、轨迹事件、缺配置分支 |
| `test_config_and_logging.py` | 7 | `.env` 解析、密钥脱敏、日志截断 |
| `test_text_chunker.py` | 5 | Markdown 标题/围栏、配置文本、类型分发 |

跳过 2 项均为 `test_..._symlinks_...`：Windows 上创建符号链接需要额外权限，测试自跳过并**不算通过的安全检查**。

### 5.2 分块与索引

```
文件 app/parser.py（编码 utf-8，44 行） -> 7 个分块
#f31dce2dbc  class   ConfigParser     行 8-11
#e968202e78  method  ConfigParser.is_empty  行 13-16   父级: 行 8-8
```

索引统计（`cli index` 实跑）：25 个分块 / 7 个文件；`class=2, function=5, method=4, nested_function=3, text=11`；`commit_source=worktree_digest`（夹具目录不是 git 仓库）。

### 5.3 问答与引用

```powershell
& '...' -m repoask.cli ask --repo-root tests/fixtures/demo_repo --question "重试逻辑是在哪个函数里实现的？"
```

实测输出要点：`termination_reason=answered`；`引用 2/2 有效 (100%)，其中 2 条已与源码逐字核对`；证据 E1 `app/service.py:1-6`、E2 `app/service.py:17-33 (with_retry)`；说明中含“未使用证据”“丢弃 1 条候选（nested_overlap）”。

无关问题（`火星车的太阳能板功率是多少瓦？`）：`termination_reason=no_evidence`、答案“不足以判断”、`checked_count=0`、无任何引用。

### 5.4 预算

```powershell
& '...' -m repoask.cli ask --repo-root tests/fixtures/demo_repo `
    --question "load_tasks 做了什么？" --max-chars 400
```

实测：渲染上下文 ≤ 400 字符；说明中出现“证据 E2 因预算只展示前 1 行，另有 1 行未进入上下文”；引用仍 2/2 有效（**裁剪不破坏引用校验**）。

### 5.5 脏工作树

```powershell
# 在临时 git 仓库中：提交 → 修改一个 .py → 再建索引
```

实测：干净时 `commit=<HEAD>`、`commit_source=git`；修改后 `commit_source=worktree_digest`、`commit` 变成 `worktree-…`、`git_head=<HEAD>`、`worktree_dirty=True`。

### 5.6 模型与存储能力（真实调用）

| 能力 | 结果 |
| --- | --- |
| `deepseek-chat` 基础调用 / usage / 工具调用 / 流式 | 全部 ok（工具调用返回 1 个 tool call；流式 16 chunks） |
| `qwen3.7-text-embedding-flash` | 返回 **1024 维**（与配置一致），单条 1906 ms，23 prompt tokens |
| `qdrant-client 1.19.1` 本地持久化 | create / upsert / payload filter / 重开均通过；本地模式无 `search()`，用 `query_points`；`create_payload_index` 是 no-op |

---

## 6. 建议你阅读的 3 个函数

### 6.1 `chunk_python_source` —— 分块入口

`repoask/parse/python_chunker.py`

- **职责**：把一个文件的文本变成 `[Chunk]`，保证“每个块的内容等于原文某段连续行”。
- **阅读顺序**：先看它如何 `text.split("\n")` 建行表 → 解析 → `emitter.collect` → 必要时 `_recover_top_level_blocks` → `fill_gaps` 补齐剩余行 → 排序返回。
- **要盯住的一点**：`_Emitter.emit` 里 `content = "\n".join(self.lines[start_row:end_row+1])`，以及 `_add` 里 `start_line = start_row + 1`。**行号的唯一来源**就在这两行。
- **常见误解**：以为“块 = 格式化后的代码片段”。实际块永远是原文切片，不做任何重新排版；父级签名单独放在 `parent_context`，不会拼进正文。

### 6.2 `build_local_index` —— 版本绑定的来源

`repoask/pipeline.py`

- **职责**：扫描 → 读字节 → 解码 → 决定版本 → 分块 → 组装 `ChunkIndex`。
- **阅读顺序**：`scan_repository` → 逐文件 `read_bytes_checked`/`decode_source` → `inspect_git_worktree` 的分支判断 → `chunk_source` → 构造索引与 `stats`。
- **要盯住的一点**：`if git_state is not None and not git_state.dirty: ... else: compute_worktree_commit(...)`。这是“什么时候能声称自己对应某个 commit”的全部逻辑。
- **常见误解**：以为文件在 git 仓库里就一定标 HEAD。工作树脏（含未跟踪的可索引文件）时会退化成内容摘要，并在 `git_head`/`worktree_dirty` 里保留证据。

### 6.3 `verify_citations` —— 程序能证明什么

`repoask/cite/verify.py`

- **职责**：把回答里的 `[E#]` 映射回证据，逐项核对仓库、commit、路径、行号范围、内容哈希，并在给了 `file_texts` 时与真实源码逐字比较。
- **阅读顺序**：`extract_citation_ids` → 逐个 id 取证据 → 五类检查 → 构造 `CitationCheck` → 汇总 `CitationReport`。
- **要盯住的一点**：`source_checked` 只有在真正与文件比对成功时才为 `True`；`all_valid` 不代表“结论正确”，只代表“引用可追溯”。
- **常见误解**：把“引用 100% 有效”当成“答案正确”。两者是分开评估的（阶段 5 才有人工要点评分）。

---

## 7. 一个你能亲手完成的小练习及验收方法

### 练习 A（10 分钟）：预测类块边界

1. 先不要运行，按下面的代码预测 `ConfigParser` 这个类块会覆盖哪些行：

```python
class ConfigParser:            # 8
    """把文本配置解析成字典。"""   # 9
                               # 10
    supported_formats = (...)  # 11
                               # 12
    @property                  # 13
    def is_empty(self):        # 14
```

2. 运行：

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli chunk `
    --repo-root tests/fixtures/demo_repo --path app/parser.py
```

**验收标准**：你能指出类块结束于第 11 行（第一个成员之前），`@property` 属于 `ConfigParser.is_empty` 方法的块，且该方法的“父级”是第 8 行；并能解释为什么这样设计（避免方法代码被索引两次）。

### 练习 B（15 分钟）：看着版本标识变化

1. 复制夹具到临时目录并 `git init`、提交：

```powershell
$d = 'E:\RepoAsk\.tmp-practice'; Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item tests/fixtures/demo_repo $d -Recurse
git -C $d init -q -b main
git -C $d -c user.name=p -c user.email=p@e.invalid add -A
git -C $d -c user.name=p -c user.email=p@e.invalid commit -q -m init
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli index --repo-root $d --out data/index/practice.jsonl
```

2. 修改 `$d\app\service.py`（随便加一行 `# test`），再建一次索引，比较输出。

**验收标准**：第一次 `commit_source=git` 且 `commit` 是 40 位 sha、`worktree_dirty=False`；第二次 `commit_source=worktree_digest`、`commit` 以 `worktree-` 开头、`git_head` 仍是那个 sha、`worktree_dirty=True`。完成后删除临时目录与 `data/index/practice.jsonl`。

### 练习 C（15 分钟）：把预算压到很小

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli ask `
    --repo-root tests/fixtures/demo_repo --question "load_tasks 做了什么？" --max-chars 400
```

**验收标准**：输出里出现“因预算只展示前 N 行/未进入上下文”的说明；引用检查仍然全部有效；把 `--max-chars` 调到 `200` 时，至少能看到“丢弃（budget）”而**不是**超长上下文。你能据此解释“为什么裁剪不会破坏引用校验”（引用校验用的是证据的完整内容与真实文件，不是渲染后的文本）。

---

## 附录 A：数据模型要点

| 模型 | 关键字段 | 不变量 |
| --- | --- | --- |
| `Chunk` | repo_id、commit、path、symbol、start_line、end_line、content_hash、kind、content | `content == "\n".join(lines[start-1:end])`；`content_hash == sha256(content)` |
| `ParentContext` | symbol、start_line、end_line、content | 自己的行号独立，不并入子块正文 |
| `Evidence` | evidence_id（`E1`…）、content、content_hash、url、score | id 在一次运行内唯一 |
| `CitationCheck` | evidence_id、valid、source_checked、issues[] | `source_checked` 仅在与真实文件比对成功时为真 |
| `CitationReport` | checks、cited_ids、unknown_ids、uncited_evidence_ids | `valid_rate` 在无引用时为 `None`，不是 0 |
| `AnswerResult` | answer_text、mode、evidence、citation_report、termination_reason、elapsed_ms、usage、notes | 离线模式 `usage=None` |
| `ChunkIndex` | repo_id、commit、commit_source、git_head、worktree_dirty、chunks、embedding | `assert_version` 拒绝跨版本使用 |

## 附录 B：终止原因与状态

| 值 | 含义 |
| --- | --- |
| `answered` | 正常给出回答（可能有无效引用，会在报告里体现） |
| `no_evidence` | 检索为空，未调用模型，答案“不足以判断” |
| `insufficient_evidence` | 模型明确回答“不足以判断” |
| `mode=offline_extractive` | 离线抽取基线，未调用任何模型 |
| `mode=llm` | 单次模型回答（阶段 3 会替换为工具循环） |

## 附录 C：本阶段明确未做

向量索引与混合检索（阶段 2）、LangGraph 工具循环与预算（阶段 3）、GitHub 导入 / FastAPI / Streamlit（阶段 4）、固定题库与对照实验（阶段 5）、演示与简历事实清单（阶段 6）。不引入多 Agent、知识图谱、分布式任务系统。

## 附录 D：可作为简历事实的条目（仅已验证）

- 用 tree-sitter 对 Python 源码做结构分块，并以测试固定“块内容 == 原文连续行切片”的不变量（94 个用例）。
- 实现离线可复现的 RAG 闭环：BM25 检索 → 预算化上下文 → 单次回答 → 程序化行号引用校验。
- 设计并实现脏工作树下的版本标识降级策略（内容摘要 + `git_head`/`worktree_dirty` 证据）。
- 用真实调用验证聊天模型（含函数调用/流式）与 embedding（1024 维）能力，并记录到实验文档。
- 处理并回归 8 项独立审查缺陷（含 2 项 P1），每项先写失败测试再修复。

> 待阶段 6 复核后写入简历；本阶段不声称任何准确率、性能提升或用户规模。
