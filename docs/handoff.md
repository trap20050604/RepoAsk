# 交接单（handoff）

最后更新：2026-10-19（Asia/Shanghai）。
当前阶段：**阶段 1 完成并处置完独立审查第 1 轮；阶段 2 增量 1（向量存储）完成**。
下一阶段：阶段 2 增量 2（RRF 混合检索 + 对照观察）。

## 提交与推送

- 当前提交：以 `git log -1` 为准（本文件随每次提交一起更新）。
- 阶段 2 增量 1 的起始提交：`9e0c0d6`。
- 审查基线：`35caa13`；审查修复提交：`1015470`（看代码差异用 `git diff 35caa13..1015470`）。
- 阶段 1 功能提交：`9ee1115`。
- 推送：全部推送到 `origin/main`。本机到 GitHub 的链路**间歇性中断**（曾出现
  `Connection was reset` / `443 端口连接超时`，而同时刻 `curl https://github.com` 返回 200）。
  处理方式就是**重试** `git push`，不要改 git 配置，更不要关闭证书校验。
- 本机代理客户端会设置 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY=http://127.0.0.1:7897`，
  GitHub 可达性与代理开关状态相关。

## 阶段报告（模块九，每阶段一份）

| 报告 | 内容 |
| --- | --- |
| [`docs/reports/stage-1.md`](reports/stage-1.md) | 阶段 1 + 审查第 1 轮：能力、模块、请求流、取舍与限制、验证、阅读清单、练习 |
| [`docs/reports/stage-2.md`](reports/stage-2.md) | 阶段 2 增量 1：向量存储、embedding 元数据、真实对照观察、NO_PROXY 故障处理 |

`docs/learning-guide.zh-CN.md` 是项目所有者自己维护的学习手册（当前**未纳入版本控制**），
本项目的文档不重复其内容。

## 环境

- 解释器：`D:\anaconda\envs\trap\python.exe`，Python 3.11.16（Windows 10.0.26100）。
  PATH 上的 `python` 是另一个 3.8 安装，**必须用全路径**。
- 为项目安装的依赖（未改动其它环境）：
  `tree-sitter==0.25.2`、`tree-sitter-python==0.25.0`、`qdrant-client==1.19.1`
  （附带 h2、hpack、hyperframe、portalocker、pywin32）。
  注意：`tree-sitter-python` **没有 0.25.1 版本**，镜像上最高 0.25.0。
- 本机目录权限有两处异常，均已处理或绕过：
  - `E:\RepoAsk` 原先缺少 `WRITE_OWNER`，已为当前用户修复；备份与回滚脚本在
    `E:\RepoAsk-acl-recovery\`（`acl-backup-8c2feff9*.ps1`）。
  - `%TEMP%\pytest-of-ncp` 连读 ACL 都被拒绝，无法在无提权情况下修复；
    已在 `tests/conftest.py` 把 pytest 的 `basetemp` 重定向到项目内 `.pytest-tmp/`。

## 已真实验证的能力（有调用记录，不是推断）

| 能力 | 端点 / 模型 | 实测结果 |
| --- | --- | --- |
| 聊天基础调用 | `deepseek-chat` @ `api.deepseek.com/v1` | 通过，回复 `pong` |
| 聊天 usage | 同上 | 通过，返回 token 统计 |
| 聊天**工具调用** | 同上 | 通过，返回 1 个 tool call |
| 聊天流式 | 同上 | 通过，16 个 chunk / 14 字符 |
| Embedding | `qwen3.7-text-embedding-flash` @ DashScope 兼容接口 | 通过，**1024 维**（与配置一致） |
| 向量索引构建 | 25 个分块 | `embedding_ms=1182`、`embedding_tokens=1467`、集合名含模型与维度 |
| Qdrant 本地模式 | `qdrant-client==1.19.1` | 建集合/写入/payload 过滤/重开均通过；本地无 `search()`，用 `query_points`；`create_payload_index` 是 no-op |

## 测试

```
python -m pytest   →   107 passed, 2 skipped in 6.01s
```

| 测试文件 | 用例数 | 覆盖 |
| --- | --- | --- |
| `test_python_chunker.py` | 14 | 行号不变式、覆盖完整、装饰器、嵌套、类边界、语法错误恢复、切分、编码与换行 |
| `test_review_fixes.py` | 18 | 独立审查 8 项缺陷的回归 |
| `test_citation_verify.py` | 13 | 引用校验各失败分支、URL 构造、输入校验 |
| `test_ingest_local.py` | 10 | 跳过规则、上限、符号链接、路径越界 |
| `test_retrieval.py` | 10 | 分词、符号命中、版本隔离、去重、预算、渲染 |
| `test_llm_client.py` | 9 | 重试、超时、usage 缺失、修正采纳规则（桩，无网络） |
| `test_stage1_end_to_end.py` | 8 | 端到端、版本绑定、轨迹事件、缺配置分支 |
| `test_config_and_logging.py` | 7 | `.env` 解析、密钥脱敏、日志截断 |
| `test_httpcompat.py` | 5 | `NO_PROXY` 方括号 IPv6 导致 httpx 崩溃 |
| `test_text_chunker.py` | 5 | Markdown 标题/围栏、配置文本、类型分发 |
| `test_vector_store.py` | 10 | 向量写入/检索的版本隔离、维度校验、集合隔离、命中映射 |

跳过 2 项均为“创建符号链接需要权限”的自跳过用例，**不算通过的安全检查**。

## 独立审查第 1 轮处置（基线 `35caa13`）

8 项全部先复现、再修复，每项都有失败优先的回归测试（`tests/test_review_fixes.py`）。

| # | 级别 | 判断 | 改动 |
| --- | --- | --- | --- |
| 1 | P1 | 成立 | 脏/未跟踪工作树不再标 HEAD：`inspect_git_worktree` + `git_head`/`worktree_dirty`，版本退化为内容摘要 |
| 2 | P1 | 成立 | 引用核对改走 `pipeline.load_indexed_source_texts`（复用安全扫描），不再 `rglob` + 裸读 |
| 3 | P2 | 成立 | 控制流内的定义递归分块（`if`/`try` 内的 `def`），保持作用域 |
| 4 | P2 | 成立（单行装饰器原本正常） | 括号配平回溯，识别多行装饰器、空行、注释 |
| 5 | P2 | 成立 | 预算按真实渲染长度计费（含分隔符）；超限裁剪并记录；不足一行代码的证据直接丢弃 |
| 6 | P2 | 成立 | 修正只在 `all_valid` 或“不足以判断且无引用”时采纳 |
| 7 | P2 | 成立 | 裸 `\r` 与 CRLF 一并归一化 |
| 8 | P2 | 成立（契约/纵深防御，未越出根目录） | 绝对路径/盘符路径解析前即拒绝 |

未采纳：审查者列为“可选改进”的 `file_texts=None` 时 `all_valid` 但未核对源码，
当前以 `source_checked_count=0` 明示，是否升级为硬失败留待阶段 4 的调用方设计。

## 本轮另外修掉的两个缺陷

1. **轨迹耗时被业务字段覆盖**：`done` 事件原用 `elapsed_ms` 传管道耗时，覆盖了日志自身
   的标准列，导致出现“1 ms 在 83 ms 之后”的非单调记录。改为 `pipeline_elapsed_ms`，
   并加断言：轨迹的 `elapsed_ms` 必须单调不减。
2. **代理变量导致 HTTP 客户端无法创建**：`NO_PROXY` 含 `[::1]` 时，httpx 抛
   `InvalidURL: Invalid port`，聊天与 embedding 客户端全部无法构造。新增
   `repoask/httpcompat.sanitize_no_proxy()`，在构造前移除方括号写法并在探针中回报。

## 已知限制（如实记录）

1. **检索质量没有结论**：阶段 2 只做了 2 个问题的观察；关键词与向量排序不同，
   但样本不足以说明优劣。质量结论要等固定题库（阶段 5）。
2. **没有 RRF / 混合检索 / 重排**：`--vectors` 与默认关键词是两条互斥路径。
3. 检索仍是词法 + 向量的“二选一”；中文按二元组切分，`时间` 之类常见词仍可能误命中。
4. 无调用图；动态调用、`getattr`、装饰器注入一律不解析。
5. 模型回答质量未评测；只验证了接口能力。
6. `file_texts=None` 时引用只证明内部自洽（`source_checked_count=0` 会显示）。
7. 预算裁剪后，模型看到的行少于引用范围（已在说明中标注）。
8. 向量以绝对路径记录；换机器或换目录需重建索引；本地 Qdrant 单进程持锁。
9. 裸 CR 文件的行号与 GitHub 行锚点可能不一致（行尾已归一化为 LF）。
10. GitHub 导入、FastAPI、Streamlit、题库与对照实验均未实现。

## 下一步

1. **RRF 融合**：新增 `repoask/retrieve/fusion.py`，按**排名**融合关键词与向量结果
   （不使用不可比的分数相加），轨迹里同时保留两路分数与名次。
2. 给 `ask` 增加 `--hybrid`，并记录三种模式的候选来源分布。
3. 用固定 5 道手检题观察三种模式的名次差异，只记录事实，不宣称提升。
4. 之后进入阶段 3（LangGraph 工具循环与预算）。

## 阅读入口

- `repoask/parse/python_chunker.py`：`chunk_python_source`、`_decorator_group_start`
- `repoask/pipeline.py`：`build_local_index`、`search_vector`、`ask`
- `repoask/vector/store.py`：`upsert_chunks`、`search`
- `repoask/cite/verify.py`：`verify_citations`
