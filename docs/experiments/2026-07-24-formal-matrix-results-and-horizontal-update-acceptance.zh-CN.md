# 2026-07-24 正式 28×1 与矩阵后横向组件验收报告

## 验收结论

```text
正式矩阵覆盖验收：ACCEPTED
正式矩阵效果验收：PARTIAL（23/28 E2E）
矩阵后数据更新：ACCEPTED_OFFLINE_ONLY
Continuation Enforce：NOT_ADMITTED
Experience Guided：NOT_ADMITTED
Strategy Ranker 晋升：REJECTED
项目最终提交就绪：NOT_YET
```

本次真正完成了当前冻结版本、DeepSeek、Vitis 2025.2、28 题、单次重复的统一真实
基线。28 个 Slot 均有唯一明确终态，因此矩阵执行工作已完成；但其中有 5 个实测
失败，不能把“矩阵完成”写成“28 题全部成功”。

## 一、协议一致性

正式运行使用冻结 HEAD
`4a05763b593a527878a0056f64763126c58ee63b`。C1.1 证明 Experience Shadow 会改变
Planner fingerprint/输入后，按 Fail-Closed Gate 将 Continuation、Experience、
Ranker 全部关闭。没有绕过 Gate。

唯一相对 HEAD 的 Runtime 差异是已哈希冻结的 Batch 终态兼容修正：在
`task_contract` 下只对公开合约 `requires_cosim=true` 的任务强制 final CoSim。
它不改变 Graph、Planner、Router、BudgetLedger 或 Vitis 调用。

## 二、28 题实测

| 验收项 | 结果 |
| --- | --- |
| 逐题 Token 预检 | 28/28 PASS |
| 唯一终态覆盖 | 28/28 PASS |
| Missing / duplicate Slot | 0 / 0 |
| Primary retry | 0 |
| E2E | 23/28，82.14% |
| 终态分布 | 23 DONE / 1 FAILED / 4 ERROR |
| Router 可比较结果 | 23/24 与期望一致 |
| hidden/reference/golden | 未访问 |
| Secret 写入 Artifact | 未发现 |

失败明细：

1. `v3d_fast_006`：两轮 REPAIR 提案均被 Patch Policy 拒绝，正常达到
   `MAX_TASK_REPAIR_ROUNDS`。
2. `v3d_fast_016` 与 `020`：执行流程产出候选，但终态没有绑定最后 Candidate，
   外层保留 `ERROR`。
3. `v3d_fast_021` 与 `022`：候选 worst latency 无效，执行器未降级成可比较失败
   终态，而是抛出 `ERROR`。
4. `v3d_fast_018` 本轮 baseline CoSim PASS，Router 因此进入 OPTIMIZE；这与
   静态期望 STRUCTURAL_FIX 不同，但符合本轮 baseline evidence。

实际开销以每个 run 的 BudgetLedger 为准：85,768 tokens、39 LLM、83 CSim、
68 Synth、21 CoSim、775 credits。外层 ERROR 行的零值不能用于预算汇总。

## 三、性能结果

4 题得到可验证加速：

| Task | 相对 baseline |
| --- | ---: |
| `v3d_fast_025` | 2.03× |
| `v3d_fast_026` | 8.70× |
| `v3d_fast_027` | 5.06× |
| `v3d_fast_028` | 2.17× |

`v3d_fast_026` 的 8.7× 结果真实触发 8× acceleration stop。`v3d_fast_023`、
`024` 完成但没有性能提升。

## 四、Experience 验收

从正式 run 中导入 31 条记录，安全排除了 24 个仅含确定性 executor marker、
无法形成标准 Agent 终态的目录。新旧记录按 Provenance 分池，103 条冻结记录没有
被覆盖或重写，最终共 134 条、102 条 ranking/retrieval eligible。

合并审计结果：

```text
all records valid = true
record IDs unique = true
source identities unique = true
only real LLM+Vitis evidence = true
all four modes present = true
forbidden artifact refs = 0
secret markers = 0
component = PASS_PROMOTABLE
guided = NOT_ADMITTED
```

因此数据可以继续支持离线实验，但不能注入 Planner。

## 五、Continuation 验收

回放严格先构造 decision-time 输入，再读取未来终态标签；未发现 future leakage。
正式矩阵新增 7 个可绑定 decision，与旧 20 个合并为 27 个。

现有样本没有 SYNTH_FIX follow-up，也没有 essential STRUCTURAL_FIX outcome。
V1 会错误阻断 6/10 有益样本；V2 虽保留全部有益样本，但也放过全部 17 个有害
样本。因此没有足够证据支持 Enforce：

```text
status = INSUFFICIENT_EVIDENCE
V1 authority = SHADOW
V2 authority = OFFLINE_ONLY
Enforce = DISABLED
main Graph modified = false
```

## 六、Strategy Ranker 验收

C2 使用冻结代码和原候选阈值，在合并后的 102 条 eligible 数据上重新评估，没有
在同一数据上调参。

LOTO 与 leave-one-task-family-out 都是 39/102 推荐覆盖（38.24%），有害重复推荐
6/39（15.38%），Group leakage 0。固定阈值要求 coverage ≥40%、harmful ≤5%；
此外 STRUCTURAL_FIX 仅 9 条，低于每 Mode 10 条要求。

所以：

```text
component = NEGATIVE_RESULT
authority = SHADOW
learned ranker = TRAINING_NOT_READY
runtime = off
```

## 七、需要继续改进的工作

优先顺序建议保持：

1. P0 修复最后 Candidate 终态绑定和无效 latency 的 fail-closed 终态表达；
2. P1 处理 `v3d_fast_006` 的 Patch Policy 连续拒绝；
3. 用本轮 baseline evidence 生成 Router 期望，避免 `v3d_fast_018` 类标签漂移；
4. 修复后按同一冻结协议做当前版本重复统计，不直接用本轮失败数据调 C2 阈值；
5. 补足各 Mode 独立的 Continuation/Experience holdout，尤其 SYNTH_FIX 和
   STRUCTURAL_FIX；
6. 通过固定外部协议后，才考虑 Experience Guided、Ranker 训练/准入；
7. 再做多模型矩阵、hidden 最终成绩以及提交材料冻结。

## 八、机器证据

- 正式覆盖：
  `llm4hls_harness/experiments/formal_matrix_20260724_4a05763_deepseek_28x1/terminal_coverage.json`
- 正式结果：
  `docs/experiments/artifacts/2026-07-24-post-matrix-horizontal-update/formal-matrix-result-audit.json`
- Experience：
  `docs/experiments/artifacts/2026-07-24-post-matrix-horizontal-update/combined-experience-manifest.json`
- C1：
  `docs/experiments/artifacts/2026-07-24-post-matrix-horizontal-update/c1-combined-audit/c1-audit-summary.json`
- Continuation：
  `docs/experiments/artifacts/2026-07-24-post-matrix-horizontal-update/continuation-fixed-protocol-reevaluation.json`
- Ranker：
  `docs/experiments/artifacts/2026-07-24-post-matrix-horizontal-update/c2-fixed-protocol-reevaluation.json`
- 最终验证：
  `docs/experiments/artifacts/2026-07-24-post-matrix-horizontal-update/final-verification.json`

矩阵后所有横向更新均为离线处理，新 LLM、Token、CSim、Synth、CoSim 和 Tool
Credits 均为 0。

最终回归为 52/52 聚焦测试、587/587 全量测试；`compileall`、关键 JSON 解析、
`git diff --check`、本轮新增/修改文件的长格式 Token 和 Authorization Bearer
模式扫描均通过。
