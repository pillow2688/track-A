# Continuation V1 / V2 离线对照

结论：`INSUFFICIENT_EVIDENCE`。20 个完整绑定点仅覆盖 OPTIMIZE=17、STRUCTURAL_FIX=3；REPAIR=0、SYNTH_FIX=0，并且 ESSENTIAL_STRUCTURAL 分母为 0。

| 指标 | V1 | V2 |
|---|---:|---:|
| Bindable decisions | 20 | 20 |
| Beneficial/essential retention | 50.0% (4/8) | 100.0% (8/8) |
| Essential structural retention | INSUFFICIENT_EVIDENCE (0/0) | INSUFFICIENT_EVIDENCE (0/0) |
| Waste block rate | 41.7% (5/12) | 0.0% (0/12) |
| False block | 4 | 0 |
| Potential Token saving | 12189.0 | 0 |
| Potential Credit saving | 73.0 | 0 |
| REPAIR coverage | 0/20 | 0/20 |
| SYNTH_FIX coverage | 0/20 | 0/20 |
| STRUCTURAL_FIX coverage | 3/20 | 3/20 |
| OPTIMIZE coverage | 17/20 | 17/20 |

V2 对有益调用更保守，但当前数据上 waste block rate 退化，且结构必要调用完全没有正样本，不能进入 Shadow Pilot。潜在节省均为离线反事实估计，不是真实节省。

固定评估协议未使用 calibration 或 outcome 调参；按 run 和 task 做 leave-one-group-out，一致性均为 100%。这只证明纯规则不依赖组上下文，不代表统计泛化。Split digest：`a86232bb46d3c28286af0505c99d07fe1a2c268a2af82a5f1302ecdaad5cb6f4`。
