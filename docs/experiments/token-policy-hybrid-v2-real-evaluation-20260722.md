# V3-E Token Policy Hybrid V2 真实复验报告

日期：2026-07-22

分支：`feat/v3e-token-policy-hybrid-v2`

模型：`deepseek-v4-pro`（OpenAI-compatible，provider 不支持 seed）

工具：AMD Vitis HLS 2025.2

范围：12 道公开 train/dev 任务，A/D 两组，每题每组 2 次，共 48 个真实 run

## 1. 结论先行

`D_HYBRID` **没有通过工程准入，不进入 28 题阶段**。

它解决了 terminal summary 的错误退出，并建立了可配置的 stable cap、极简 Token Prompt、第二轮调用 Gate 和可审计的 A/D 实验链路；但真实复验表明，它没有降低总 Token：

| 指标 | A_FIXED | D_HYBRID | D 相对 A |
|---|---:|---:|---:|
| run 数 | 24 | 24 | 0 |
| final success | 22/24 = 91.67% | 21/24 = 87.50% | -4.17 pp |
| 平均总 Token | 2044.04 | 2714.83 | **+32.82%** |
| 每个成功 run 的 Token | 2229.86 | 3102.67 | **+39.14%** |
| 平均输入 Token | 1565.25 | 2056.71 | +31.40% |
| 平均输出 Token | 478.79 | 658.13 | +37.46% |
| 平均 Planner 调用 | 1.00 | 1.25 | +25.00% |
| Patch invalid rate | 0% | 4.17% | +4.17 pp |
| truncation rate | 0% | 0% | 0 |
| budget compliance | 100% | 100% | 0 |
| 平均 Credits | 41.75 | 42.75 | +2.40% |
| 平均 wall time | 85.99 s | 91.08 s | +5.92% |

主要问题不是 stable cap，而是 `OPTIMIZE` 中所有 6 个 D run 都执行了第二次 Planner 调用；只有 2/6 的第二次调用产生了晋升或最终改善。额外调用把输入上下文再次发送给模型，使 Token 成本明显上升。

本次没有删除或美化负结果。48 个 run 的记录、失败、Planner 请求、工具结果和 final receipts 全部保留在本地实验目录。

## 2. Terminal summary Bug 修复

问题表现：部分 `REPAIR`、`SYNTH_FIX`、`STRUCTURAL_FIX` run 已经完成 fresh final CSim/Synth/CoSim PASS，但 Vitis 没有提供可比较的 worst latency，CLI 在生成 terminal summary 时抛出：

```text
baseline/candidate worst latency is invalid
```

修复位置：`llm4hls_harness/llm4hls_agent/v3_prototype.py`

修复规则：

- `OPTIMIZE` 仍使用严格 latency gate，缺失 latency 不能被当作性能提升；
- correctness 模式使用 optional latency summary；
- fresh final 的 CSim/Synth/CoSim PASS 是最终正确性事实；
- latency 不可用时 acceleration/local score 为 `UNKNOWN/null`，不再把已通过的 final 改成失败；
- 原始 Vitis result、Candidate registry 和 artifact 不修改；
- 旧 run 仍可 reconciliation，新 run 不再依赖它才能得到正确 terminal status。

回归测试增加了 `final PASS + latency UNKNOWN` 场景。新 48-run 实验的 reconciliation 数为 0，说明修复作用于新执行路径，而不是事后改结果。

## 3. 已有 72 个真实 run 的离线分析

离线分析输入为上一轮 DeepSeek + Vitis 的 72 条 reconciled records；本步骤调用外部模型 0 次、调用 Vitis 0 次。

| Mode | Runs | Final success | 每次成功调用 output P95 | Planner calls 均值 | 第二次调用率 | 第二次调用改善率 |
|---|---:|---:|---:|---:|---:|---:|
| REPAIR | 18 | 100% | 459.15 | 1.00 | 0% | N/A |
| SYNTH_FIX | 18 | 100% | 829.25 | 1.00 | 0% | N/A |
| STRUCTURAL_FIX | 18 | 61.11% | 462.00 | 1.22 | 22.22% | 0/4 = 0% |
| OPTIMIZE | 18 | 100% | 796.65 | 1.33 | 33.33% | 3/6 = 50% |

完整 P50/P75/P90/P95/max、输入 Token、Patch invalid 和 acceleration 分布见：

- `llm4hls_harness/experiments/token_policy_hybrid_v2/historical-72/token_policy_historical_distribution.json`
- `llm4hls_harness/experiments/token_policy_hybrid_v2/historical-72/token_policy_historical_distribution.csv`
- `llm4hls_harness/experiments/token_policy_hybrid_v2/historical-72/token_policy_historical_analysis.md`

## 4. Stable cap 的来源

规则为：真实成功 Planner call 的 output Token P95，加 15% safety margin，再向上取整到 16 Token。

| Mode | 成功 call P95 | P95 × 1.15 | Stable cap |
|---|---:|---:|---:|
| REPAIR | 459.15 | 527.02 | 544 |
| SYNTH_FIX | 829.25 | 953.64 | 960 |
| STRUCTURAL_FIX | 462.00 | 531.30 | 544 |
| OPTIMIZE | 796.65 | 916.15 | 928 |

这些值位于版本化配置 `llm4hls_harness/llm4hls_agent/config/token_policy_hybrid_v2.json`，不是散落在代码里的常量，也不是“最佳参数”的结论。

## 5. Hybrid Token Policy 实现

`TokenBudgetPolicy(profile="hybrid")` 的行为：

- LOW：使用 mode stable cap，不主动缩小；Prompt 不显示 TokenEnvelope；
- MEDIUM：继续使用 stable cap，只加入一行极简提示；
- HIGH：按剩余预算缩小 effective cap，压缩低优先级上下文，最多一个 strategy；
- CRITICAL：`planner_call_allowed=false`，沿现有 final/fallback 条件边退出；
- BudgetLedger 仍是硬约束，模型不能改变预算、工具权限、Candidate 晋升或 final 选择。

没有增加 Agent 或 LangGraph 节点，也没有修改 ToolServer、Candidate correctness、fresh final 或 Vitis credit 规则。

## 6. Prompt Token 减少

旧 dynamic TokenEnvelope 区块在代表性 LOW 配置下为 577 个字符；当前环境没有 DeepSeek 官方 tokenizer，因此确定性 fallback 只能报告 577 code-point 上界，不能把它冒充真实 provider token。

Hybrid 的新增内容为：

- LOW：0 字符；
- MEDIUM/HIGH：一行，代表性 HIGH 为 127 字符、15 个空格分词片段；
- 完整 Envelope 仍写入 artifact/report，不进入 Planner Prompt。

与上一轮 C_DYNAMIC_VISIBLE 相比，固定约 137 model-token 的预算说明块被移除。与本轮 A_FIXED 相比，单次调用的实际 input Token 通常相同，因为 A_FIXED 本身也不展示完整 dynamic Envelope；因此本轮 D 的端到端 Token 增减主要由 Planner 调用次数决定，而不是这行提示。

## 7. 第二轮 Planner Call Gate

Gate 位于 Planner adapter 内，复用现有条件边，不增加 Graph 节点。每轮决策持久化为：

```text
planner/call_gates/round_NNN.json
```

本次 D_HYBRID 的实际触发情况：

- 24 个首轮调用全部允许；
- 6 个 run 真实执行第二次 Planner 调用，全部位于 OPTIMIZE；
- 其中 2/6 被记录为第二轮产生晋升/改善；
- 6 个 run 出现后续 BLOCK：3 个 STRUCTURAL_FIX 在第二轮前被阻止，3 个 OPTIMIZE 在更后续调用前被阻止；
- 主要 reason codes：`NO_NEW_STRUCTURED_EVIDENCE` 4 次、`BEST_FAILURE_OR_BOTTLENECK_UNCHANGED` 3 次、`ESTIMATED_INPUT_EXCEEDS_AVAILABLE_TOKEN_BUDGET` 3 次、`DUPLICATE_STRATEGY_BUNDLE` 1 次。

真实数据说明当前 Gate 两边都不理想：

- 对 STRUCTURAL_FIX，它可能在首轮修复失败后过早停止；
- 对 OPTIMIZE，只要产生了新 Synth evidence，就容易放行第二轮，即使新增调用最终没有收益。

## 8. A_FIXED vs D_HYBRID 实验控制

固定条件：同一 provider/model、temperature 0、top_p 1、12000 run-token limit、100 credit、最多 4 轮、相同 Vitis 2025.2、相同任务快照、相同 fast-experiment validation 和 fresh final。

差异仅为：

- A：fixed policy、旧摘要、Experience off；
- D：hybrid policy、stable cap、极简提示、第二轮 Gate、Experience off。

provider 不支持 seed，已明确记录 `provider_seed_supported=false`。执行顺序使用 `BALANCED_ALTERNATING_AD_V1`，每个 run 都是新目录；Planner response、Candidate 和 final 均未跨组复用。

## 9. 12 题 task-level 结果

表中 Token 和 Planner calls 为每题两个 repeat 的均值；acceleration 为两个 repeat 的中位数。

| Task | Mode | A success | D success | A Token | D Token | A/D calls | A accel | D accel |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| v3d_fast_001 | REPAIR | 2/2 | 2/2 | 1548.0 | 1537.5 | 1.0 / 1.0 | N/A | N/A |
| v3d_fast_004 | REPAIR | 2/2 | 2/2 | 1743.0 | 1776.5 | 1.0 / 1.0 | N/A | N/A |
| v3d_fast_005 | REPAIR | 2/2 | 2/2 | 1476.5 | 1496.0 | 1.0 / 1.0 | N/A | N/A |
| v3d_fast_009 | SYNTH_FIX | 2/2 | 2/2 | 1755.0 | 1713.0 | 1.0 / 1.0 | N/A | N/A |
| v3d_fast_012 | SYNTH_FIX | 2/2 | 2/2 | 2406.0 | 2499.0 | 1.0 / 1.0 | N/A | N/A |
| v3d_fast_013 | SYNTH_FIX | 2/2 | 2/2 | 2046.5 | 2048.5 | 1.0 / 1.0 | N/A | N/A |
| v3d_fast_015 | STRUCTURAL_FIX | 2/2 | 1/2 | 2204.0 | 2204.5 | 1.0 / 1.0 | 1.00 | 1.00 |
| v3d_fast_016 | STRUCTURAL_FIX | 0/2 | 0/2 | 2153.5 | 2171.5 | 1.0 / 1.0 | N/A | N/A |
| v3d_fast_017 | STRUCTURAL_FIX | 2/2 | 2/2 | 2299.0 | 2297.5 | 1.0 / 1.0 | 1.00 | 1.00 |
| v3d_fast_021 | OPTIMIZE | 2/2 | 2/2 | 2037.5 | 4353.5 | 1.0 / 2.0 | 1.625 | 1.000 |
| v3d_fast_023 | OPTIMIZE | 2/2 | 2/2 | 2394.5 | 5251.5 | 1.0 / 2.0 | 1.811 | 1.405 |
| v3d_fast_025 | OPTIMIZE | 2/2 | 2/2 | 2465.0 | 5229.0 | 1.0 / 2.0 | 1.014 | 2.029 |

## 10. Paired comparison

以 task 为配对单位，先对同一 task/condition 的两个 repeat 取均值，再进行 paired bootstrap：

| Metric | A task mean | D task mean | D-A | Paired bootstrap 95% CI | 解释 |
|---|---:|---:|---:|---:|---|
| Final success | 91.67% | 87.50% | -4.17 pp | [-12.50 pp, 0] | 未越过 -5 pp 点估计门槛，但样本小 |
| Total Token | 2044.04 | 2714.83 | +670.79 | [25.58, 1367.01] | D 明显更高 |
| Input Token | 1565.25 | 2056.71 | +491.46 | [0, 996.17] | 额外调用重复发送上下文 |
| Output Token | 478.79 | 658.13 | +179.33 | [20.50, 363.50] | 额外调用增加输出 |
| Planner calls | 1.00 | 1.25 | +0.25 | [0, 0.50] | D 未减少调用 |
| Acceleration | 1.3626 | 1.3585 | -0.0041 | [-0.5152, 0.6592] | 仅 4 个有双方可比值的 task，区间很宽 |
| Credits | 41.75 | 42.75 | +1.00 | [-4.00, 7.88] | 无节省证据 |
| Wall time | 85.99 s | 91.08 s | +5.09 s | [-5.60, 19.93] | 无加速证据 |

只有 12 个配对 task，小于预设的 20 题 inferential threshold；以上 bootstrap 仅作描述性不确定性分析，不宣称统计显著性。

## 11. Final success 与失败分布

- A_FIXED：22/24 成功；
- D_HYBRID：21/24 成功；
- 5 个失败全部来自 STRUCTURAL_FIX；
- `v3d_fast_016` 两组两次都失败；
- `v3d_fast_015` 的 D 第一次失败、第二次成功，而 A 两次均成功；
- D 的一次 Patch invalid 位于 `v3d_fast_016` repeat 2；
- 没有 provider transport error、Vitis license error 或 output truncation。

因此 D 的成功率下降没有超过预设 5 pp 点估计门槛，但它也没有改善正确性，且 Patch invalid=0 的硬要求未满足。

## 12. Acceleration 质量

工程准入只用 OPTIMIZE 的 median/geometric mean 判断优化质量：

| OPTIMIZE 指标 | A_FIXED | D_HYBRID | 变化 |
|---|---:|---:|---:|
| Median acceleration | 1.4199 | 1.4054 | -1.02% |
| Geometric mean acceleration | 1.4019 | 1.3976 | -0.31% |

两者都在允许的 5% 退化范围内，因此质量 gate 本身通过。但 task 结果高度混合：D 在 025 更好，在 021 明显更差，在 023 的均值/中位数更差；不能声称 Hybrid 提升了比赛分数。

## 13. 工程准入逐项判定

| 条件 | 结果 |
|---|---|
| final success 下降不超过 5 pp | PASS（点估计 -4.17 pp） |
| average total tokens 不增加 | **FAIL（+32.82%）** |
| average total tokens 至少降低 10% | **FAIL** |
| tokens per success 不增加 | **FAIL（+39.14%）** |
| Planner calls 不增加 | **FAIL（+25%）** |
| Patch invalid = 0 | **FAIL（1/24）** |
| truncation = 0 | PASS |
| OPTIMIZE median acceleration 退化不超过 5% | PASS |
| OPTIMIZE geometric mean 退化不超过 5% | PASS |
| budget compliance = 100% | PASS |

总判定：`NOT_PASSED`。

## 14. 是否进入 28 题

**否。** 按预先写死的准入规则，不能因为结果不理想而降低门槛。当前结果也不支持成功率、Token 效率或比赛分数提升声明。

可使用的准确表述是：

> Hybrid Token Policy engineering implementation and deterministic validation passed. Controlled 48-run real-provider evaluation did not pass engineering admission: average total tokens increased by 32.82%, while final correctness and optimization quality showed no reliable improvement.

## 15. 下一步唯一优先项

不要继续调 stable cap，也不要进入 28 题。下一步应只重做“后续 Planner 调用的价值判定”：

1. OPTIMIZE 在 Candidate 已经严格提升后默认停止，除非剩余预算和预期增益明确支持继续；
2. no-improvement 后只有出现新的、可操作的 memory/scheduling bottleneck，且下一策略与已失败策略实质不同，才允许第二次调用；
3. STRUCTURAL_FIX 的 Candidate CoSim 新失败应被视为可修复的新 evidence；只要 credit 与 fresh final reserve 足够，不应仅因旧 bottleneck 未变化而阻止一次针对性修复；
4. 在离线 replay 上先验证“会阻止 4 个无收益 OPTIMIZE follow-up，同时不阻止有收益的 2 个 follow-up”，再考虑新的小规模真实复验。

## 16. 实现与实验文件

实现：

- `llm4hls_harness/llm4hls_agent/budget.py`
- `llm4hls_harness/llm4hls_agent/openai_provider.py`
- `llm4hls_harness/llm4hls_agent/v3_openai_planner.py`
- `llm4hls_harness/llm4hls_agent/v3_prototype.py`
- `llm4hls_harness/llm4hls_agent/v3_prototype_cli.py`
- `llm4hls_harness/llm4hls_agent/token_policy_historical_analysis.py`
- `llm4hls_harness/llm4hls_agent/token_policy_hybrid_runner.py`
- `llm4hls_harness/llm4hls_agent/token_policy_hybrid_aggregate.py`
- `llm4hls_harness/llm4hls_agent/config/token_policy_hybrid_v2.json`

测试：

- `llm4hls_harness/tests/test_token_policy_hybrid.py`
- `llm4hls_harness/tests/test_token_policy_hybrid_matrix.py`
- `llm4hls_harness/tests/test_v3_task_aware_smoke.py`

实验入口与结果：

- `llm4hls_harness/experiments/token_policy_hybrid_v2/token_policy_hybrid_matrix.json`
- `llm4hls_harness/experiments/token_policy_hybrid_v2/pilot-real-deepseek-v4-pro-20260722-01/manifest.json`
- `llm4hls_harness/experiments/token_policy_hybrid_v2/pilot-real-deepseek-v4-pro-20260722-01/token_policy_hybrid_results.jsonl`
- `llm4hls_harness/experiments/token_policy_hybrid_v2/pilot-real-deepseek-v4-pro-20260722-01/token_policy_hybrid_summary.json`
- `llm4hls_harness/experiments/token_policy_hybrid_v2/pilot-real-deepseek-v4-pro-20260722-01/token_policy_hybrid_summary.csv`
- `llm4hls_harness/experiments/token_policy_hybrid_v2/pilot-real-deepseek-v4-pro-20260722-01/token_policy_hybrid_report.md`

本地 `runs/` 保存约 3.9 GiB 原始 Vitis/Planner artifacts，按仓库规则忽略，不纳入 Git；顶层 manifest、schedule、records、summary 和本报告纳入版本控制。

## 17. 测试与提交

新增 Hybrid focused tests 覆盖：

- LOW/MEDIUM/HIGH/CRITICAL allocation；
- LOW 不注入 Envelope；
- MEDIUM/HIGH 极简提示；
- 第二轮有/无新 evidence 的 ALLOW/BLOCK；
- Gate artifact；
- Hybrid Planner 到 fresh final 的完整确定性闭环；
- 48-run 平衡调度、A/D 条件隔离、无复用、无 hidden；
- task-level final success 配对与工程准入；
- final PASS + latency UNKNOWN terminal summary。

最终全套回归：`523 tests in 36.635s, OK`。Git commit hash 由包含本报告的提交元数据和最终交付信息给出。
