# Demo Repo

用于验证 RepoAsk 解析与检索的最小示例仓库。

## 安装

```bash
pip install -e .
# 代码块里的这一行不是标题，用来验证围栏处理
```

## 配置

配置文件是 `config.toml`，其中 `[server]` 段落定义端口和超时。

## 任务流程

`app/pipeline.py` 把配置解析和任务加载连起来。
