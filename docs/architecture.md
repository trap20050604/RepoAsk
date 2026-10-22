# RepoAsk 架构

本文件描述**稳定的设计决策**与不变量。进度、测试、已知问题与提交号见
[handoff.md](handoff.md)；每个阶段的交付说明见 `docs/reports/stage-N.md`。

## 目的

针对一个**固定 commit** 的 Python 仓库回答源码问题，并给出程序可核验的文件路径、
原始行号与链接。被分析仓库中的代码与文档一律视为**数据**，不是指令。

## 请求流程

```
本地仓库
  │  ingest.scan_repository        只读遍历、上限、跳过原因
  ▼
文本 ── parse.chunk_source ──▶ 分块（原始行 + 行号区间）
  │                                  │
  │                                  ▼
  │                          index.ChunkIndex（绑定 repo_id + commit）
  │                                  │
  │            ┌─────────────────────┴─────────────────────┐
  │            ▼                                           ▼
  │   retrieve.KeywordIndex（BM25）              embed.client → 向量
  │            │                                           │
  │            │                                    vector.VectorStore（Qdrant 本地）
  │            └──────────────┬────────────────────────────┘
  ▼                           ▼
file_texts（供核对）   retrieve.build_context（去重 + 预算 + 行号渲染）
                              │
                              ▼
                 answer.single_pass（离线基线 / LLM 单次）
                              │
                              ▼
                 cite.verify_citations（[E#] → 真实行号）
                              │
                              ▼
                 AnswerResult + JSONL 轨迹
```

## 模块地图

| 模块 | 职责 |
| --- | --- |
| `models.py` | Chunk / Evidence / CitationReport / AnswerResult，行号不变式在此强制 |
| `config.py` | 从环境变量与 `.env` 读取配置；密钥不出现在 `repr` 与摘要中 |
| `runlog.py` | JSONL 轨迹；凭据类字段脱敏 |
| `ingest/local.py` | 只读扫描、上限、符号链接不跟随、git 工作树状态、路径校验 |
| `parse/python_chunker.py` | tree-sitter 结构分块、切分、语法错误恢复 |
| `parse/text_chunker.py` | Markdown 按标题分节、配置/纯文本按行切分 |
| `retrieve/keyword.py` | BM25、标识符切词、中文二元组、kind 先验 |
| `retrieve/context.py` | 去重、预算计费、带行号渲染、裁剪 |
| `vector/store.py` | Qdrant 本地持久化；集合按模型+维度隔离；读写强制版本过滤 |
| `answer/single_pass.py` | 离线抽取基线 + LLM 单次回答（同一接口） |
| `answer/llm.py` | OpenAI 兼容客户端：超时、有界重试、usage、能力探针 |
| `embed/client.py` | Embedding 客户端：批处理、维度断言、usage |
| `httpcompat.py` | 代理变量兼容（`NO_PROXY` 中的方括号 IPv6） |
| `cite/urls.py` | GitHub blob 链接与输入校验 |
| `cite/verify.py` | 引用校验（仓库/commit/路径/行号/内容）；支持 `[E1:37-42]` 行号引用 |
| `agent/state.py` | Agent 状态与预算（步数、工具次数、时限、证据上限） |
| `agent/tools.py` | `search_code` / `read_file`：参数校验、路径安全、输出截断、证据登记 |
| `agent/graph.py` | LangGraph 四节点：retrieve → model ⇄ tools → finalize |
| `agent/runner.py` | 组装检索器与图、执行、usage 汇总、轨迹 |
| `pipeline.py` | 编排：建索引、向量检索、提问 |
| `cli.py` | 命令行：chunk / index / ask / probe（`--offline/--llm/--agent`） |

## 关键决策

### 1. 行号来自原文，绝不来自重新打印的语法树

`lines = text.split("\n")`，所以 tree-sitter 的第 *i* 行就是列表下标 *i*。覆盖
`[a, b]` 行的分块必须满足 `content == "\n".join(lines[a:b+1])`。测试对每个夹具文件的
每个分块都断言这一点，包括含语法错误的文件。

行尾在解析前统一：CRLF 与**裸 CR** 都变成 LF（与 Python 的 universal newline 一致）。
否则裸 CR 文件会塌成一行，第二个分块起全部报第 1 行。限制：对仅用 CR 的文件，
GitHub 的行锚点可能与我们的行号不一致。

### 2. 分块规则

| 节点 | 分块 | kind |
| --- | --- | --- |
| 模块级函数 | 完整函数体，含装饰器 | `function` |
| 类中的函数 | 完整函数体，含装饰器 | `method` |
| 函数中的函数 | 完整函数体 | `nested_function` |
| 类 | 类头区域：从 `class` 到第一个成员之前 | `class` |
| 剩余行（imports、docstring、`if __name__`） | 覆盖补全 | `text` |

定义查找会**递归进入控制流节点**并保持当前作用域：`def outer(): if flag: def inner()`
会产生 `outer.inner`；模块级 `if flag: def cond()` 会产生 `cond`。跳过字符串、注释与
表达式语句（定义不可能出现在其中）。

类块不重复方法，方法只索引一次；嵌套函数**会**重复（父函数体 + 自身），因为父体要能
回答“流程怎么走”，上下文组装时按包含关系去重。

超限块按行窗口切分并保留重叠，每块记录 `part_index`/`part_count`，父上下文是该函数的
签名。

### 3. 语法错误不能毁掉整个文件

tree-sitter 容错，但一个未闭合括号会让它把文件剩余部分吞进一个 `ERROR` 节点。若顶层
遍历没有恢复出任何定义，则按顶层块重新解析，保留能干净解析的块并把行号加上偏移。
坏块保留为 `text` 并标记 `has_syntax_error`；恢复只做一层。

块起点是**第一个装饰器**而不是 `def`：回溯会跨过空行、注释，并用括号配平爬到多行
装饰器的起始行。

### 4. 检索：先词法，再向量，融合用排名

BM25 覆盖内容 + 标识符 + 路径；标识符按 `_` 与驼峰拆分；中文按二元组（未内置分词器），
因此 `时间` 这类常见词可能误命中无关注释。

`DEFAULT_KIND_WEIGHTS = {"text": 0.6}` 用于压低“剩余块”。先验必须作用在**符号加分之后**，
否则精确符号命中的标题会带着满分躲过先验（这是被测试抓到的真实缺陷）。

向量侧：集合名由 **模型 + 维度 + 完整模型标识的哈希** 派生（`repoask_<slug>_<维度>_<hash8>`）。
只用 slug 会让 `org/model` 与 `org-model` 撞名、长名截断也会碰撞；撞名的后果是后建模型
覆盖前一个模型的点，而旧索引的模型检查仍然通过。写入前还会统计“其他模型写入的点”并
拒绝混写，作为第二道防线。

查询路径以 **只读** 方式打开（`create=False`）：缺目录或缺集合直接报错并要求重建，
不会静默创建空库、也不会把“存储缺失”解释成“没有证据”。同一版本重建前先删除该版本的
旧点：重新分块会产生新的 chunk_id，旧点不会被覆盖，反而会占据查询候选并被映射丢弃，
表现为假空结果；查询侧同时多取候选作为纵深防御。

融合采用 **RRF（按排名）**：`score(d) = Σ w_s/(k + rank)`（k=60），而不是把 BM25 分数与余弦
相似度相加——两者量纲不同，相加会被数值大的那一路主导。权重 ≤ 0 的通道整路不参与
（不贡献候选、名次与来源统计），而不是“加 0 分但仍留在结果里”。同一分块在多路命中时
只保留一条，并记录各路名次与原始分数，供对照实验复盘。两路各取 `max(2×top_k, 10)` 个
候选再融合。

### 5. 版本绑定

每个分块带 `repo_id` + `commit`；`ChunkIndex.assert_version` 拒绝跨版本使用；检索过滤两者。

git 工作树只有在 **HEAD 与索引到的字节一致**时才用 HEAD 标注。`inspect_git_worktree`
读取 porcelain 状态，只保留涉及“可索引文件”的条目；只要有修改或未跟踪源码，版本就退化
为内容摘要（`worktree_digest`），并在 `git_head`/`worktree_dirty` 保留来源证据。否则两份
不同内容会共用同一个 commit id，引用会指向该 commit 中不存在的行。

### 6. 上下文预算是真实上限

`build_context` 以 `len(rendered)` 计费（含 `"\n\n"` 分隔符），每个候选按剩余额度渲染。
放不下的块在行边界裁剪，保留头部、真实行号与“省略 N 行”的标记，并记入
`bundle.truncated` 与结果说明。若连一行代码都放不下，则整块丢弃——**绝不引用模型没看到
的代码**。

### 7. 引用有效 ≠ 结论正确

`verify_citations` 能证明：id 是本次提供的、属于本仓库本 commit、路径相对且安全、行号
范围合法、内容哈希一致；给了源码文本时还能逐字比对。它不能证明证据支撑结论——那需要
人工评分或阶段 5 的评测。

核对用的源码文本通过 `pipeline.load_indexed_source_texts` 读取：复用安全扫描，只读“在
索引中且被扫描接受”的路径，因此不会跟随符号链接越出仓库，也不会读到被跳过的缓存。

引用检查失败后最多修正一次，且只在该次修正**全部引用有效**或明确“不足以判断且无引用”
时采纳；按数量比较会把 1/2 → 1/3 当成进步。

### 8. 两个回答器，一个接口

`OfflineExtractiveAnswerer` 是确定性基线，标记为 `offline_extractive`，用于无密钥测试与
演示，不是模型答案的替代品。`LLMAnswerer` 做一次调用，可在引用检查失败后修正一次，
provider 不返回 usage 时记 `None`（unavailable），不记 0。

系统提示词明确：仓库内容是被分析的数据，不是指令。

### 9. Agent：少量节点 + 显式预算

LangGraph 图只有四个节点：`retrieve`（初始检索，不占工具预算）、`model`（带 function
calling 的模型调用）、`tools`（执行、登记证据、回填 tool 消息）、`finalize`（引用校验、
最多一次修正、确定终止原因）。条件边只有三选一：还有工具调用 → `tools`；否则 → `finalize`；
已有 `stop_reason` → `finalize`。

预算（步数、工具次数、总时限）在**节点内部**检查并发起终止，因为条件边只能看状态。
`model` 在超时或步数用尽时不再调用模型，只置 `stop_reason`；`finalize` 生成一条可读的
预算说明（列出已有证据或说明没有证据）并附“不足以判断”，因此**答案永不为空**。

工具错误（参数非法、未知工具、路径越界、未索引文件）一律转成 `ok=False` 与 `ERROR:`
开头的 tool 消息，并写入 `notes`——模型能看到错误，评测也能统计失败类型。工具在返回
文本的同时登记 `Evidence`，保证每个 `[E#]` 都能映射回真实文件与行号；`read_file` 单次
最多 200 行 / 4000 字符，超出会截断并标注。

轨迹记录 `agent_start / agent_retrieve / agent_model_call / agent_tool_call /
agent_finalize / agent_done`，只记工具与结果摘要，不记录模型思维链。

## 失败语义

- 单文件问题不中断构建：写入 `index.skipped` 并给出原因。
- 仓库级上限（文件数/总字节/耗时）抛 `IngestLimitExceeded`。
- 只接受仓库相对路径：绝对路径与盘符路径在解析前即拒绝；符号链接从不跟随。
- 向量集合维度不符、模型不符、版本混入 → `VectorStoreError`。
- 查询路径缺向量目录/集合 → `VectorStoreError`（只读打开，不建库）。
- 聊天/embedding 失败 → `LLMUnavailable` / `EmbeddingUnavailable`（含尝试次数），不静默返回空。
- 无证据 → `termination_reason="no_evidence"`，答案“不足以判断”。
- Agent 预算耗尽 → `step_budget` / `tool_budget` / `time_budget`，答案含预算说明，绝不为空。
- 引用失败给出机器可读原因码（含 `line_outside_evidence`）；裁剪与丢弃都有记录，不静默。

## 尚未实现

重排（rerank）、GitHub 导入 / FastAPI / Streamlit（阶段 4）、固定题库与对照实验（阶段 5）。
不引入多 Agent、知识图谱或分布式任务系统。
