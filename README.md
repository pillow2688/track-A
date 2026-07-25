# FPT 2026 Track A

本仓库用于团队协作开发 FPT 2026 Track A 的 LLM4HLS Agent。

当前代码处于 V3-F 安全基线与 Track A 提交收口阶段：四种 task-aware mode、空 Stub Generation、不可变 Candidate、预算、真实 Vitis、fresh final、Experience/Strategy Ranker Shadow 和 Continuation advisory 均已形成，但当前仍有 8 个 OPTIMIZE 路由测试回归，三模型矩阵、当前 28 题真实回归和最终提交包尚未冻结。完整事实见[新成员完整接手指南](doc/docs/2026-07-22-new-member-complete-onboarding.md)，一周收口路径见[7 天完成项目执行手册](doc/docs/2026-07-23-one-week-project-completion-handbook.md)。下载的官方参考仓库只保存在被 Git 忽略的 `_external/` 中，不作为运行时依赖。

官方 reference harness 的功能宽度大致达到内部 V2 原型，并触及 hidden grading、deadlock task 和容器等 V4 素材；但它缺少不可变 baseline、持久 ledger、幂等恢复和 candidate registry，因此不能按本项目标准视为完整 V0。详细映射见[官方 Reference Harness 与内部 V0 对比](doc/materials/02_harness/2026-07-14-official-reference-vs-internal-v0.md)。

## Start Here

1. [新成员完整接手指南（复核至 2026-07-23 当前 HEAD）](doc/docs/2026-07-22-new-member-complete-onboarding.md)
2. [接手后 7 天完成项目执行手册](doc/docs/2026-07-23-one-week-project-completion-handbook.md)
3. [当前系统说明与团队月度进展](doc/docs/2026-07-20-current-system-and-team-progress.md)
4. [团队知识库入口](doc/README.md)
5. [Track A 比赛总览](doc/docs/track-a-competition-overview.md)
6. [Track A 0 基础入门手册](doc/docs/track-a-zero-foundation-guide.md)
7. [Budget-Aware LangGraph LLM4HLS Agent 设计总规范](doc/materials/04_agent_basics/2026-07-17-budget-aware-langgraph-llm4hls-agent-design.md)
8. [资料总索引](doc/materials/00_index/README.md)
9. [Agent 开发规则](llm4hls_harness/AGENTS.md)
10. [Agent 中文使用与历史说明](llm4hls_harness/README_CN.md)
11. [官方 Reference Harness 与内部 V0 对比](doc/materials/02_harness/2026-07-14-official-reference-vs-internal-v0.md)

## Repository Layout

```text
doc/
  README.md   原知识库入口
  docs/       面向团队阅读的成品文档
  materials/  官方资料、学习笔记、设计讨论、实验模板
docs/
  superpowers/ 历史设计规范和实施计划
  submission/  提交实验表、复现、失败分析、Demo 与 Checklist
llm4hls_harness/
  AGENTS.md       Agent 代码目录的开发规则和验收边界
  README_CN.md    V0–V3-D 中文使用和历史说明
  llm4hls_agent/  V0–V3-D Agent 实现
  tests/           单元测试和任务编排 smoke
  releases/        不可变、脱敏的阶段实验事实
_external/         本地只读参考仓库，不提交、不作为包依赖
```

## Team Rules

1. 团队知识和新人入口放 `doc/`；历史设计放 `docs/superpowers/`；提交素材放 `docs/submission/`；不可变实验摘要放 `llm4hls_harness/releases/`，不要散落在仓库根目录。
2. Agent 代码、测试、Docker 和运行入口后续使用独立目录。
3. 不提交 API key、账号、license server、私有服务器密码或 `.env`。
4. 不提交 Vitis 生成目录、实验运行产物和下载的外部仓库。
5. 文档必须区分官方规则、reference harness 行为和团队设计判断。
