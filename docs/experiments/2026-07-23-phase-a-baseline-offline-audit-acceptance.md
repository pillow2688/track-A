# Phase A / Phase A.1 基线与离线审计验收报告

## 验收范围

本报告验收：

1. Router P0 的最小修复和直接回归测试；
2. 既有公共 72-run Token Policy A/B/C 批次的离线审计；
3. 当前 28 题 inventory 与当前代码影响分析；
4. 不执行真实调用的两级付费运行提案；
5. Phase A.1 本地 Git 基线冻结。

本报告不验收五类真实 Anchor、正式 28 题矩阵、多模型矩阵或 hidden 最终成绩。

## 仓库基线

- 分支：`feat/track-a-empty-stub-generation-smoke`
- Phase A 起始 HEAD：`65ab82c13f28921356b77ea454306d6dc1ca8e17`
- Phase A 验收对象（提交前）：起始 HEAD + 工作区 Router 修复 + 新增测试和离线审计文档
- Router 修复代码 commit：`e5ba32a032432579bb9daa8015d2f705cff93498`
- Phase A.1 文档冻结 HEAD：提交后记录于 `artifacts/2026-07-23-phase-a1-freeze/final-commit-summary.json`

因此，旧 HEAD 本身没有包含 Router 修复；通过验收的是随后形成并正式提交的 Router 代码基线。

## 预先存在的工作区状态

Phase A 开始前已有五个用户文件处于修改或未跟踪状态：

- `README.md`
- `doc/README.md`
- `doc/docs/README.md`
- `doc/docs/2026-07-22-new-member-complete-onboarding.md`
- `doc/docs/2026-07-23-one-week-project-completion-handbook.md`

它们均标记为 `PRE_EXISTING_USER_WORK`，本阶段没有修改、暂存或提交。Router、Phase A 审计与 Phase A.1 文档可以按路径独立辨认。

## Router 契约分析

Router 只能根据 baseline 验证事实选择四种 Mode：

```text
CSim FAIL                                      → REPAIR
CSim PASS，Synth FAIL                          → SYNTH_FIX
CSim/Synth PASS，required/observed CoSim FAIL  → STRUCTURAL_FIX
所需 baseline gate 全部 PASS                   → OPTIMIZE
```

Generation 元数据只能表明 kernel 为空/不完整，或为 Planner 放宽仍限于 kernel 的上下文和 Patch 大小；它不能选择 PhaseMode。

## Router 修复

根因是已提交代码读取 `task_type=generate`，并在 baseline gate 已通过后强制返回 REPAIR。修复删除了这一 mode override。

变更边界：

- 产品代码：`llm4hls_harness/llm4hls_agent/v3_phase_router.py`
- 直接测试：`llm4hls_harness/tests/test_v3_phase_router.py`
- 无硬编码 task ID
- 无第五种 Mode
- 无新增 LangGraph 节点
- 无 Budget、Candidate 或 final policy 变化

修复后的 Router 产品文件与历史正确实现 commit `431f7a65627ab6911bd11d1a4a8ae6f57094ab08` 的同路径文件 SHA-256 相同。

## 单元测试结果

| 阶段 | 测试数 | Failure | Error | 结果 |
|---|---:|---:|---:|---|
| Phase A 修复前完整测试 | 555 | 8 | 0 | FAIL |
| Phase A.1 聚焦 Router/语料测试 | 25 | 0 | 0 | PASS |
| Phase A.1 提交前完整测试 | 556 | 0 | 0 | PASS |

另外：

- `compileall`：PASS
- `git diff --check`：PASS

提交后验证结果记录在 Phase A.1 证据目录。

## 历史 72-run 发现

唯一内容匹配批次为：

`llm4hls_harness/experiments/token_policy_abc/pilot-real-deepseek-v4-pro-20260722-01`

该批次满足 12 题 × 3 条件 × 2 次重复，共 72 个 schedule/result/run 槽位。单 run preflight 和 48-run hybrid 批次均因实验结构不匹配被排除。

## Artifact 完整性

- 历史目标/实际定位：72/72
- Manifest 有效：72/72
- 按 Manifest 校验文件：85,500
- Ledger 对账：72/72

13 条结果由 durable Artifact 显式对账，而不是伪装成普通终止 run。未删除、重写或覆盖历史原始 Artifact。

## 复用准入

- 描述性稳定性审计：72/72 可用
- Continuation：10 个 run 有候选 decision point，9 个完整绑定
- Experience/Ranker：72 个源 run 可产生公共真实 Artifact Candidate
- 所有导出的 Experience/Ranker record 均为 `training_ready=false`

“9 个完整绑定”不能简写成“9/72 run 是全部 Continuation 数据”，因为完整漏斗还包含 1 个被排除的候选点。

## 重复运行成对一致性

- 可比较 repeat pairs：36/36
- terminal agreement：35/36 = 0.9722

这只表示固定 pair 内的终止结果一致性，不构成统计显著性、总体成功率或稳定性收敛证明。

## Continuation 绑定漏斗

72 个历史 run 中：

- 10 个 run 含 follow-up decision point；
- 共发现 10 个候选 decision point；
- 9 个满足完整绑定条件；
- 1 个因准入条件不满足被排除；
- 准入：`INSUFFICIENT_EVIDENCE`；
- authority：`SHADOW`。

没有证据证明 Continuation 能降低 Token、Tool Credit 或 wall time，也没有证据证明性能提升。`enforce` 继续禁止。

## 信息泄漏审计

每条 replay record 将 `pre_state`、`policy_decision`、`outcome_label` 和 `leakage_audit` 物理分开，决策输入边界仅为 `pre_state`。未来 Planner response、Patch、Candidate、promotion、final 和 outcome 不作为策略输入。审计未发现违规。

## 旧 V3-F 对比

旧 V3-F 报告保持文件和结论不变：

- scanned runs：83
- completely bound samples：11
- beneficial/essential：5
- harmful/waste：6
- beneficial retention：60%
- waste block rate：16.7%
- admission：`NOT_READY`

本次 72-run 审计是新的受限样本审计，结论为 `INSUFFICIENT_EVIDENCE / SHADOW`，不能覆盖旧报告或据此启用 enforce。

## Experience Candidate 记录

共导出 **81** 条可绑定的公共真实 run Candidate record，用于离线 shadow 分析。81 条 Candidate record 可以来自 72 个 run，因为一个 run 可以包含多个 Candidate；两者不是同一统计单位。

Experience 当前为 `SHADOW`，未启用 `guided`，所有 record 均为 `training_ready=false`。

## Strategy Ranker Candidate 池

共导出 **81** 条 Bayesian shadow Candidate record。当前状态为：

- authority：`BAYESIAN_SHADOW`
- learned training：未执行
- learned routing：未启用
- admission：`TRAINING_NOT_READY`

这不是“Strategy Ranker 已训练”或“学习型排序器已准入”。

## 历史失败类型

历史 terminal 分布为：

```json
{"DONE": 54, "ERROR": 13, "FAILED": 5}
```

经 durable Artifact 对账的 post-validation 行仍保留其真实分类，没有被重标为普通成功。

## 当前 28 题清单

- inventory：28/28
- REPAIR：8
- SYNTH_FIX：6
- STRUCTURAL_FIX：6
- OPTIMIZE：8
- `requires_cosim=true`：7
- 历史覆盖：12/28

Mode 来自开发侧 baseline facts 的确定性 Router 重放；acceptance/oracle facts 不会进入 Planner。

## 当前版本影响分析

逐题影响分类为：

```text
GLOBAL_BEHAVIORAL 28/28
```

Router 修复恢复了历史 baseline-fact 语义，本身不被误记为 28 题逐题直接修改。全局影响来自独立的 Planner、task contract/final、Budget 与 Vitis 执行路径变化，因此历史 72-run 不能作为当前冻结版本的正式同版本矩阵。

## 下一阶段运行提案

原“5 个 Anchor + 独立 16 题 gap”默认执行方式已取消，改为两级 Gate：

- Gate B1：五类 `full_internal_audit` Anchor
- Gate B2 方案 A：五类 Anchor + 28 题 `task_contract` 矩阵
- Gate B2 方案 B：四个 V3-D Anchor 计入 28 题 `full_internal_audit` 矩阵，空 Stub 因不属于 V3-D 28 而单独计 1 个 run

独立 16 题清单仅保留为工程覆盖参考，状态为 `DROPPED_AS_DEFAULT`。当前推荐方案 A；真实执行状态为 `AWAITING_USER_APPROVAL`。

## 安全与信息隔离

- 未调用真实 DeepSeek 或 Qwen
- 未运行 CSim、Synth 或 CoSim
- 未启用 Continuation Enforce
- 未启用 Experience Guided
- 未训练 Strategy Ranker
- 未访问 hidden、reference 或 golden 内容
- 未修改旧 V3-F 报告
- 未 push，未创建 PR

## 预算使用

```text
真实 LLM calls = 0
真实 Token = 0
CSim = 0
Synth = 0
CoSim = 0
Tool Credits = 0
```

## 证据索引

- Phase A 审计证据：`docs/experiments/artifacts/2026-07-23-phase-a-offline-audit/`
- Phase A.1 冻结证据：`docs/experiments/artifacts/2026-07-23-phase-a1-freeze/`
- 28 题清单：`docs/experiments/current-28task-inventory.zh-CN.md`
- 当前影响：`docs/experiments/current-head-impact-analysis.zh-CN.md`
- 真实运行提案：`docs/experiments/current-head-paid-run-proposal.md`
- 完成看板：`docs/status/2026-07-23-track-a-completion-board.md`

## 验收结论

- **Phase A 验收：ACCEPTED**
- **Phase A.1 冻结：ACCEPTED**
- **真实实验准入：AWAITING_USER_APPROVAL**

Phase A/Phase A.1 的 ACCEPTED 只覆盖 Router 提交、离线审计、中文口径修正和本地基线冻结，不表示五类 Anchor、28 题正式矩阵、多模型矩阵或最终提交已经完成。
