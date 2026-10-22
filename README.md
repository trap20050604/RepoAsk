# RepoAsk

针对**一个固定 commit** 的公开仓库回答源码级问题，并给出**程序可核验**的引用：
文件路径、原始行号、绑定该 commit 的 GitHub 链接。

> 当前进度：阶段 1、2 完成（含两轮独立审查修复）；**阶段 3 完成**（LangGraph 工具循环与预算）。
> GitHub 导入、FastAPI、Streamlit、正式评测题库尚未实现。
> 真实验证记录见 [docs/handoff.md](docs/handoff.md)，每阶段交付说明见
> [docs/reports/](docs/reports/)，稳定设计见 [docs/architecture.md](docs/architecture.md)。

## 现在能做什么

- **结构分块**：tree-sitter 解析 Python，按函数/方法/类/嵌套函数切块，含装饰器、
  中文注释、语法错误文件（含块级恢复）；每块保留原始代码与真实行号。
- **只读导入**：大小/数量/时间上限，符号链接不跟随，二进制与缓存跳过并记录原因；
  脏工作树不会冒用 git HEAD 作为版本。
- **关键词/符号检索**（BM25）与**向量检索**（Qdrant 本地持久化，集合按模型+维度隔离），
  两者都强制按 `repo_id + commit` 过滤，并可用 RRF 按排名融合。
- **预算化上下文**：去重、父级签名、带真实行号渲染，超限按行裁剪并记录。
- **三种回答模式**：离线抽取式基线（无密钥可测）、单次模型回答、**LangGraph 工具循环**
  （`search_code` / `read_file`，带步数/工具次数/时限预算与显式终止原因）。
- **引用校验**：`[E#]` 或 `[E1:37-42]` 映射回证据，检查仓库/commit/路径/行号/内容，
  并能与真实源码逐字比对。
- **JSONL 轨迹**：检索、上下文、回答、工具调用、引用检查、完成事件，凭据字段脱敏。

## 快速开始

```powershell
# 测试（离线，不需要密钥）
& 'D:\anaconda\envs\trap\python.exe' -m pytest

# 看一个文件被切成了哪些块（符号、种类、原始行号、父级）
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli chunk `
    --repo-root tests/fixtures/demo_repo --path app/service.py

# 离线提问（不调用模型）
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli ask `
    --repo-root tests/fixtures/demo_repo --question "load_tasks 做了什么？"

# 构建索引；加 --embed 同时写入向量（需要 .env 中的 EMBEDDING_*）
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli index `
    --repo-root tests/fixtures/demo_repo --out data/index/demo.jsonl --embed --vector-dir data/vectors

# 用向量检索提问
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli ask `
    --index data/index/demo.jsonl --repo-root tests/fixtures/demo_repo `
    --question "任务名称里的空白是怎么处理的？" --vectors

# 关键词 + 向量按 RRF 融合
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli ask `
    --index data/index/demo.jsonl --repo-root tests/fixtures/demo_repo `
    --question "任务名称里的空白是怎么处理的？" --hybrid

# 用真实模型回答（需要 LLM_* 配置）
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli ask `
    --repo-root tests/fixtures/demo_repo --question "..." --llm

# Agent 工具循环（search_code / read_file，带预算；需要 LLM_* 配置）
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli ask `
    --repo-root tests/fixtures/demo_repo --question "load_tasks 是怎么处理空名称的？" `
    --agent --max-steps 5 --max-tool-calls 5 --max-seconds 60 `
    --log data/runs/agent.jsonl

# 环境与模型能力自检（不打印密钥）
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli probe
```

配置：复制 `.env.example` 为 `.env` 并填入真实值；`.env` 已在 `.gitignore` 中。
聊天模型与 embedding 模型**分开配置**，不假设同一供应商。

## 目录结构

```
repoask/
  parse/      tree-sitter 分块、Markdown/文本备用切分
  ingest/     只读安全扫描与 git 状态
  retrieve/   BM25 检索、上下文组装
  vector/     Qdrant 本地向量存储
  embed/      embedding 客户端
  answer/     离线基线、OpenAI 兼容客户端
  agent/      LangGraph 工具循环（状态、工具、图、运行入口）
  cite/       GitHub 链接、引用校验
  index.py    版本绑定索引（JSONL）
  pipeline.py 建索引 / 向量检索 / 提问
  cli.py      chunk | index | ask | probe
tests/        行为测试 + 小型夹具仓库
docs/         架构、交接单、每阶段报告
scripts/      检索调试脚本
```

## 已知限制

- 检索质量**尚无结论**：阶段 2 只做了 5 题 × 3 路的观察（混合 top-1 与关键词相同），
  没有固定题库与对照实验，不能声称任何提升。
- 中文按二元组切词，常见词（如“时间”）可能误命中无关注释；RRF 的 k 与权重未调参。
- 不做调用图；动态调用与 `getattr` 一律不解析。
- 模型回答质量未评测，只验证了接口能力（调用/工具调用/流式/usage）。
- Agent 工具串行执行；证据上限 8 条，`read_file` 单次最多 200 行 / 4000 字符；
  引用修正不计入 `max_steps` 统计；没有流式与多轮会话记忆。
- `ask --index` 未给 `--repo-root` 时，引用只证明内部自洽，不会与真实文件比对。
- 本地 Qdrant 单进程持锁，不支持多进程并发写。
- 仅支持 Python 源码、README 与常见配置文件；其它类型跳过并记录原因。
