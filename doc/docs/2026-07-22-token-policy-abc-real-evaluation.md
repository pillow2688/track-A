# Token Policy Dynamic A/B/C 真实评测报告

日期：2026-07-22

分支：`feat/v3e-token-policy-abc-eval`

实验：`token-policy-abc-real-pilot-v1`

## 1. 结论先行

本轮已完成 12 道公开 train/dev 任务、3 个条件、每题每组 2 次，共 72 个真实 DeepSeek + Vitis 2025.2 run。没有使用 hidden-like 任务，没有启用 Experience Guidance，没有复用跨组 Planner response、Candidate 或 final 结果。

最终结论是：**C_DYNAMIC_VISIBLE 没有通过工程准入，不能宣称当前 Token Policy 提升了 Token 效率或比赛分数。**

- A_FIXED：22/24 成功，91.67%；平均 2,047.75 Token。
- B_DYNAMIC_HARD：22/24 成功，91.67%；平均 2,634.04 Token。
- C_DYNAMIC_VISIBLE：21/24 成功，87.50%；平均 2,571.38 Token。
- C 相对 A 的成功率下降 4.17 个百分点，在允许的 5 个百分点内。
- 但 C 相对 A 的平均 Token **增加 25.57%**，不是目标中的降低至少 10%。
- C 的每成功 run Token **增加 31.55%**。
- C 的 OPTIMIZE 平均 acceleration 为 1.353，A 为 1.697，低约 20.2%，超过 5% 质量退化容忍度。
- 三组预算合规率均为 100%，输出截断率均为 0%。
- C 出现 1 个 Patch invalid，A/B 都为 0。

因此没有启动 28 题正式阶段。继续扩大样本只会在当前已失败的工程配置上消耗更多 Provider 与 Vitis 资源。

> Controlled real-provider A/B/C Pilot runs completed, but engineering admission was not met. No claim is made about token-efficiency or competition-score improvement.

## 2. 实验条件

| 项目 | 固定值 |
|---|---|
| Provider | OpenAI-compatible DeepSeek |
| Model | `deepseek-v4-pro` |
| Provider model fingerprint | `deepseek-v4-pro\|fp_9954b31ca7_prod0820_fp8_kvcache_20260402` |
| Temperature / top_p | 0.0 / 1.0 |
| Seed | Provider 不支持，记录为 `provider_seed_supported=false` |
| Run Token limit | 12,000 |
| Credit limit | 100 |
| Max Planner rounds | 4 |
| Max no-improvement rounds | 2 |
| Final reserve | 25 Credit |
| Validation profile | `fast-experiment`，fresh final 规则保持不变 |
| Toolchain | AMD Vitis 2025.2 |
| Experience | `off` |
| Baseline/Candidate/final 跨组复用 | 全部禁用 |
| 执行顺序 | 按 task/repeat 轮换 A/B/C，降低时间顺序偏差 |

三个条件只有 Token Policy 区域不同：

| Condition | Provider 输出上限 | Planner 是否看见 TokenEnvelope | 预算 Prompt |
|---|---|---|---|
| A_FIXED | 固定 4,096 | 否 | 旧摘要 |
| B_DYNAMIC_HARD | 动态 effective cap | 否 | 与 A 相同的旧摘要 |
| C_DYNAMIC_VISIBLE | 动态 effective cap | 是 | Envelope + pressure 软提示 |

实际平均 effective max output：A=4,096，B=1,939.87，C=1,861.96。动态上限确实传给了 Provider，并非只写在报告里。

## 3. 任务与运行规模

Pilot 使用 12 道公开任务：

- REPAIR：`v3d_fast_001`、`v3d_fast_004`、`v3d_fast_005`
- SYNTH_FIX：`v3d_fast_009`、`v3d_fast_012`、`v3d_fast_013`
- STRUCTURAL_FIX：`v3d_fast_015`、`v3d_fast_016`、`v3d_fast_017`
- OPTIMIZE：`v3d_fast_021`、`v3d_fast_023`、`v3d_fast_025`

共计划 72 个 run，实际完成 72 个；65 个有 fresh final CSim/Synth/CoSim PASS，7 个失败。所有 72 份 artifact receipt 哈希校验通过。

累计开销：

| 指标 | 数值 |
|---|---:|
| 实际总 Token | 174,076 |
| 实际总 Credit | 3,134 |
| Planner calls | 82 |
| CSim / Synth / CoSim | 218 / 179 / 110 |
| 累计 run wall time | 6,457.15 秒（约 107.6 分钟） |
| 输出截断 / JSON incomplete / Patch incomplete | 0 / 0 / 0 |
| Patch invalid | 1 |

## 4. A/B/C 总体指标

| 指标 | A_FIXED | B_DYNAMIC_HARD | C_DYNAMIC_VISIBLE |
|---|---:|---:|---:|
| Runs | 24 | 24 | 24 |
| Final success | 91.67% | 91.67% | 87.50% |
| Avg total Token | 2,047.75 | 2,634.04 | 2,571.38 |
| Token / successful run | 2,233.91 | 2,873.50 | 2,938.71 |
| Avg input Token | 1,565.25 | 2,012.63 | 2,026.33 |
| Avg output Token | 482.50 | 621.42 | 545.04 |
| Avg Planner calls | 1.00 | 1.25 | 1.17 |
| Patch valid rate | 100% | 100% | 97.92% |
| Candidate promotion rate | 91.67% | 85.42% | 77.08% |
| Budget compliance | 100% | 100% | 100% |
| Output truncation | 0% | 0% | 0% |
| Avg Credit | 42.58 | 45.17 | 42.83 |
| Avg wall time | 86.45 s | 94.41 s | 88.20 s |

每组总开销：

| Condition | Total Token | Planner calls | Credits | CSim / Synth / CoSim |
|---|---:|---:|---:|---:|
| A_FIXED | 49,146 | 24 | 1,022 | 70 / 58 / 36 |
| B_DYNAMIC_HARD | 63,217 | 30 | 1,084 | 76 / 62 / 38 |
| C_DYNAMIC_VISIBLE | 61,713 | 28 | 1,028 | 72 / 59 / 36 |

动态 cap 没有减少总 Token 的主要原因不是截断，而是 cap 对当前输出并不紧：A 的平均 cap 利用率只有约 10.1%，B/C 约 22.6%/21.1%。B/C 又触发了更多 Planner 轮次，额外输入上下文和调用次数抵消并超过了单次上限收益。

## 5. 按 mode 的结果

| Mode | A success / Token | B success / Token | C success / Token |
|---|---:|---:|---:|
| REPAIR | 100% / 1,602.00 | 100% / 1,596.17 | 100% / 1,717.83 |
| SYNTH_FIX | 100% / 2,066.50 | 100% / 2,078.50 | 100% / 2,223.17 |
| STRUCTURAL_FIX | 66.67% / 2,212.17 | 66.67% / 2,934.50 | 50.00% / 3,108.17 |
| OPTIMIZE | 100% / 2,310.33 | 100% / 3,927.00 | 100% / 3,236.33 |

OPTIMIZE 的平均 acceleration：

- A：1.697×
- B：7.535×
- C：1.353×

B 的平均值受到 `v3d_fast_023` repeat 2 的 67→2（33.5×）强结果显著影响，因此 12 题 Pilot 只能描述，不能据此宣称稳定质量提升。C 相对 A 的平均 acceleration 低约 20.2%，工程准入按保守规则判定质量退化。

## 6. Task-level paired comparisons

每个比较使用 task-level 配对均值和 paired bootstrap 95% CI。Pilot 只有 12 个 paired tasks，低于预设 20，因此全部标记为 `DESCRIPTIVE_ONLY`。

| 比较 | Avg total Token 差值 | 相对差值 | Bootstrap 95% CI | Final success 差值 |
|---|---:|---:|---:|---:|
| A→B | +586.29 | +28.63% | [+3.17, +1,219.46] | 0.00 pp |
| B→C | -62.67 | -2.38% | [-549.25, +221.04] | -4.17 pp |
| A→C | +523.63 | +25.57% | [+120.17, +1,111.67] | -4.17 pp |

A→C 的 task-level tokens per successful task 差值为 +570.68（+28.00%），95% CI 为 [+118.05, +1,233.14]。配对 binary success 只有一个 discordant run slot，McNemar 双侧值为 1.0；这不代表两者等价，只代表 Pilot 对成功率差异没有足够统计能力。

## 7. 失败与异常对账

最终 7 个失败 run：

- `v3d_fast_016`：A/B/C 两次 repeat 共 6 个都失败，主要是 Candidate CoSim 仍失败或 no-improvement。
- `v3d_fast_017`：C repeat 2 的 Candidate CoSim 失败，最终没有可提交 Candidate。

另外有 12 个 run 在 fresh final 已全部 PASS 后，CLI 因 `baseline/candidate worst latency is invalid` 抛出 terminal summary 异常。它们不是功能失败。聚合器根据以下不可变持久化证据恢复了 final success 与成本：

- `budget_state.json` / `budget_ledger.jsonl`
- `candidate_registry.json` 中 `FINAL_VERIFIED`
- fresh final action result 的 `cached=false`、`validation_scope=final` 和 PASS

总共有 13 个 run 做了 durable-artifact reconciliation，其中 12 个恢复为 final success，1 个仍是真实失败。原始错误、原始 record 和所有 action artifact 均保留，没有把失败日志删除或伪造成正常 CLI 退出。

## 8. 工程准入

| 准入条件 | 结果 |
|---|---|
| C success 下降不超过 5 pp | PASS（-4.17 pp） |
| Avg total Token 降低至少 10% | FAIL（反而 +25.57%） |
| Token / success 降低至少 10% | FAIL（反而 +31.55%） |
| Budget compliance=100% | PASS |
| Truncation 不增加 | PASS（均为 0） |
| JSON/Patch invalid 不增加 | FAIL（C 有 1 个 Patch invalid） |
| Latency/quality 不明显退化 | FAIL（OPTIMIZE acceleration 低约 20.2%） |

最终：`NOT_PASSED`。

- 不能支持统计性结论：只有 12 个 paired tasks。
- 不能支持比赛分数提升结论：local score proxy 缺失，且质量准入未通过。
- 不能用“分数没涨但 Token 降了”的表述：本轮 Token 实际上增加了。

## 9. 下一轮参数调整建议

优先级从高到低：

1. **压缩 visible TokenEnvelope。** C 每次调用比 A/B 多约 137 个固定输入 Token；只保留 `remaining/effective/pressure` 三个关键数字，把完整分配明细留在 artifact，不放 Prompt。
2. **调整 prompt compression order。** 先压缩历史与重复 evidence，再注入极短 Envelope；不能让预算提示本身增加大量输入。
3. **降低 mode output cap，但保持 minimum viable output。** 当前无截断且实际输出远低于 cap，可试 REPAIR 1,000、SYNTH_FIX 1,300、STRUCTURAL_FIX 1,600、OPTIMIZE 1,800；minimum viable 暂不降低。
4. **提高第二轮 pressure threshold 的约束强度。** 在不改主 Graph 的前提下，明确要求单一 hypothesis、最多一个 strategy bundle、禁止复述 evidence，减少 B/C 多轮时的冗余输出。
5. **重新校准 future reserve。** 当前后续轮 cap 有时恢复到 mode cap，没能抑制总调用成本；应让 reserve 与剩余可用轮数绑定，但不能牺牲 OPTIMIZE 的 Candidate 质量。
6. **先做 12 题小型复验，不直接跑 28 题。** 只有 C 相对 A 至少不增加 Token、无 invalid、且 acceleration 非退化后，再进入正式 28 题矩阵。

## 10. 产物位置

- Matrix 配置：`llm4hls_harness/experiments/token_policy_abc/token_policy_abc_matrix.json`
- Pilot manifest：`llm4hls_harness/experiments/token_policy_abc/pilot-real-deepseek-v4-pro-20260722-01/manifest.json`
- 原始 run records：`token_policy_abc_results.jsonl`
- 对账后 records：`token_policy_abc_reconciled_results.jsonl`
- 完整统计：`token_policy_abc_summary.json`
- 表格摘要：`token_policy_abc_summary.csv`
- 自动报告：`token_policy_abc_report.md`
- 72 个完整 run artifact：Pilot 目录下的 `runs/`（体积约 6.1 GiB，受 `.gitignore` 保护，不提交 Git）

## 11. 测试状态

实现前完整回归：511 tests PASS。

新增 reconciliation 聚焦测试：5 tests PASS。

最终完整回归：512 tests PASS。最终 commit hash 由提交后的交付信息记录。
