[English](v2-ppa-gated-final.md)

# V2 PPA 门控最终发布记录

发布日期：2026-07-17

发布 ID：`v2-ppa-gated-final`

总体状态：`PASS / FINAL`

## 发布决定

V2 全局冠军固定为 `runs/v2-optimize-final/candidate_004`，发布状态为
`FROZEN_GLOBAL_CHAMPION`。本次门控回归的
`runs/next-ppa-gated-real/candidate_003` 状态固定为
`VALID_BUT_NOT_GLOBAL_BEST`，保留真实 DeepSeek/Vitis 证据，但不替换全局冠军。

## 全局冠军

| 字段 | 结果 |
|---|---:|
| Candidate | `candidate_004` |
| Latency worst | 128 |
| II max | 129 |
| Estimated clock | 0.880 ns |
| LUT / FF | 4829 / 129 |
| PPA cost（越低越好） | 0.4006831529 |
| CSim / Synth / CoSim / Clock | PASS / PASS / PASS / PASS |
| 状态 | `FROZEN_GLOBAL_CHAMPION` |

冠军源码 SHA-256：
`ee5a0fc7b61f578159487877bde799f14dc53a51ddd40d7013bc9a7988aec65e`。
冠军 Patch SHA-256：
`768fa8cc6f402a8a1666d7d2adb7d6602ef6048bcaab14d032a0c08f45e1e1e0`。
冠军 Artifact Manifest SHA-256：
`aee4310f17753a359264153a6e1a77f4e8f5f007030abc7edbed5a5b12740419`。

## 固定优化流程

```text
CSim -> Synth -> 严格 PPA 门控
  -> 只有优于 current best 才运行探索 CoSim
  -> CoSim、Clock、资源硬约束全部 PASS 才晋升
  -> 最多 6 个优化 Candidate
  -> 连续 2 次无提升即停止
  -> 最终 best 独立完整运行 CSim、Synth、CoSim 和 Clock 检查
```

正确性和硬约束始终优先于 PPA。未通过门控的 Candidate 必须记录
`CoSim=NOT_RUN`，不得成为 best。

## 门控回归结果

本次回归真实调用 `deepseek-v4-pro` 5 次，无 fallback；input/output/cached/total
Token 为 9481/2436/2432/11917。最终 `candidate_003` 的 CSim、Synth、CoSim 和 Clock
全部 PASS，但 PPA cost 为 0.6005631669，差于全局冠军的 0.4006831529，因此不晋升为
全局冠军。

| 指标 | 原正式运行 | 门控回归 | 差值 |
|---|---:|---:|---:|
| CSim 调用 | 6 | 7 | +1 |
| Synth 调用 | 6 | 7 | +1 |
| CoSim 调用 | 6 | 4 | -2 |
| Credits | 150 | 115 | **-35** |

少运行 2 次 CoSim 毛节省 40 credits；多运行 1 次 CSim 和 Synth 增加 5 credits，净节省
35 credits。该真实回归证明了 CoSim 门控能够降低成本，同时保持最终完整验证。

## 验收与完整性

- 全局冠军机器验收：Evaluator `v2.1`，`REAL / PASS`，包含
  `exploration_cosim_gated=true`，结果 SHA-256
  `ff585393de42eb2232eb08a14bc724c7e764c3fa23f99699ef1f3fcbdc9f2196`；
- 门控回归机器验收：Evaluator `v2.1`，`REAL / PASS`，包含
  `exploration_cosim_gated=true`，结果 SHA-256
  `83c3dac575832e6f0fdcbdd2ac36751e58d7b67e4c234916cc3952fbe3b35302`；
- 安全拒绝 Manifest SHA-256：
  `4000b1bda7763273d39f641c2ef3719c451ff77f5fc07cc0c5fb05e12ac2d815`；
- 两次机器验收均无 reason code，Ledger、Trace、action、Candidate tree 和 Manifest
  一致性检查通过。
- 发布前 140 项快速单元测试、Python compileall、release Hash 绑定检查和
  `git diff --check` 全部通过。

完整的机器可读 release 元数据见
[`v2-ppa-gated-final.json`](v2-ppa-gated-final.json)。原始证据位于：

- [全局冠军运行](../runs/v2-optimize-final/experimental_report.md)
- [全局冠军发布验收](../runs/v2-release-acceptance/acceptance_result.json)
- [门控回归运行](../runs/next-ppa-gated-real/experimental_report.md)
- [门控回归机器验收](../runs/v2-gated-regression-acceptance/acceptance_result.json)
- [安全拒绝运行](../runs/v2-safety-rejection-final/v2_rejection_result.json)

V2 至此关闭。后续开发进入 `V3_LANGGRAPH_ORCHESTRATION`，不得用本次回归
`candidate_003` 覆盖冻结冠军。
