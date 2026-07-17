# FPT 2026 Track A

本仓库用于团队协作开发 FPT 2026 Track A 的 LLM4HLS Agent。

当前已经建立内部里程碑 V0 的无 LLM 确定性验证闭环。文档统一放在 [`doc/`](doc/) 中，Agent 源码和测试位于 [`llm4hls_harness/`](llm4hls_harness/)；下载的官方参考仓库只保存在被 Git 忽略的 `_external/` 中，不作为运行时依赖。

官方 reference harness 的功能宽度大致达到内部 V2 原型，并触及 hidden grading、deadlock task 和容器等 V4 素材；但它缺少不可变 baseline、持久 ledger、幂等恢复和 candidate registry，因此不能按本项目标准视为完整 V0。详细映射见[官方 Reference Harness 与内部 V0 对比](doc/materials/02_harness/2026-07-14-official-reference-vs-internal-v0.md)。

## Start Here

1. [团队知识库入口](doc/README.md)
2. [Track A 比赛总览](doc/docs/track-a-competition-overview.md)
3. [Track A 0 基础入门手册](doc/docs/track-a-zero-foundation-guide.md)
4. [Budget-Aware LangGraph LLM4HLS Agent 设计总规范](doc/materials/04_agent_basics/2026-07-17-budget-aware-langgraph-llm4hls-agent-design.md)
5. [资料总索引](doc/materials/00_index/README.md)
6. [Agent 开发规则](llm4hls_harness/AGENTS.md)
7. [内部 V0 中文说明](llm4hls_harness/README_CN.md)
8. [官方 Reference Harness 与内部 V0 对比](doc/materials/02_harness/2026-07-14-official-reference-vs-internal-v0.md)

## Repository Layout

```text
doc/
  README.md   原知识库入口
  docs/       面向团队阅读的成品文档
  materials/  官方资料、学习笔记、设计讨论、实验模板
llm4hls_harness/
  AGENTS.md       Agent 代码目录的开发规则和验收边界
  README_CN.md    内部 V0 中文使用说明
  llm4hls_agent/  自包含的 V0 实现
  tests/           快速单元测试
_external/         本地只读参考仓库，不提交、不作为包依赖
```

## Team Rules

1. 文档和资料统一放入 `doc/`，不要散落在仓库根目录。
2. Agent 代码、测试、Docker 和运行入口后续使用独立目录。
3. 不提交 API key、账号、license server、私有服务器密码或 `.env`。
4. 不提交 Vitis 生成目录、实验运行产物和下载的外部仓库。
5. 文档必须区分官方规则、reference harness 行为和团队设计判断。
