# eval 目录说明

这里存放**实验输入与原始记录**。正式评测（阶段 5）尚未开始，本目录目前只有阶段 2 的
观察数据，**不构成质量结论**。

| 文件 | 内容 |
| --- | --- |
| `stage2_questions.jsonl` | 5 道观察题（id / question / type），仅供阶段 2 对照使用，不是正式题库 |
| `runs/stage2-observation.jsonl` | 首次观察：5 题 × 3 种检索方式 = 15 条原始记录（集合名为修复前的旧格式） |
| `runs/stage2-observation-rebuilt.jsonl` | 重建演示索引后的复跑记录：同样 15 条，用于核对重命名与重建没有改变 top-1 |

两次记录的唯一差异：`build_tasks` 的向量分数相差约 6e-4（0.52170 → 0.52230），
导致问题 q2 的混合结果里 `README.md` 的向量名次从 8 变 7。**这不代表代码变更**，
而是 embedding 服务对相同输入的两次调用并非逐位相同（详见 `docs/handoff.md` 的
“embedding 非确定性”一条）。因此跨运行比较时不要依赖原始分数，应固定设置并对比名次/命中。

生成命令（真实调用 embedding，需要 `.env` 中的 `EMBEDDING_*`）：

```powershell
& 'D:\anaconda\envs\trap\python.exe' -m repoask.cli index `
    --repo-root tests/fixtures/demo_repo --out data/index/vector.jsonl `
    --embed --vector-dir data/vectors

& 'D:\anaconda\envs\trap\python.exe' scripts/compare_retrieval.py `
    --index data/index/vector.jsonl `
    --questions eval/stage2_questions.jsonl `
    --out eval/runs/stage2-observation.jsonl
```

`data/` 在 `.gitignore` 中（索引与向量可重建）；`eval/runs/` 里的原始记录会提交，便于复盘。
