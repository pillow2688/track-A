# Phase B1.2：单一 STRUCTURAL_FIX 补证状态

## 结论

- 阶段状态：**DONE**
- 单题验收：**ACCEPTED**
- 任务：`residual_stream_deadlock`
- 来源：公开官方 fixture `fpt26-harness-public`
- 实际 Mode：`STRUCTURAL_FIX`
- Terminal：`DONE / STRUCTURAL_FIX_FINALIZED`
- Combined five-mode Gate：**READY_FOR_28_TASK_MATRIX**
- 28 题矩阵：**NOT_STARTED**

本阶段只补充 STRUCTURAL_FIX 证据，没有重跑 REPAIR、SYNTH_FIX、OPTIMIZE 或空 Stub，也没有启动 28 题或多模型矩阵。

## 候选稳定性

当前 HEAD 下三次全新、非缓存 baseline（两次独立稳定性运行，加正式 run 自带 baseline）得到完全相同的路由事实：

| 样本 | CSim | Synth | CoSim | CoSim 时间 | 结论 |
|---|---|---|---|---:|---|
| stability1 | PASS | PASS | FAIL | 25.333 秒 | 稳定匹配 |
| stability2 | PASS | PASS | FAIL | 26.188 秒 | 稳定匹配 |
| 正式 run baseline | PASS | PASS | FAIL | 27.347 秒 | 稳定匹配 |

baseline source SHA-256 始终为 `a0f40b47dea175d761e22f26edb42ac72f2ba5e25e21fdc460887fe2c639457b`，未修改。CoSim 证据包含 Vitis deadlock detector 和 `COSIM 212-4` 失败。

纯 Router 和正式 LangGraph run 都判定：

```text
mode=STRUCTURAL_FIX
reason=REQUIRED_BASELINE_COSIM_FAILED
requires_cosim=true
```

一次使用父安装目录的工具链预检在进入 HLS 前以 `TOOLCHAIN_UNAVAILABLE` 返回；它没有完成真实 CSim，不计为稳定性样本。正式配置使用从既有真实 run 核对出的 `/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis`。

## 真实 DeepSeek + Vitis 结果

| 项目 | 结果 |
|---|---|
| Model | `deepseek-v4-pro` |
| Planner | 1 次 |
| Token | 1,611 input + 591 output = 2,202 |
| Patch | 将 stageA 的 `s_main`、`s_skip` burst 写改为逐项交错写 |
| Candidate | `candidate_001 / FINAL_VERIFIED` |
| Candidate CSim | PASS |
| Candidate CoSim | PASS |
| Fresh final CSim | PASS，`cached=false` |
| Fresh final Synth | PASS，`cached=false` |
| Fresh final CoSim | PASS，`cached=false` |
| Final policy | `full_internal_audit` |
| Final digest | `9b88f96f98526f06dca992c9524ae4052d77360623f3f11732d070a86d1d5cd8` |
| Interface guard | allowed，LOW |

最终 Synth latency 为 68 cycles、interval 64、clock 2.796 ns、LUT 406、FF 231。baseline Synth 对应为 latency 135、interval 136、LUT 539、FF 248。结构正确性是本题主要验收目标。

## 预算和完整性

- Token：2,202 / 32,768；
- Credits：71 / 80；
- CSim / Synth / CoSim：3 / 2 / 3；
- Ledger runtime：122.699 秒；
- Final reserve gate：allowed；
- terminal final reserve failure：0；
- Manifest：`v3a.package-manifest.v1`；
- Artifact：85/85 SHA-256 校验通过；
- API Key / Authorization 写入：无；
- hidden / reference / golden 访问：无。

## 运行后检查

- 完整单元测试：556/556 PASS；
- `compileall`：PASS；
- `git diff --check`：PASS；
- HEAD：仍为 `4a05763b593a527878a0056f64763126c58ee63b`；
- 产品代码和测试变化：无。

## Combined five-mode Gate

Phase B1.1 已通过 REPAIR、SYNTH_FIX、OPTIMIZE 和空 Stub；本阶段在相同 HEAD 下用稳定公开 Anchor 补齐 STRUCTURAL_FIX。五种实际 Mode、Planner 路径、fresh final 三件套、digest、Ledger、预算、接口和隔离证据现已齐全。

因此 Gate 证据状态更新为 `READY_FOR_28_TASK_MATRIX`，但本阶段严格停在 Gate，不启动 28 题矩阵。
