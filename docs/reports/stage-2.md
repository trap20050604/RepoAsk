# 阶段 2 增量 1 交付报告（模块九：教学与交接）

| 项 | 值 |
| --- | --- |
| 报告范围 | 阶段 2 增量 1：向量存储（Qdrant 本地持久化）+ embedding 元数据绑定 |
| 起始提交 | `9e0c0d6` |
| 当前提交 | 本次提交（见 `git log -1`） |
| 撰写日期 | 2026-10-19（Asia/Shanghai） |
| 验证环境 | Windows 10.0.26100，`D:\anaconda\envs\trap\python.exe`（Python 3.11.16） |
| 测试结果 | **109 个用例：107 passed，2 skipped** |
| 一句话结论 | 索引可以“带向量”构建，向量集合按“模型 + 维度”隔离、按“仓库 + commit”强制过滤；向量检索已接入同一条上下文与引用校验链路。关键词与向量的**融合（RRF）尚未实现**，因此本轮不对检索质量下任何结论 |

本增量**不做**：RRF 融合、混合检索对照实验、重排、GitHub 导入、API/UI。

---

## 1. 当前你能做什么

### 1.1 构建带向量的索引（真实调用 embedding）

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli index `
    --repo-root tests/fixtures/demo_repo `
    --out data/index/vector.jsonl --embed --vector-dir data/vectors
```

输出会多出两行关键信息：

```
embedding: {'provider': 'openai-compatible', 'model': 'qwen3.7-text-embedding-flash', 'dimensions': 1024}
vectors: {'path': 'E:\\RepoAsk\\data\\vectors', 'collection': 'repoask_qwen3_7_text_embedding_flash_1024'}
```

含义：这份索引的向量是由哪个模型、多少维产生的；向量存在哪个目录、哪个集合。**集合名内嵌模型与维度**，换模型就落到另一个集合。

### 1.2 用向量检索提问

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli ask `
    --index data/index/vector.jsonl --repo-root tests/fixtures/demo_repo `
    --question "任务名称里的空白是怎么处理的？" --vectors
```

与关键词模式共用同一套上下文组装与引用校验，唯一区别是候选来自向量检索，证据的 `source` 字段为 `vector`（轨迹里也能看到）。

### 1.3 离线测试向量链路

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m pytest tests/test_vector_store.py -v
```

10 个用例全部使用确定性桩向量，不联网、不需要密钥。它们证明**接线与约束**，不证明语义质量。

---

## 2. 本增量修改了哪些模块

| 模块 | 变更 | 关键点 |
| --- | --- | --- |
| `repoask/vector/store.py`（新增） | Qdrant 本地持久化封装 | 集合名 = 模型 + 维度；写入/检索强制版本过滤；维度不符直接报错 |
| `repoask/vector/__init__.py`（新增） | 导出 | — |
| `repoask/index.py` | 新增 `VectorRef`，`ChunkIndex.vectors` | 索引自描述：向量存在哪、集合叫什么 |
| `repoask/pipeline.py` | `build_local_index(embedder=..., vector_path=...)`、`_attach_vectors`、`search_vector`、`ask(hits=...)` | 向量写入与检索都走同一条版本校验 |
| `repoask/retrieve/keyword.py` | `SearchHit.source` | 关键词/向量共用命中类型，证据可标注来源 |
| `repoask/retrieve/context.py` | 证据 `source` 取自命中 | 对照实验能区分候选来源 |
| `repoask/cli.py` | `index --embed --vector-dir`、`ask --vectors` | 两个开关即可切换检索方式 |
| `repoask/httpcompat.py`（新增） | `sanitize_no_proxy()` | 修复代理变量导致 HTTP 客户端无法创建的崩溃 |
| `repoask/embed/client.py`、`answer/llm.py` | 构造客户端前调用兼容清理 | 并在探针输出中说明清理了什么 |
| `tests/test_vector_store.py`（新增） | 10 个用例 | 桩向量，离线 |
| `tests/test_httpcompat.py`（新增） | 5 个用例 | 复现并锁定 NO_PROXY 故障 |

---

## 3. 一次请求怎样经过这些模块

### 3.1 构建向量索引（`index --embed`）

```
CLI._cmd_index
  └─ Settings.load()                   读 .env（密钥只在这里）
  └─ EmbeddingClient(settings)         构造前先 sanitize_no_proxy()
  └─ pipeline.build_local_index
       ├─ scan/decode/chunk             同阶段 1
       ├─ ChunkIndex(...)               版本绑定
       ├─ embedder.embed([chunk.content ...])   → 25 条 1024 维向量（1 次请求、1467 tokens）
       ├─ collection_name(model, dims)   集合名内嵌模型与维度
       ├─ VectorStore(...).upsert_chunks 写入前校验：数量一致、维度一致、版本一致
       └─ index.embedding / index.vectors / stats 记录元数据
  └─ ChunkIndex.save()                  JSONL（向量不落在这里，只在 Qdrant）
```

### 3.2 向量检索提问（`ask --vectors`）

```
CLI._cmd_ask
  └─ ChunkIndex.load()                  读回索引（含 vectors 位置与 embedding 元数据）
  └─ pipeline.search_vector
       ├─ embedder.embed([question])    一条查询向量
       ├─ 模型一致性校验                 索引模型 ≠ 当前模型 → 直接报错
       ├─ VectorStore.search            filter: repo_id AND commit
       └─ payload.chunk_id → 索引中的 Chunk（正文以索引为准）
  └─ pipeline.ask(hits=...)             同一套上下文、预算、引用校验
  └─ RunLogger                         search 事件里带 source=vector
```

**为什么正文从索引回取，而不是存在向量库里**：向量库只保存标识与分数，正文与行号永远来自 `ChunkIndex`，避免两份数据漂移；引用校验也比对索引与真实文件。

---

## 4. 关键取舍与已知限制

| 取舍 | 原因 | 代价 | 何时重估 |
| --- | --- | --- | --- |
| 集合名由“模型 + 维度”派生 | 物理隔离，旧向量不可能被新模型误用 | 换模型后需要重建索引（不能增量迁移） | 若将来要支持模型迁移，再写显式迁移脚本 |
| 打开集合时校验维度 | 维度不符时余弦相似度会“算得出来但没意义” | 多一次集合元数据读取 | 无需重估 |
| 写入/检索都强制 `repo_id + commit` | 同一集合可放多版本，但绝不串版本 | 每个查询多两个过滤条件 | 无需重估 |
| 向量库只存标识与分数，正文回取索引 | 单一事实来源，避免两处内容不一致 | 检索后要多一次内存映射 | 无需重估 |
| 查询前校验 embedding 模型一致 | 不同模型的向量空间不可比 | 换模型必须重建 | 阶段 5 做模型对比时会频繁重建，属预期成本 |
| 本地模式 Qdrant（`path=`） | 无服务、无网络依赖，适合演示与离线测试 | 无 `search()`（用 `query_points`）、payload 索引是 no-op、单进程持锁 | 阶段 4 若要并发服务化，切到 Qdrant server |
| 桩向量只用于测试 | 保持测试离线、确定性 | 桩向量的排序没有语义意义 | 质量结论一律用真实模型 + 固定题库（阶段 5） |
| **未实现 RRF** | 先把“向量可存、可查、可校验”做扎实 | 现在只能二选一：关键词或向量 | 下一增量：按排名融合，并保留各自分数供对照 |

### 已知限制

1. 向量检索**没有质量结论**：本轮只在夹具上做了 2 个问题的观察，样本不足以说明优劣。
2. 没有混合检索与重排；`--vectors` 与默认关键词是互斥的两条路。
3. 向量不随 JSONL 索引一起移动：索引里记录的是绝对路径，换机器需要重建。
4. 本地 Qdrant 单进程持锁：同一目录不能被两个进程同时打开（测试中已通过顺序开关规避）。
5. `NO_PROXY` 兼容层只处理方括号 IPv6 写法；其他畸形代理配置仍可能让 httpx 失败。

---

## 5. 实际验证命令和结果

### 5.1 测试

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m pytest
# 107 passed, 2 skipped in 6.01s
```

新增用例：`test_vector_store.py` 10 个（版本过滤、跨版本写入拒绝、维度不符拒绝、换模型换集合、删除版本、命中映射回索引、模型不一致拒绝、无向量索引拒绝、`ask(hits=...)` 标注来源），`test_httpcompat.py` 5 个。

### 5.2 真实构建（一次真实 embedding 调用）

```
chunks: 25
embedding: qwen3.7-text-embedding-flash / 1024 维 / provider=openai-compatible
collection: repoask_qwen3_7_text_embedding_flash_1024
embedding_ms: 1182    embedding_tokens: 1467    vectors: 25    build_ms: 77
```

### 5.3 同题对照（仅观察，不作质量结论）

问题 A：「任务名称里的空白是怎么处理的？」

| 检索方式 | top-1 | top-2 | top-3 |
| --- | --- | --- | --- |
| 关键词 | `load_tasks` | `build_tasks` | README「安装」 |
| 向量 | `load_tasks` | `build_tasks` | `service.py:53-54`（`if __name__` 块） |

问题 B：「重试逻辑是在哪个函数里实现的？」

| 检索方式 | top-1 | top-2 | top-3 |
| --- | --- | --- | --- |
| 关键词 | `with_retry` | `service.py:1-6`（模块头） | `broken.py:1-6` |
| 向量 | `with_retry.decorator.wrapper` | `broken.py:1-6` | `load_tasks` |

两次都仍在**同一条引用校验链路**下产出 2/2 有效引用；但排序差异说明：现在既不能声称向量更好，也不能声称更差，必须等固定题库与 RRF 融合（阶段 5）再下结论。

### 5.4 修复的真实故障：代理变量导致 HTTP 客户端无法创建

现象：`index --embed` 直接崩溃，堆栈为 `httpx.InvalidURL: Invalid port: ':1]'`。

根因：本机代理客户端把 `NO_PROXY` 写成 `localhost,127.0.0.1,::1,[::1]`；httpx 在构造 `Client` 时把每个条目当 URL 模式解析，方括号 IPv6 触发异常。**任何** `openai.OpenAI(...)` 都建不起来，聊天与 embedding 一起失效。

修复：新增 `sanitize_no_proxy()`，在构造客户端前移除方括号写法（保留 `::1` 本身），并把被移除的条目回报到探针输出中。验证：

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m pytest tests/test_httpcompat.py -v   # 5 passed
```

---

## 6. 建议你阅读的 3 个函数

### 6.1 `VectorStore.upsert_chunks`（`repoask/vector/store.py`）

- **职责**：把分块与向量成对写入集合。
- **看什么**：三个前置校验——数量一致、维度一致、`repo_id/commit` 与本次构建一致；以及 `point_id(chunk_id)` 的 UUID5 派生（同一分块重复导入会覆盖同一点，幂等）。
- **常见误解**：以为“写了向量就等于建好了索引”。向量库只是候选来源之一，上下文与引用仍由 `ChunkIndex` 决定。

### 6.2 `VectorStore.search`（同文件）

- **职责**：带 `repo_id + commit` 过滤的向量查询。
- **看什么**：`models.Filter(must=[...])` 两个条件缺一不可；`query_points` 而不是 `search`（本地模式没有 `search`）。
- **常见误解**：以为过滤是“检索后筛掉”。它是查询期过滤（payload filter），不会先把别的版本召回再丢弃。

### 6.3 `pipeline.search_vector`（`repoask/pipeline.py`）

- **职责**：把问题变成向量、查库、把命中映射回索引分块。
- **看什么**：`index.embedding.model != model` 时直接抛 `VectorStoreError`；`payload["chunk_id"]` → `by_id` 的映射；返回的 `SearchHit.source="vector"`。
- **常见误解**：以为换模型只要重新算问题向量。索引侧向量也必须重建，否则两个空间不可比——代码选择直接报错而不是给出错误结果。

---

## 7. 一个你能亲手完成的小练习及验收方法

### 练习 A：亲眼看到“换模型就换集合”（10 分钟）

1. 用现有配置建一次向量索引：

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli index `
    --repo-root tests/fixtures/demo_repo --out data/index/v1.jsonl --embed --vector-dir data/vectors
```

2. 把 `.env` 里的 `EMBEDDING_DIMENSIONS` 临时改成 `768`（**不要**改模型名），再建一次到另一个输出文件。

**验收标准**：第二次命令**失败**并明确报“实际维度 1024 与配置 768 不一致”；把配置改回 1024 后恢复成功。你能解释为什么这里要“失败”而不是自动适配（维度声明是契约，静默适配会让旧索引被错误复用）。

### 练习 B：验证版本过滤真的生效（15 分钟）

```powershell
# 用当前夹具构建，记录 commit
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli index `
    --repo-root tests/fixtures/demo_repo --out data/index/v2.jsonl --embed --vector-dir data/vectors

# 用另一个 commit 标签去问同一个索引（模拟版本不一致）
& 'D:\anaconda\envs\trap\python.exe' -c "
from repoask.index import ChunkIndex
from repoask.pipeline import search_vector
from repoask.config import Settings
from repoask.embed.client import EmbeddingClient
idx = ChunkIndex.load('data/index/v2.jsonl')
try:
    search_vector(idx, 'load_tasks', EmbeddingClient(Settings.load()), vector_path='data/vectors')
except Exception as exc:
    print('预期报错：', type(exc).__name__)
"
```

**验收标准**：能说出“集合里可能存着多个版本的点，但每次查询都被 `repo_id + commit` 过滤住”；并能指出过滤发生在 `VectorStore.search` 的哪一行。

### 练习 C：最小预算下的向量证据（10 分钟）

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli ask `
    --index data/index/vector.jsonl --repo-root tests/fixtures/demo_repo `
    --question "任务名称里的空白是怎么处理的？" --vectors --max-chars 400
```

**验收标准**：输出出现“因预算只展示前 N 行 / 丢弃（budget）”，但引用检查仍然有效。这说明**裁剪只影响模型看到的文本，不影响引用校验**（后者用完整证据与真实源码）。

---

## 附录：本增量明确未做

RRF 融合、混合检索对照实验、重排、真实题库、GitHub 导入、FastAPI、Streamlit。下一增量：`repoask/retrieve/fusion.py` 用 **RRF 按排名**融合两路结果，并在轨迹里同时保留两路分数与名次。
