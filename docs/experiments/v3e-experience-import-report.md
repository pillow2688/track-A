# V3-E 历史经验导入报告

更新时间：2026-07-21

## 导入结果

| 指标 | 数量 |
|---|---:|
| 扫描到的历史来源 | 335 |
| 导入的真实 LLM/Vitis terminal runs | 20 |
| 经验记录总数 | 26 |
| 已物化真实 Candidates | 18 |
| 默认可检索/排序记录 | 18 |
| Patch policy rejection（仅审计） | 7 |
| 重复策略 proposal（仅审计） | 1 |
| 未知或无界 latency 指标 | 2 |

四种 mode 的记录分布：OPTIMIZE 11、REPAIR 7、STRUCTURAL_FIX 4、SYNTH_FIX 4。默认学习集只包含 18 个真实 Candidate，不包含尚未通过 Patch policy、没有调用过 Vitis 的 proposal。

## 结果分布

全部 26 条审计记录中：

- `FINAL_PASS`：15
- `PROMOTED`：1
- `CANDIDATE_REJECTED`：2
- `PATCH_POLICY_REJECTED`：7
- `DUPLICATE_STRATEGY_BUNDLE`：1

18 条默认学习记录的验证覆盖：CSim 18/18 PASS，Synth 18/18 PASS，CoSim 15 次 PASS，完整 Final 15 次 PASS；OPTIMIZE 中确认 4 个严格 latency 改善 Candidate。

这些数字是历史经验数据的组成，不是新 12 题 pilot 的成功率。

## 被排除的数据

| 原因 | 来源数 |
|---|---:|
| deterministic executor，不是真实 Agent 经验 | 153 |
| oracle/golden，不可伪装成 Agent 成功 | 151 |
| scripted/demo fixture | 11 |

## 数据质量处理

- Vitis 的未知/无界 latency sentinel 转为 `null`，不参与平均值；真实 0-cycle 值仍保留。
- final success 要求 fresh CSim、Synth、CoSim 三项全部 PASS。
- 最新 trajectory revision 先覆盖旧 revision，再决定能否进入排名；新 revision 可以撤销旧正例。
- task identity 不参与算法族分类；算法族由公开源码的固定规则提取。
- 写入使用 append-only JSONL、文件锁、内容哈希和幂等 record ID。

机器产物位于 `llm4hls_harness/experiments/v3e/`：

- `experience_store.jsonl`
- `experience_import_report.json`
- `experience_stats.json`
- `experience_data_quality_report.md`

本次干净导入为 26 inserted、0 duplicate；报告同时记录 parsed count 和 repository total，重复运行时不会把“0 新增”误解成“经验库为空”。
