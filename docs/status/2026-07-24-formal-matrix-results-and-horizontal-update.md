# 2026-07-24 正式矩阵与横向组件更新状态

冻结 HEAD：`4a05763b593a527878a0056f64763126c58ee63b`  
正式模型：`deepseek-v4-pro`  
正式后端：Vitis 2025.2  
总状态：`FORMAL_MATRIX_COMPLETE_WITH_MEASURED_FAILURES`

## 1. 正式 28×1

固定协议未变：

```text
Repeat = 1
Continuation = off
Experience = off
Ranker = off
Fresh final = task_contract
Token policy = fixed
Primary retry = disabled
Batch = failure-isolating + resumable
```

`terminal_coverage.json` 验收通过：28 个预期 Slot 均且仅有一个明确终态，无缺失、
无重复、无失败重试。

| 结果 | 数量 |
| --- | ---: |
| DONE / E2E success | 23 |
| FAILED | 1 |
| ERROR | 4 |
| 合计 | 28 |

E2E 成功率为 82.14%。按期望 Mode 分层：

| Mode | 成功 / 总数 |
| --- | ---: |
| REPAIR | 7 / 8 |
| SYNTH_FIX | 6 / 6 |
| STRUCTURAL_FIX | 4 / 6 |
| OPTIMIZE | 6 / 8 |

失败分层：

| 优先级 | Task | 分类 | 需要改进 |
| --- | --- | --- | --- |
| P0 | 016、020 | `TERMINAL_LAST_CANDIDATE_BINDING` | 修复最后 Candidate 与终态绑定，保留正确的终态 Provenance |
| P0 | 021、022 | `INVALID_CANDIDATE_WORST_LATENCY` | 对缺失/无效 latency 使用显式不可比较终态，不能抛掉整个任务结果 |
| P1 | 006 | `MAX_TASK_REPAIR_ROUNDS` | 分析两次 Patch Policy 拒绝，改进 REPAIR 提案约束或拒绝后的恢复策略 |
| P1 | 018 | 静态标签与本轮 baseline 事实不同 | 将 Anchor 期望建立在本次 baseline evidence 上，避免固定 Mode 标签漂移 |

实际预算：39 次 LLM、85,768 tokens、83 CSim、68 Synth、21 CoSim、775 credits。
4 个 OPTIMIZE 成功提升性能，最大 8.7×，8× stop 已被实测触发。

## 2. Experience 更新

正式矩阵导入 31 条新 V2 记录；与 C1 冻结的 103 条记录分池合并为 134 条，原记录
未修改，record/source identity 均无重叠。

| 指标 | 数量 |
| --- | ---: |
| 总记录 | 134 |
| 离线排名/检索可用 | 102 |
| REPAIR / SYNTH_FIX / STRUCTURAL_FIX / OPTIMIZE | 43 / 25 / 11 / 55 |
| SUCCESS / FAILURE / NO_IMPROVEMENT | 66 / 43 / 25 |

C1 审计仍为 `PASS_PROMOTABLE`，仅表示记录层可用于隔离的离线分析；Experience
Guided 继续 `NOT_ADMITTED`，Planner ingestion 为 false。

## 3. Continuation 固定协议回放

正式矩阵中有 11 个 follow-up candidate：7 个能绑定终态，4 个因没有标准 Agent
终态而隔离；另有 17 个任务没有 follow-up。与旧数据合并后有 27 个可判定 decision：

```text
OPTIMIZE 23
REPAIR 1
STRUCTURAL_FIX 3
SYNTH_FIX 0
BENEFICIAL_PERFORMANCE 10
HARMFUL 17
```

V1 保留有益样本 4/10、阻断浪费 10/17；V2 固定规则全部 ALLOW，保留 10/10，
但没有阻断 17 个有害 continuation。由于没有 SYNTH_FIX follow-up、没有
essential structural outcome 且 Mode 极度不均衡，结论仍是
`INSUFFICIENT_EVIDENCE`。V2 保持 `OFFLINE_ONLY`，Enforce `DISABLED`。

## 4. Strategy Ranker 固定协议复评

没有调阈值，也没有用同一数据重新校准。102 条 eligible 记录下：

| Holdout | Coverage | 有害重复推荐率 | Group leakage |
| --- | ---: | ---: | ---: |
| Leave-one-task-out | 38.24% | 15.38% | 0% |
| Leave-one-task-family-out | 38.24% | 15.38% | 0% |

固定 Gate 要求 coverage 至少 40%、有害率至多 5%、每个 Mode 至少 10 条；
STRUCTURAL_FIX 只有 9 条 eligible。因此 C2 仍为 `NEGATIVE_RESULT`，Learned Ranker
为 `TRAINING_NOT_READY`，运行时保持 off。

## 5. 权限与边界

```text
Continuation Enforce = DISABLED
Experience Guided = NOT_ADMITTED
Strategy Ranker runtime = off
Main Graph modified by post-matrix update = false
Post-matrix LLM/Vitis budget = 0
C2 threshold tuning = false
hidden/reference/golden access = false
```

## 6. 证据入口

- 正式运行：`llm4hls_harness/experiments/formal_matrix_20260724_4a05763_deepseek_28x1`
- 正式矩阵审计：`docs/experiments/artifacts/2026-07-24-post-matrix-horizontal-update/formal-matrix-result-audit.json`
- Experience 合并清单：`docs/experiments/artifacts/2026-07-24-post-matrix-horizontal-update/combined-experience-manifest.json`
- C1 合并审计：`docs/experiments/artifacts/2026-07-24-post-matrix-horizontal-update/c1-combined-audit/c1-audit-summary.json`
- Continuation 复评：`docs/experiments/artifacts/2026-07-24-post-matrix-horizontal-update/continuation-fixed-protocol-reevaluation.json`
- Ranker 复评：`docs/experiments/artifacts/2026-07-24-post-matrix-horizontal-update/c2-fixed-protocol-reevaluation.json`
- 最终验证：`docs/experiments/artifacts/2026-07-24-post-matrix-horizontal-update/final-verification.json`

## 7. 当前项目整体判断

主框架和五类真实闭环已经成立，28 题单模型单次正式基线也已得到。项目已从
“没有统一当前版本基线”进入“有可复现基线、能据实修失败”的阶段，但还不能写成
最终完成：P0 执行器终态问题需修复，随后需要同协议重复统计；Continuation
Enforce、Experience Guided、学习型 Ranker、多模型矩阵和 hidden 最终成绩仍未完成。

最终验证：52/52 聚焦测试、587/587 全量测试、`compileall`、关键 JSON 解析、
`git diff --check` 和本轮文件 Secret 模式扫描均 PASS。
