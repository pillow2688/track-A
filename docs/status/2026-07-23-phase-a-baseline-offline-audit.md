# Phase A / Phase A.1 基线恢复、离线审计与冻结状态

## 阶段目标

Phase A 的目标是恢复确定性 Router 契约、离线审计既有 72 个真实公共 run、盘点当前 28 题并形成后续真实运行提案。Phase A.1 负责复核上述结果、正式提交 Router 修复、修正中文验收口径并冻结可复现的本地 Git 基线。

本阶段不运行真实 LLM、CSim、Synth 或 CoSim，也不启用 Continuation Enforce、Experience Guided 或 learned Strategy Ranker。

## 仓库基线

- 分支：`feat/track-a-empty-stub-generation-smoke`
- Phase A 起始 HEAD：`65ab82c13f28921356b77ea454306d6dc1ca8e17`
- Router 修复代码 commit：`e5ba32a032432579bb9daa8015d2f705cff93498`
- Phase A.1 文档冻结 HEAD：提交后记录于 `docs/experiments/artifacts/2026-07-23-phase-a1-freeze/final-commit-summary.json`
- Router 提交只包含产品代码和直接回归测试。
- 五个 `PRE_EXISTING_USER_WORK` 文件没有被修改、暂存或提交；整体工作区最终状态以 `final-git-state.txt` 为准。

## Router P0 与测试

修复前，generation 元数据会覆盖已经观测到的 baseline facts，使八个 baseline 已通过的 OPTIMIZE 语料错误进入 REPAIR。正确契约为：

```text
CSim FAIL
→ REPAIR

CSim PASS，Synth FAIL
→ SYNTH_FIX

CSim/Synth PASS，required 或 observed CoSim FAIL
→ STRUCTURAL_FIX

任务所需 baseline gate 全部 PASS
→ OPTIMIZE
```

Generation 字段只描述空/不完整实现和 Planner 上下文权限，不能选择 PhaseMode。修复没有增加任务 ID 特例、第五种 Mode、LangGraph 节点，也没有改变 Budget、Candidate 或 final policy。

| 检查项 | 结果 |
|---|---:|
| 修复前完整测试 | 555 tests；8 failures；0 errors |
| Phase A.1 聚焦 Router/语料测试 | 25/25 PASS |
| Phase A.1 提交前完整测试 | 556/556 PASS；0 failures；0 errors |
| `compileall` | PASS |
| `git diff --check` | PASS |

## 历史 72-run 离线审计

- 目标/实际定位：**72/72**
- Manifest 有效：**72/72**
- 验证文件数量：**85,500**
- Ledger 对账：**72/72**
- 可比较 repeat pairs：**36/36**
- terminal agreement：**35/36 = 0.9722**

`35/36` 只表示成对终止结果一致性，不表示统计显著性、总体可靠性或方差已经收敛。

## Continuation 绑定漏斗

72 个历史 run 中：

- 10 个 run 含 follow-up decision point；
- 共发现 10 个候选 decision point；
- 9 个满足完整绑定条件；
- 1 个因准入条件不满足被排除；
- 当前准入：`INSUFFICIENT_EVIDENCE`；
- 当前 authority：`SHADOW`。

本次审计没有证明 Continuation 能降低 Token、Tool Credit 或 wall time，也没有证明性能提升，因此禁止启用 `enforce`。

## Experience 与 Strategy Ranker

- Experience Candidate records：**81**
- Strategy Ranker Candidate records：**81**
- Experience authority：`SHADOW`
- Strategy Ranker authority：`BAYESIAN_SHADOW`
- `training_ready=false`
- learned Strategy Ranker：未训练、未准入

81 是从 72 个 run 中抽取的 Candidate record 数；一个 run 可以产生多个 Candidate，因此 Candidate record 数量不能与 run 数直接等同。

## 旧 V3-F 结论

旧 V3-F 报告保持原文件和原结论不变：

- scanned runs：83
- completely bound samples：11
- beneficial/essential：5
- harmful/waste：6
- beneficial retention：60%
- waste block rate：16.7%
- admission：`NOT_READY`

Phase A 新审计的结论是 `INSUFFICIENT_EVIDENCE / SHADOW`，不会覆盖旧报告，也不会自动提升 authority。

## 当前 28 题与影响分析

- 当前 inventory：**28/28**
- Mode 分布：REPAIR 8、SYNTH_FIX 6、STRUCTURAL_FIX 6、OPTIMIZE 8
- `requires_cosim=true`：7/28
- 历史覆盖：**12/28**
- 当前代码路径影响：`GLOBAL_BEHAVIORAL` **28/28**
- 正式同版本矩阵建议：`REQUIRED`

历史 Artifact 可用于描述性工程分析，但 Planner、task contract/final、Budget 和 Vitis 路径存在全局行为变化，不能替代当前冻结版本的正式矩阵。

## 运行提案状态

- Gate B1 五类真实 Anchor：`TODO / AWAITING_APPROVAL`
- Gate B2 当前冻结版本正式 28 题矩阵：`TODO / AWAITING_APPROVAL`
- 原独立 16 题 gap 批次：`DROPPED_AS_DEFAULT`
- 推荐方案：预算优先的方案 A
- 备选方案：证据优先的方案 B
- 当前准入：`AWAITING_USER_APPROVAL`

五类 Anchor 和 28 题矩阵均未运行；Proposal 完成不等于真实实验完成。

## 安全与预算

- 仅使用公共 train/dev Artifact 做离线审计。
- 未打开或传入 hidden、reference 或 golden 内容。
- 旧 V3-F 报告未修改。
- 真实 LLM calls：**0**
- 真实 Token：**0**
- CSim / Synth / CoSim：**0 / 0 / 0**
- Tool Credits：**0**

## 限定性结论

**Phase A 阶段状态：DONE**

**Phase A 验收结论：ACCEPTED**

该结论仅表示：

- Router P0 已修复并提交；
- 单元测试全绿；
- 历史 72-run Artifact 离线审计完成；
- 28 题 inventory 和影响分析完成；
- 下一阶段两级 Gate 提案已经生成。

该结论不表示：

- 五类真实 Anchor 已完成；
- 当前版本 28 题正式矩阵已完成；
- 多模型矩阵已完成；
- 项目已经达到提交条件。

**实验基线冻结：DONE**

下一阶段必须等待用户审查并批准真实运行方案。
