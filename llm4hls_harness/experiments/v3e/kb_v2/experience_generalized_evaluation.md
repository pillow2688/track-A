# V3-E Experience 泛化离线评估

评估真实可排名 Candidate：76。

| Holdout | Coverage | 成功策略命中 | 有害建议率 | 重复失败抑制 | 泄漏率 | 平均 Guidance Token |
|---|---:|---:|---:|---:|---:|---:|
| leave_one_algorithm_family_out | 0.00% | 0.00% | 0.00% | 100.00% | 0.00% | 0.0 |
| leave_one_run_out | 0.00% | 0.00% | 0.00% | 100.00% | 0.00% | 0.0 |
| leave_one_task_family_out | 0.00% | 0.00% | 0.00% | 100.00% | 0.00% | 0.0 |
| leave_one_task_out | 0.00% | 0.00% | 0.00% | 100.00% | 0.00% | 0.0 |

Task 分组 hash 只用于离线剔除 support，不进入 Retriever、Ranker 或 Planner 特征。ABSTAIN 表示安全回退原 Planner。
