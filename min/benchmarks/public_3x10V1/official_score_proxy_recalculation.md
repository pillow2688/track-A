# V1 官方 Score Proxy 重新计算

## 结论

以 `/home/ying/下载/scoring.py`（SHA-256
`5f65aaa27bfc3cd488ae2294aa4cf463b6be4399306ffb847c44cdd3861d33fe`）为唯一公式来源重新计算后：

| 任务 | difficulty | 10 次分数合计 | 平均 Score Proxy | fallback 次数 | 满 PPA 次数 |
|---|---:|---:|---:|---:|---:|
| projection_bugfix | 2 | 14.0000 | 1.4000 | 0 | 0 |
| dotProduct_optimize | 3 | 24.0380 | 2.4038 | 6 | 2 |
| residual_stream_deadlock | 4 | 30.9780 | 3.0978 | 0 | 0 |

- 每个 repetition 的三任务合计平均：**6.9016 / 9.0000**
- 30 个实验 Slot 的均值：**2.3005**
- 30 个 Slot 分数合计：**69.0160**
- 30/30 与 V1 已存 `selected_public_score_proxy` 完全一致。需要纠正的是
  对分数的解释，不是这 30 个已存数值。

这仍是 **Score Proxy**：V1 只有公开验证证据。正式服务器若 hidden
functional（以及必要的 cosim）失败，该任务实际官方分数为 0。

## 官方公式

功能通过时：

```text
acceleration = baseline_latency_cycles / candidate_latency_cycles
ppa_norm = min(acceleration, 8) / 8
score = difficulty × (0.5 + 0.2 × synth_pass + 0.3 × ppa_norm)
```

功能失败时分数严格为 0。`is_opt` 只判断 acceleration 是否严格大于 1，
不充当计分 Gate。

因此：

- baseline fallback 且 acceleration=1：
  `score = difficulty × 0.7375`，DotProduct 为 **2.2125**，不是 0。
- `0 < acceleration ≤ 1` 仍有 PPA 分：
  `score = difficulty × (0.7 + 0.0375 × acceleration)`。
- latency 为 0 或缺失时 acceleration 为 null、PPA 为 0；只要功能和 Synth
  通过仍得 `difficulty × 0.7`。Projection 的 latency=0，因此为 **1.4000**。

## DotProduct 逐次结果

| Run | 最终选择 | baseline cycles | candidate cycles | acceleration | ppa_norm | 官方 Score Proxy | clock ns（工程） | ≤5ns |
|---|---|---:|---:|---:|---:|---:|---:|:---:|
| 01 | candidate_000 | 1027 | 1027 | 1.000000 | 0.1250 | 2.2125 | 3.170 | 是 |
| 02 | candidate_000 | 1027 | 1027 | 1.000000 | 0.1250 | 2.2125 | 3.170 | 是 |
| 03 | candidate_001 | 1027 | 513 | 2.001949 | 0.2502 | 2.3252 | 3.170 | 是 |
| 04 | candidate_000 | 1027 | 1027 | 1.000000 | 0.1250 | 2.2125 | 3.170 | 是 |
| 05 | candidate_000 | 1027 | 1027 | 1.000000 | 0.1250 | 2.2125 | 3.170 | 是 |
| 06 | candidate_001 | 1027 | 342 | 3.002924 | 0.3754 | 2.4378 | 3.170 | 是 |
| 07 | candidate_001 | 1027 | 35 | 29.342857 | 1.0000 | 3.0000 | 31.133 | 否 |
| 08 | candidate_000 | 1027 | 1027 | 1.000000 | 0.1250 | 2.2125 | 3.170 | 是 |
| 09 | candidate_001 | 1027 | 35 | 29.342857 | 1.0000 | 3.0000 | 31.133 | 否 |
| 10 | candidate_000 | 1027 | 1027 | 1.000000 | 0.1250 | 2.2125 | 3.170 | 是 |

run_07 和 run_09：

```text
acceleration = 1027 / 35 = 29.342857×
capped acceleration = 8×
ppa_norm = 1
score = 3 × (0.5 + 0.2 + 0.3) = 3.0000
```

两次估算 clock 都是 31.133 ns，但官方 `scoring.py` 不读取 clock，
因此不会扣分。只要服务器 hidden correctness 和 Synth PASS，二者就是
DotProduct 满分。

## 工程指标与官方分数的边界

- clock、LUT、FF、DSP、BRAM、URAM 已逐 run 保留在 JSON 的
  `engineering_metrics`。
- 28/30 个最终选择满足 5 ns；不满足的是 DotProduct run_07/run_09。
- 所有资源均在报告的器件可用量内。
- 这些指标可用于工程决策和风险提示，但没有进入本次官方 Score Proxy 的
  correctness、synthesizable 或 cycle-acceleration 三个计分项。

## 对原 V1 汇总的更正

原报告中的平均 Score Proxy：

- Projection 1.4000
- DotProduct 2.4038
- Residual 3.0978
- Overall per-slot 2.3005

数值本身与官方公式一致。需要废止的是“只有 objective success 或
hardware-qualified 才有官方分”的解释：6 次 DotProduct baseline fallback
共贡献 **13.2750** 分；31.133 ns 的两次各贡献 **3.0000**。
