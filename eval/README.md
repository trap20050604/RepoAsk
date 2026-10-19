# eval 目录说明

这里存放**实验输入与原始记录**。正式评测（阶段 5）尚未开始，本目录目前只有阶段 2 的
观察数据，**不构成质量结论**。

| 文件 | 内容 |
| --- | --- |
| `stage2_questions.jsonl` | 5 道观察题（id / question / type），仅供阶段 2 对照使用，不是正式题库 |
| `runs/stage2-observation.jsonl` | 5 题 × 3 种检索方式 = 15 条原始记录，含两路名次与原始分数 |

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
