# FPT 2026 Track A

本仓库用于团队协作开发 FPT 2026 Track A 的 LLM4HLS Agent。

当前阶段以比赛调研、HLS/Agent 学习、架构讨论和实现契约为主，所有文档统一放在 [`doc/`](doc/) 中。后续 Agent 源码、测试和 Docker 文件放在仓库根目录的独立代码目录，不与讨论资料混放。

## Start Here

1. [团队知识库入口](doc/README.md)
2. [Track A 比赛总览](doc/docs/track-a-competition-overview.md)
3. [Track A 0 基础入门手册](doc/docs/track-a-zero-foundation-guide.md)
4. [Budget-Aware LangGraph LLM4HLS Agent 设计总规范](doc/materials/04_agent_basics/2026-07-14-budget-aware-langgraph-llm4hls-agent-design.md)
5. [资料总索引](doc/materials/00_index/README.md)

## Repository Layout

```text
doc/
  README.md   原知识库入口
  docs/       面向团队阅读的成品文档
  materials/  官方资料、学习笔记、设计讨论、实验模板
```

## Team Rules

1. 文档和资料统一放入 `doc/`，不要散落在仓库根目录。
2. Agent 代码、测试、Docker 和运行入口后续使用独立目录。
3. 不提交 API key、账号、license server、私有服务器密码或 `.env`。
4. 不提交 Vitis 生成目录、实验运行产物和下载的外部仓库。
5. 文档必须区分官方规则、reference harness 行为和团队设计判断。
