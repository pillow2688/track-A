# Phase C2：Bayesian Strategy Ranker V2 状态

日期：2026-07-23  
冻结 HEAD：`4a05763b593a527878a0056f64763126c58ee63b`  
C1 依赖：`PASS_PROMOTABLE`  
组件状态：`NEGATIVE_RESULT`  
正式权限：`SHADOW`  
Learned Ranker：`TRAINING_NOT_READY`

## 结论

Bayesian Strategy Ranker V2 的独立实现、专用测试和离线 holdout 评估均正确完成，
但 Mode-specific 安全指标不满足候选门槛，因此不晋升。

这是实验负结果，不是工程失败。保留产品 diff 和完整证据供人工审查；现有
Bayesian Ranker V1、确定性主流程和 `ABSTAIN` 回退保持不变。

## V2 设计边界

- 只使用 `mode + subtype + algorithm_family` 和公开结构特征形成查询。
- 历史 support 只接受 `REAL_LLM_VITIS`、train/dev、ranking eligible 记录。
- 使用固定 Beta(1,1) prior、可信下界、最小 top margin。
- 推荐前要求 attempts、run、task family、patch digest 和成功 family 多样性。
- 当前 run/candidate/patch 与已尝试策略会被排除。
- 只返回 `RECOMMEND` 或 `ABSTAIN` 的 Shadow 结果。
- 不生成可注入 Prompt，不调用工具，不接入主 Graph。

## 固定候选门槛

评估前固定：

- Leave-One-Task coverage ≥ 40%；
- Leave-One-Task-Family coverage ≥ 40%；
- 有害重复推荐率整体和每个有推荐的 Mode 均 ≤ 5%；
- 每个 Mode 至少 10 条可排名记录；
- 所有 holdout 泄漏检查为 0。

没有根据 held-out 结果放宽阈值或删除负样本。

## 离线结果

| Holdout | 推荐/查询 | Coverage | 正例策略命中 | 有害重复推荐 | 失败策略抑制 | 泄漏 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Leave-One-Run | 31/76 | 40.79% | 35.48% | 0.00% | 100.00% | 0 |
| Leave-One-Task | 33/76 | 43.42% | 37.10% | 3.03% | 92.86% | 0 |
| Leave-One-Task-Family | 33/76 | 43.42% | 37.10% | 3.03% | 92.86% | 0 |

关键 Mode 结果（Leave-One-Task 与 Leave-One-Task-Family 相同）：

| Mode | 可排名记录 | Coverage | 正例策略命中 | 有害重复推荐 |
| --- | ---: | ---: | ---: | ---: |
| OPTIMIZE | 26 | 30.77% | 8.33% | **12.50%** |
| REPAIR | 25 | 48.00% | 48.00% | 0.00% |
| STRUCTURAL_FIX | 6 | 0.00% | 0.00% | 0.00% |
| SYNTH_FIX | 19 | 68.42% | 52.63% | 0.00% |

现有 V1 正式 guidance baseline 在相同 76 条记录的四种 holdout 中均为
0% coverage / 全部 ABSTAIN。V2 虽产生了非零 Shadow coverage，但不能用总体
3.03% 掩盖 OPTIMIZE 单模式 12.5% 的风险。

## 状态原因

`NEGATIVE_RESULT` 的直接原因：

1. OPTIMIZE 有害重复推荐率 12.5%，超过固定 5% Mode-specific Gate；
2. OPTIMIZE 正例策略命中率仅 8.33%；
3. STRUCTURAL_FIX 只有 6 条可排名记录，且全部 ABSTAIN；
4. 各 Mode 最少 10 条记录的证据门槛未满足。

## 验证与预算

- C2 聚焦测试：19/19 PASS。
- 全套测试：579/579 PASS。
- `compileall`：PASS。
- `git diff --check`：PASS。
- run/task/task-family holdout leakage：0。
- 查询 outcome 字段：0。
- hidden/reference/golden support：不允许，未访问。
- 当次真实 LLM、Token、CSim、Synth、CoSim、Tool Credits：全部 0。

## 证据

- 产品候选：
  `llm4hls_harness/llm4hls_agent/v3_strategy_ranker_v2.py`
- 专用测试：
  `llm4hls_harness/tests/test_v3_strategy_ranker_v2.py`
- 评估脚本：
  `docs/experiments/artifacts/2026-07-23-c2-bayesian-strategy-ranker-v2/c2_offline_evaluation.py`
- 完整结果：
  `docs/experiments/artifacts/2026-07-23-c2-bayesian-strategy-ranker-v2/c2-offline-evaluation.json`

