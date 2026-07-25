# 当前 HEAD 正式 28×1 矩阵冻结状态

日期：2026-07-24  
冻结 HEAD：`4a05763b593a527878a0056f64763126c58ee63b`  
模型：`deepseek-v4-pro`  
状态：`COMPLETE_WITH_MEASURED_FAILURES`  
矩阵运行：`28/28 Slot 唯一终态；23 DONE / 1 FAILED / 4 ERROR`

## 冻结结论

正式矩阵的 28 个公开 Task、单模型、单次 Repeat、预算、超时、最终验证策略、
横向组件权限和失败隔离/恢复策略均已冻结。Graph、Planner、BudgetLedger、Router
和 Vitis Runtime 与冻结 HEAD 完全一致。批处理终态 Provenance 验收器包含一项已
哈希冻结的兼容修正：在 `task_contract` 下，仅当公开 Task 明确要求 CoSim 时才把
CoSim 作为成功终态的必需阶段。

C1.1 完全等价 Gate 未通过后，原定的 Experience Shadow / Ranker Shadow 已按
Fail-Closed 规则改为运行时 off：

```text
Continuation = off
Experience = off
Ranker runtime = off
Final validation = task_contract
Token policy = fixed
Repeat = 1
```

## STRUCTURAL_FIX Anchor

不重跑。当前 HEAD 已存在同版本真实闭环：

- Task：`residual_stream_deadlock`；
- baseline：CSim PASS / Synth PASS / CoSim FAIL，连续 3 次稳定；
- Router：`STRUCTURAL_FIX`；
- 真实 DeepSeek + Vitis：候选 CSim/Synth/CoSim PASS；
- Fresh final：`full_internal_audit` 全部 PASS；
- Artifact integrity：85/85。

证据：
`docs/experiments/artifacts/2026-07-23-phase-b12-single-structural-anchor/single-anchor-result.json`

## 逐题 Token 预检

28/28 PASS，未调用 Provider 或 Vitis。

计算口径：

```text
PreparedPlannerCall.estimated_input_tokens
+ configured output cap 4096
+ safety headroom 128
```

| Mode | 题数 | 最大 required_with_headroom |
| --- | ---: | ---: |
| REPAIR | 8 | 11,924 |
| SYNTH_FIX | 6 | 12,674 |
| STRUCTURAL_FIX | 6 | 13,117 |
| OPTIMIZE | 8 | 10,530 |

每题冻结总 Token 上限均为 32,768。后续 Planner call 不预支假定额度，每轮必须重新
Prepare 并通过 BudgetLedger；不足时显式 fail-closed，而不是越过 Gate。

## Batch 语义

- 使用现有 `BatchBenchmarkRunner`；
- 串行 Vitis；
- 单个 Task 异常写入独立 terminal record，不中止后续 Task；
- JSONL 按 Slot 持久化；
- 重新执行时自动 Resume 已验证 Slot；
- Primary matrix 不 retry 失败 Slot，保证 28×1；
- 没有 batch 总时限；每题 runtime 上限 3600 秒；
- 完成 Gate 要求 28 个 Task 恰好各有一个明确终态。

## 真实启动预检与兼容修正

环境文件就绪后已完成 DeepSeek 只读连通性检查：HTTP 200，目标模型可见。随后
真实启动预检得到两条有效 Agent 结果：

- `task001_repair_syntax`：Agent `DONE`，1 次 LLM，1,558 tokens，CSim/Synth fresh PASS；
- `task002_repair_oob`：Agent `DONE`，1 次 LLM，1,517 tokens，CSim/Synth fresh PASS。

两条结果随后被旧 Batch 验收器误判为缺少 final CoSim。该判断与冻结的
`task_contract` 冲突，因为这两题公开合约不要求 CoSim，Agent 正确记录为
`NOT_RUN`。修正仅作用于 Batch 终态证据检查，不改变 Planner 输入、Graph 主路径、
预算、工具调用或模型配置；文件 SHA-256 已写入冻结清单。两次中止目录均保留，
正式矩阵随后已从干净目录完成，预检 Slot 未混入正式统计。

## 全量回归

- 修正前完整单元测试：585/585 PASS；
- Batch 聚焦回归：23/23 PASS；
- 加入 C1.1 等价测试后的完整单元测试：587/587 PASS；
- `compileall`：PASS；
- `git diff --check`：PASS；
- 本阶段新增文件 Secret 模式扫描：0 命中。

## 正式矩阵实测结果

正式输出目录：
`llm4hls_harness/experiments/formal_matrix_20260724_4a05763_deepseek_28x1`

完成 Gate：

```text
expected_slots = 28
terminal_slots = 28
missing_task_ids = []
non_unit_task_counts = []
retry_failures_used = false
```

结果为 23/28 端到端成功（82.14%），终态分布为 23 `DONE`、1 `FAILED`、
4 `ERROR`。五个未成功 Slot 均已保留明确终态及失败证据：

| Task | 期望 Mode | 终态 | 失败分类 |
| --- | --- | --- | --- |
| `v3d_fast_006` | REPAIR | FAILED | `MAX_TASK_REPAIR_ROUNDS` |
| `v3d_fast_016` | STRUCTURAL_FIX | ERROR | `TERMINAL_LAST_CANDIDATE_BINDING` |
| `v3d_fast_020` | STRUCTURAL_FIX | ERROR | `TERMINAL_LAST_CANDIDATE_BINDING` |
| `v3d_fast_021` | OPTIMIZE | ERROR | `INVALID_CANDIDATE_WORST_LATENCY` |
| `v3d_fast_022` | OPTIMIZE | ERROR | `INVALID_CANDIDATE_WORST_LATENCY` |

按 run-local BudgetLedger 汇总：39 次 LLM、85,768 tokens、83 次 CSim、
68 次 Synth、21 次 CoSim、775 credits。4 题获得性能提升，最大 8.7×，并真实触发
8× acceleration stop。

Router 在 24 个可比较 Slot 中 23 个与预期标签一致。`v3d_fast_018` 本轮 baseline
CoSim 实测 PASS，因此 Router 按事实进入 OPTIMIZE，而静态验收标签原期望
STRUCTURAL_FIX；其余 4 个 Slot 在形成可用 Router 终态前发生执行器错误。

矩阵结果机器审计：
`docs/experiments/artifacts/2026-07-24-post-matrix-horizontal-update/formal-matrix-result-audit.json`

## 矩阵后横向组件结论

- Experience：从正式矩阵导入 31 条新记录，与冻结的 103 条合并为 134 条；102 条
  可用于离线排名。C1 数据审计继续 `PASS_PROMOTABLE`，但 Guided 仍
  `NOT_ADMITTED`。
- Continuation：正式矩阵提供 11 个 follow-up candidate，其中 7 个可绑定终态；
  与旧数据合并后共 27 个 decision。固定协议复评仍为
  `INSUFFICIENT_EVIDENCE`，V2 仅 `OFFLINE_ONLY`，Enforce `DISABLED`。
- Strategy Ranker：使用原冻结阈值复评，LOTO 与 LO-family coverage 均为
  38.24%，有害重复推荐率均为 15.38%；结果 `NEGATIVE_RESULT`，Learned
  Ranker `TRAINING_NOT_READY`。
- 上述更新未调用新 LLM/Vitis，未修改主 Graph，也未在同一数据上调阈值。

## 证据

- 冻结配置：
  `docs/experiments/artifacts/2026-07-24-formal-matrix/formal-matrix-freeze.json`
- 逐题 Token 预检：
  `docs/experiments/artifacts/2026-07-24-formal-matrix/per-task-token-preflight.json`
- 冻结脚本：
  `docs/experiments/artifacts/2026-07-24-formal-matrix/freeze_formal_matrix.py`
- 可恢复 Runner：
  `docs/experiments/artifacts/2026-07-24-formal-matrix/run_formal_matrix.py`
- 启动前验证：
  `docs/experiments/artifacts/2026-07-24-formal-matrix/prelaunch-verification.json`
