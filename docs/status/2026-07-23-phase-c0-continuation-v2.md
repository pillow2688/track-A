# Phase C0 状态：Mode-Specific Continuation V2 离线 Shadow 试验

日期：2026-07-23  
阶段状态：**DONE（离线实现与回放完成）**  
准入结论：**INSUFFICIENT_EVIDENCE**  
V2 Shadow Pilot：**NOT_READY**  
Continuation Enforce：**DISABLED**

## 1. 本阶段完成内容

- 完成 Continuation V1 当前输入、共用规则、Evidence Delta、Strategy Novelty、决策条件、权限和信息泄漏审计。
- 新增独立纯函数 `v3.continuation-decision.v2`，不覆盖 V1，不接入主 Graph。
- 从旧 V3-F R02、72-run Token Policy A/B/C 和当前 Continuation 数据构造统一 follow-up decision-point 数据。
- 对 Phase B1/B1.1/B1.2 Anchor 和 Experience Candidate records 做重叠与 follow-up 可用性筛查。
- 完成 V1/V2 离线对照、按 Mode 指标、offline potential savings、leave-one-run-out 和 leave-one-task-out 稳定性检查。
- 新增四种 Mode 的规则测试和 future-field 隔离测试。

## 2. 权限边界

| 组件 | 当前状态 |
|---|---|
| Continuation V1 | IMPLEMENTED / SHADOW（运行时默认仍为 `off`） |
| Continuation V2 | OFFLINE_ONLY |
| Continuation V2 Shadow Pilot | NOT_READY |
| Continuation Enforce | DISABLED |
| Experience Guided | NOT_ADMITTED |
| Ranker Training | NOT_READY |

V2 只读取 `mode + pre_state`，输出仍以 `FOLLOW_EXISTING_MAIN_POLICY` 为 fallback。它不调用 LLM/Vitis，不创建 Candidate，不修改 BudgetLedger，不改变正式停止、晋升或 final 行为。

## 3. 数据绑定漏斗

| 项目 | 数量 |
|---|---:|
| 来源筛查记录 | 101 |
| 真实 follow-up 候选 decision points | 21 |
| 完整绑定 | 20 |
| follow-up 绑定排除 | 1 |
| 全部筛查排除 | 81 |
| REPAIR | 0 |
| SYNTH_FIX | 0 |
| STRUCTURAL_FIX | 3 |
| OPTIMIZE | 17 |

全部 20 个绑定点来自公开 train/dev 侧真实 LLM+Vitis Artifact。Outcome 为 `BENEFICIAL_PERFORMANCE=8`、`HARMFUL=12`。没有 REPAIR/SYNTH_FIX follow-up，也没有 `ESSENTIAL_STRUCTURAL` 正样本。

排除分布：`NO_FOLLOW_UP=74`、`OUTCOME_NOT_BINDABLE=1`、`DUPLICATE_DECISION=1`、`MISSING_DECISION_TIME_EVIDENCE=1`、`NOT_REAL_VITIS=4`。

## 4. V1 / V2 结果

| 指标 | V1 | V2 |
|---|---:|---:|
| ALLOW | 11/20 | 20/20 |
| BLOCK | 9/20 | 0/20 |
| DEFER_TO_FINAL | 0/20 | 0/20 |
| Beneficial/essential retention | 50.0%（4/8） | 100.0%（8/8） |
| Essential structural retention | INSUFFICIENT_EVIDENCE（0/0） | INSUFFICIENT_EVIDENCE（0/0） |
| Waste block rate | 41.7%（5/12） | 0.0%（0/12） |
| False block | 4/8 | 0/8 |
| Offline potential Token saving | 12,189 | 0 |
| Offline potential Credit saving | 73 | 0 |

V2 的保守规则消除了本数据集上的有益 false BLOCK，但没有识别出可安全停止的 waste；waste block rate 相对 V1 退化。节省数字仅是基于已知后续成本的离线反事实估计，不是真实节省。

## 5. 稳定性与泄漏

- 规则没有在回放 outcome 上调阈值，也没有 calibration。
- Leave-one-run-out：20/20 决策一致。
- Leave-one-task-out：9/9 task group 一致。
- Split digest：`a86232bb46d3c28286af0505c99d07fe1a2c268a2af82a5f1302ecdaad5cb6f4`。
- 该一致性证明纯规则不依赖 task/run 组上下文，不等价于统计泛化。
- 旧 R02 把终态 `result.budget` 放进 `pre_state`；Phase C0 已删除该字段，缺失值保留 `UNKNOWN/null`，没有用未来值回填。
- Phase C0 V2 policy 输入中未发现 follow-up response、Patch、future Candidate、promotion、future tool/final/outcome/latency/resource。
- 聚焦测试 27/27、全量测试 572/572、compileall 和 `git diff --check` 全部 PASS。

## 6. 未通过的准入条件

1. REPAIR follow-up 分母为 0。
2. SYNTH_FIX follow-up 分母为 0。
3. STRUCTURAL_FIX essential retention 分母为 0，不能声称 100%。
4. V2 waste block rate 从 5/12 退化到 0/12。
5. 当前 leave-one-group-out 仅证明确定性，样本不足以证明跨任务收益。

因此不能写 `V2_SHADOW_PILOT_CANDIDATE`，更不能启用 Enforce。

## 7. 阶段资源消耗

本阶段真实 LLM calls=0、真实 Token=0、CSim/Synth/CoSim=0/0/0、Tool Credits=0。未访问 hidden/reference/golden，未启动 28 题或多模型矩阵。
