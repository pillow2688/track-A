# Phase C2：Bayesian Strategy Ranker V2 验收报告

## 验收结论

**工程验收通过，实验结论为 `NEGATIVE_RESULT`，不晋升。**

这意味着实现、测试、数据和泄漏隔离正确，但离线安全效果不满足
Mode-specific Shadow 候选门槛。

## 验收矩阵

| 验收项 | 结果 | 说明 |
| --- | --- | --- |
| C1 输入依赖 | PASS | 103 条有效，76 条可排名 |
| 条件化 Bayesian 排序 | PASS | mode/subtype/algorithm family |
| 安全 ABSTAIN | PASS | 多样性、可信下界、margin |
| 与主 Graph 隔离 | PASS | 无集成 |
| 可注入 Prompt | DISABLED | Ranker 不授权注入 |
| Leave-One-Run | PASS | leakage 0 |
| Leave-One-Task | PASS | leakage 0 |
| Leave-One-Task-Family | PASS | leakage 0 |
| 总体 coverage Gate | PASS | 43.42% |
| 总体 harmful Gate | PASS | 3.03% |
| 逐 Mode harmful Gate | **FAIL** | OPTIMIZE 12.50% |
| 每 Mode 最少 10 条 | **FAIL** | STRUCTURAL_FIX 6 |
| C2 聚焦测试 | PASS | 19/19 |
| 完整测试 | PASS | 579/579 |
| compileall | PASS | 0 error |
| diff check | PASS | 0 error |
| 当次真实预算 | PASS | 全部 0 |

## 权限判定

```text
Bayesian Ranker authority = SHADOW
Learned Ranker = TRAINING_NOT_READY
Guided = NOT_ADMITTED
Fallback = V1 / FOLLOW_EXISTING_MAIN_POLICY / ABSTAIN
```

## 人工审查建议

- 保留补丁用于理解 V2 的条件化和安全 Gate 设计。
- 不合并为正式策略，不连接主 Graph。
- 不在当前 held-out 数据上继续调阈值。
- 新增独立 STRUCTURAL_FIX 与 OPTIMIZE 反例后，再开新冻结评估。

