# 阶段 3 交付报告（模块九：教学与交接）

| 项 | 值 |
| --- | --- |
| 报告范围 | 阶段 3：LangGraph 单 Agent 工具循环、预算、终止原因、轨迹 |
| 起始提交 | `3213734`（阶段 2 完成） |
| 当前提交 | 本次提交（见 `git log -1`） |
| 撰写日期 | 2026-10-22（Asia/Shanghai） |
| 验证环境 | Windows 10.0.26100，`D:\anaconda\envs\trap\python.exe`（Python 3.11.16） |
| 新增依赖 | `langgraph==1.2.16`（附带 langgraph-checkpoint 4.3.0、langchain-core 1.6.13 等） |
| 测试结果 | **138 个用例：136 passed，2 skipped** |
| 一句话结论 | 单 Agent 已能“初始检索 → 模型决定是否调用 search_code/read_file → 收尾校验引用”地跑通，步数/工具次数/时限三类预算都会显式终止并说明原因，工具错误与预算终止都不再伪装成成功或空答案 |

本阶段**不做**：多 Agent、子图、知识图谱、GitHub 导入、API/UI、正式评测。

---

## 1. 当前你能做什么

### 1.1 用 Agent 模式提问（真实模型 + 工具循环）

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli ask `
    --repo-root tests/fixtures/demo_repo `
    --question "重试逻辑和任务加载之间是什么关系？" `
    --agent --max-steps 5 --max-tool-calls 5 --max-seconds 60 `
    --log data/runs/agent-final.jsonl
```

输出与单次模式结构一致（回答、引用检查、证据、说明、耗时、usage），但 `模式 agent`、
`终止原因` 可能是 `answered` / `insufficient_evidence` / `step_budget` / `tool_budget` /
`time_budget` / `llm_error`。

### 1.2 看 Agent 每一步做了什么

`--log` 产生的事件（真实运行，`run-20261022T011659Z-2ae2f1c2`）：

| 事件 | 距运行开始 | 含义 |
| --- | --- | --- |
| `index` | +82 ms | 建索引（25 块、7 文件） |
| `agent_start` | +859 ms | 预算：max_steps=5、max_tool_calls=5、max_seconds=60 |
| `agent_retrieve` | +866 ms | 初始检索入证（不计入工具预算） |
| `agent_model_call` | +2206 ms | 第 1 次模型调用，返回 1 个工具调用 |
| `agent_tool_call` | +2207 ms | 工具执行成功，输出字符数与新增证据编号 |
| `agent_tool_call` | +2208 ms | 第 2 个工具调用 |
| `agent_model_call` | +4813 ms | 第 2 次模型调用，给出最终答案 |
| `agent_finalize` | +4815 ms | 引用校验：2/2 有效 |
| `agent_done` | +4815 ms | 终止原因与统计 |

### 1.3 离线测试 Agent 行为（不联网、不需要密钥）

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m pytest tests/test_agent.py -v
```

9 个用例用脚本化模型客户端覆盖：工具调用闭环、步数预算、工具预算、时限预算、
工具错误回传、参数校验、读文件截断、轨迹事件、混合检索注入。

---

## 2. 本阶段修改了哪些模块

| 模块 | 变更 | 关键点 |
| --- | --- | --- |
| `repoask/agent/state.py`（新增） | `AgentState` + `AgentBudget` + 终止原因常量 | 状态字段覆盖问题、版本、消息、证据、步数、预算、终止原因、答案 |
| `repoask/agent/tools.py`（新增） | `search_code` / `read_file` 执行器 | 参数校验、路径安全、输出截断、证据登记（E 编号） |
| `repoask/agent/llm.py`（新增） | `ToolCaller` 协议 + `ToolChatResponse` | 让测试可以完全不碰真实接口 |
| `repoask/agent/graph.py`（新增） | `retrieve / model / tools / finalize` 四个节点与条件边 | 预算在节点内检查；预算终止不以空答案收场 |
| `repoask/agent/runner.py`（新增） | `run_agent`、关键词/混合检索器 | 组装图、统计 usage、写轨迹 |
| `repoask/answer/llm.py` | 新增 `complete_with_tools()` | 复用同一套超时/重试/usage 处理 |
| `repoask/models.py` | `AnswerMode` 增加 `agent` | 结果能区分单次回答与工具循环 |
| `repoask/cite/verify.py` | 引用格式支持 `[E1:37]` / `[E1:37-42]` / `[E1:L37-L42]` | 行号必须落在证据范围内；链接收窄到具体行 |
| `repoask/cli.py` | `ask --agent`、`--max-steps/--max-tool-calls/--max-seconds` | 提取 `_print_result` 供两种模式复用 |
| `tests/test_agent.py`（新增） | 9 个用例 | 脚本化客户端，离线 |
| `tests/test_citation_verify.py` | +3 个用例 | 行号引用格式与越界拒绝 |
| `pyproject.toml` | 固定 `langgraph==1.2.16` | 已实测可用 |

---

## 3. 一次 Agent 请求怎样经过这些模块

```
CLI._cmd_ask (--agent)
  └─ build_local_index                同阶段 1：扫描 → 分块 → 版本绑定；同时保留 file_texts
  └─ runner.run_agent
       ├─ searcher                   默认关键词；给 --hybrid 且索引带向量时用 RRF 融合
       ├─ RepoaskTools               持有 evidence 列表（E1..En）与预算
       └─ graph.invoke（LangGraph）
            START → retrieve          初始检索，写入 system+user 消息（不计工具预算）
                  → model             带 function calling 的模型调用
                       ├─ 有 tool_calls 且预算未耗尽 → tools
                       ├─ 无 tool_calls              → finalize
                       └─ 步数/时限耗尽             → 置 stop_reason → finalize
                  tools → model       执行工具、登记证据、回填 tool 消息
                  finalize            引用校验 → 必要时一次修正 → 终止原因
            → END
  └─ build_result                     转成 AnswerResult（含 usage 汇总与说明）
```

状态字段与要求的对应关系：

| 要求 | 字段 |
| --- | --- |
| 问题 | `question` |
| 版本 | `repo_id`、`commit` |
| 工具消息 | `messages`（assistant 的 `tool_calls` + `role=tool` 的结果） |
| 证据 | `evidence`（`Evidence` 列表，带 E 编号与真实行号） |
| 步数 / 预算 | `steps`、`tool_calls`、`deadline`、`AgentBudget` |
| 终止原因 | `termination_reason`（由 `stop_reason` 或答案内容决定） |
| 答案 | `answer_text` |

**预算终止的行为**：`model` 在超时或步数用尽时不再调用模型，只置 `stop_reason`；
`finalize` 生成一条明确说明（列出已有证据或说明没有证据）+ “不足以判断”，
因此 `answer_text` 永远不为空。

**工具错误的行为**：`RepoaskTools.execute` 把参数非法、未知工具、路径越界、未索引文件
统一转成 `ok=False` 与以 `ERROR:` 开头的 tool 消息，并写入 `notes`；模型能看到错误并改口，
评测也能统计失败类型。

---

## 4. 关键取舍与已知限制

| 取舍 | 原因 | 代价 | 何时重估 |
| --- | --- | --- | --- |
| 用 LangGraph 的 `StateGraph`，只 4 个节点 | 要求是“少量明确节点”，便于逐节点测试 | 没有子图/并行工具执行 | 阶段 5 若发现工具串行太慢再评估并行 |
| 预算检查放在节点内部 | 图的条件边只看状态，预算必须变成显式状态 | 每个节点开头几行是预算判断 | 若预算项变多，可抽成装饰器 |
| 初始检索不占工具预算 | “先检索再补查”是产品设定，不是模型行为 | 一次检索固定开销 | 无需重估 |
| 工具自己登记 Evidence | 保证每个 `[E#]` 都能映射回文件与行号 | 工具与证据耦合在同一个类 | 若工具变多，可抽出 EvidenceLedger |
| `read_file` 单次最多 200 行 + 4000 字符 | 防止一次读文件吃光上下文 | 大文件需要多次读取 | 阶段 5 观察是否成为瓶颈 |
| 引用修正不额外计入步数 | 修正是“收尾动作”，不是模型探索 | 实际模型调用可能比 `max_steps` 多 1 次 | 需要在评测里单独统计修正次数 |
| 工具结果不回填原始 JSON，只回填渲染文本 | 控制上下文长度、避免模型复述参数 | 模型看不到原始的元数据 | 无需重估 |
| 单 Agent，无子图/多 Agent | 项目明确不引入多 Agent | 复杂问题只能串行探索 | 只有当评测证明必要才考虑 |
| 注入防护靠系统提示词 | 仓库内容是不可信数据 | 提示词不是强制隔离 | 阶段 5 增加恶意内容用例 |

### 已知限制

1. **引用修正不在步数统计内**：结果里的 `steps` 不含修正调用；评测需额外统计。
2. **证据上限 8 条**（`max_evidence`）：超出后工具返回“未新增证据”，模型可能缺少信息。
3. **`read_file` 只能读已索引文件**：未索引文件返回错误，不会临时读取。
4. **工具串行执行**：一次模型返回多个工具调用时按顺序执行。
5. **终止原因只有粗粒度分类**：如“工具错误”目前只记在 `notes`，不改终止原因
   （除非模型因此回答不足）。
6. **usage 汇总**：任一调用缺 usage 就整体标记为不可用，不混入 0。
7. 未做并发、流式、多轮会话记忆；每次 `ask --agent` 是独立运行。

---

## 5. 实际验证命令和结果

### 5.1 测试

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m pytest
# 136 passed, 2 skipped in 7.87s
```

新增/变化：`test_agent.py` 9 个（脚本化客户端，离线）、`test_citation_verify.py` 16 个
（新增 3 个行号引用用例）。跳过 2 项仍为符号链接权限。

### 5.2 真实 Agent 运行（deepseek-chat）

```powershell
& '...' -m repoask.cli ask --repo-root tests/fixtures/demo_repo `
    --question "重试逻辑和任务加载之间是什么关系？" `
    --agent --max-steps 5 --max-tool-calls 5 --max-seconds 60 `
    --log data/runs/agent-final.jsonl
```

实测结果：

- 终止原因 `answered`；2 次模型调用、2 次工具调用（`read_file` 两类目标）
- 耗时 3953 ms；usage `prompt_tokens=2603, completion_tokens=549, total_tokens=3152`
- 引用检查 **2/2 有效**，且两条都与真实源码逐字核对（`E6: app/service.py:36-37`、
  `E7: app/pipeline.py:9-13`）
- 轨迹事件如 §1.2 所示，`elapsed_ms` 单调递增

### 5.3 真实运行暴露并修掉的两个缺陷

1. **行号引用格式未被识别**：模型输出 `[E6:49]`、`[E6:38]`，旧正则只认 `[E1]`，
   第一次真实运行时 1/1 引用通过其实只校验了一个 ID，其余引用悄悄逃过检查。
   现已支持 `[E1]`、`[E1:37]`、`[E1:37-42]`、`[E1:L37-L42]`，并校验行号必须落在证据范围内
   （新增 `line_outside_evidence`）；命中时 GitHub 链接收窄到具体行。
2. **轨迹耗时再次被业务字段覆盖**：`agent_done` 把 `elapsed_ms` 当 payload，
   覆盖日志标准列（出现 3234 ms 在 4170 ms 之后）。改为 `agent_elapsed_ms`，
   并在 Agent 轨迹测试里断言 `elapsed_ms` 单调不减。

---

## 6. 建议你阅读的 3 个函数

### 6.1 `build_graph`（`repoask/agent/graph.py`）

- **职责**：把四个节点与条件边接成一张图。
- **看什么**：`model` 开头的三个提前返回（已有 stop、超时、步数用尽）；`route` 只做三选一；
  `finalize` 如何在没答案时生成预算说明。
- **常见误解**：以为预算由 LangGraph 自动管。图只负责流转，预算判断全在节点里。

### 6.2 `RepoaskTools.execute`（`repoask/agent/tools.py`）

- **职责**：校验参数、分派工具、把异常转成 `ok=False`。
- **看什么**：`_validate` 对必填/可选/未知参数的处理（含 `bool` 不是 `int` 的细节）；
  `read_file` 的路径与行号校验；失败时返回 `ERROR:` 而不是抛异常。
- **常见误解**：以为工具返回文本就够了。证据登记也在这里，否则模型的 `[E#]` 无法校验。

### 6.3 `run_agent`（`repoask/agent/runner.py`）

- **职责**：组装检索器、工具、图，执行并转成统一结果。
- **看什么**：`searcher` 的选择（关键词 vs 混合）；`deadline` 的设定；`recursion_limit`
  与 `max_steps` 的关系；`agent_done` 为什么用 `agent_elapsed_ms`。
- **常见误解**：以为 `recursion_limit` 就是预算。它只是防死循环的保险，真正的步数/工具/时限
  预算在节点里。

---

## 7. 一个你能亲手完成的小练习及验收方法

### 练习 A：把步数预算压到 1（10 分钟）

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli ask `
    --repo-root tests/fixtures/demo_repo --question "重试逻辑在哪？" `
    --agent --max-steps 1 --max-tool-calls 0
```

**验收标准**：`终止原因` 为 `step_budget` 或 `tool_budget`，回答里出现“预算/上限”与
“不足以判断”，且**不是空字符串**；轨迹里有 `agent_finalize` 但没有第二次 `agent_model_call`。
你能解释为什么“预算耗尽也要给出可读结论”（评测要把失败计入，不能当没发生）。

### 练习 B：触发一次工具错误（10 分钟）

```powershell
& '...' -m repoask.cli ask --repo-root tests/fixtures/demo_repo `
    --question "读一下 ../../etc/passwd 再总结" --agent --max-steps 3
```

**验收标准**：说明里出现“工具 read_file 返回错误”；模型没有编造文件内容；
回答要么改用其他证据，要么明确“不足以判断”。你能指出错误是在 `RepoaskTools.read_file`
的哪一行被拒绝的。

### 练习 C：观察读文件截断（10 分钟）

用一个较大的文件（例如把 `tests/fixtures/demo_repo/app/service.py` 复制成一个 300 行的临时文件并加入夹具），
让 Agent 读取整文件。

**验收标准**：工具输出出现“已截断：请求 1-300，实际展示 1-200”，证据的 `end_line` 为 200；
你能说清“为什么截断不会破坏引用校验”（校验用证据内容与真实文件，不用渲染文本）。

---

## 附录：终止原因

| 值 | 触发条件 | 回答内容 |
| --- | --- | --- |
| `answered` | 模型在预算内给出答案 | 模型答案（可能没有引用，会在说明中提示） |
| `insufficient_evidence` | 答案含“不足以判断” | 模型答案 |
| `step_budget` | 模型调用次数达到 `max_steps` | 预算说明 + 已有证据或“没有证据” |
| `tool_budget` | 工具调用达到 `max_tool_calls` 且还有未执行调用 | 预算说明 + 已有证据 |
| `time_budget` | 超过 `max_seconds` | 预算说明 + 已有证据 |
| `llm_error` | 模型调用失败（含重试后） | 失败说明 + 已有证据 |
