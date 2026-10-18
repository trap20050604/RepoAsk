# AGENTS.md — working rules for this repository

RepoAsk answers source-level questions about one fixed commit of a public GitHub
repository, with file/line citations that a program can verify.

## Environment (verified 2026-10-18)

- Interpreter: `D:\anaconda\envs\trap\python.exe` (Python 3.11.16).
  The `python` on PATH is a different 3.8 install — always use the full path.
- Pinned: `tree-sitter==0.25.2`, `tree-sitter-python==0.25.0`
  (there is no `tree-sitter-python` 0.25.1 release).
- Run tests: `& 'D:\anaconda\envs\trap\python.exe' -m pytest`
- Inspect chunks: `... -m repoask.cli chunk --repo-root tests/fixtures/demo_repo --path app/service.py`
- Ask offline: `... -m repoask.cli ask --repo-root tests/fixtures/demo_repo --question "..."`

## Rules

- Do not modify global git config, do not disable certificate verification, do
  not delete or recreate `.git`.
- Never execute code from a target repository. Read bytes only.
- Secrets only via environment/`.env`; `.env.example` uses placeholders only.
- Do not log API keys; `RunLogger` redacts credential-shaped fields.
- Tests must not need network access or API keys.
- Do not weaken tests or swallow exceptions to make a check pass. If behaviour is
  wrong, fix the code; if a test's assumption is wrong, fix the test and say why.
- Never report unverified model results as real. The offline extractive baseline
  is labelled `offline_extractive` and must stay distinguishable from `llm`.

## Non-goals for now

No multi-agent orchestration, no knowledge graph, no distributed queue, no
running target-repo tests, no code-modification features.
