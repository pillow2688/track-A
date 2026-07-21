# V3-E Experience Dataset Card

来源：76 条 public train/dev、真实 LLM + Vitis、ranking-eligible Candidate。

| 数据集 | Train | Validation | Test |
|---|---:|---:|---:|
| strategy_ranking | 83 | 6 | 16 |
| cosim_risk | 44 | 4 | 16 |
| continue_value | 11 | 1 | 0 |
| evidence_selection | 55 | 5 | 16 |

同一 task family 只进入一个 split。task ID、完整源码、完整 Patch、Prompt、日志、secret 及 golden/reference 内容均不进入数据行。

当前导出适合做规则/特征审计与小规模基线，不代表数据量已经足够训练复杂模型；正式训练必须服从 readiness 报告。
