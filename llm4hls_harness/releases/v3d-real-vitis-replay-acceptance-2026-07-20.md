# V3-D 真实 Vitis 回放验收记录（2026-07-20）

## 结论与证据边界

本记录证明当前 task-aware Graph 的 `REPAIR`、`STRUCTURAL_FIX` 和
`SYNTH_FIX` 三条路径都能用 **Vitis 2025.2** 完成 Candidate 验证和全新的
Final CSim/Synth/CoSim closure。

这些成功 run 的 Patch 来源是历史真实模型输出或人工确定性回放，因此证据等级是：

- 工具证据：`REAL_VITIS_VALIDATED`；
- 本轮 Planner 证据：`NOT_REAL_LLM`，LLM 调用数和 Token 均为 0；
- 不能把三次回放宣称为新的端到端真实 LLM 验收；
- 当前进程缺少 `OPENAI_BASE_URL`、`OPENAI_API_KEY` 和 `LLM4HLS_MODEL`，
  因而 P0/P1/P2 的新模型尝试与模型矩阵仍被同一个外部条件阻塞。

所有失败目录均保留，成功 run 没有覆盖旧结果。`runs/` 为本地实验证据，
不进入普通源码提交。

## 验收汇总

| Mode | Task | Run | Patch 来源 | Baseline 路由证据 | Candidate | Fresh Final | Credits | Calls C/S/Co/L | Tokens | Wall time |
|---|---|---|---|---|---|---|---:|---:|---:|---:|
| REPAIR | projection_bugfix | `v3d-projection-postfix-replay-r02-NOT_REAL_LLM-LOCAL_BUDGET_OVERRIDE` | 历史 A03 DeepSeek Patch 回放 | CSim runtime fail → REPAIR | CSim/Synth PASS | PASS/PASS/PASS | 31/40 | 3/2/1/0 | 0 | 67.162 s |
| STRUCTURAL_FIX | residual_stream_deadlock | `v3d-residual-structural-replay-r04-NOT_REAL_LLM` | 确定性结构修复 Patch | CSim/Synth PASS，CoSim DEADLOCK → STRUCTURAL_FIX | CSim/CoSim PASS | PASS/PASS/PASS | 71/100 | 3/2/3/0 | 0 | 114.926 s |
| SYNTH_FIX | u55c_synthesis_repair | `v3d-synth-fix-dynamic-replay-r02-NOT_REAL_LLM` | 确定性 dynamic-allocation 修复 Patch | CSim PASS，Synth source error → SYNTH_FIX | CSim/Synth PASS | PASS/PASS/PASS | 35/80 | 3/3/1/0 | 0 | 67.942 s |

`LOCAL_BUDGET_OVERRIDE` 表示 projection 使用 40-credit 本地验收预算，**不满足且不宣称满足**
官方示例的 20-credit 预算。

## REPAIR：projection_bugfix

### 历史真实模型事实

原始真实模型输出保存在
`runs/v3c-real-projection-repair-a03-LOCAL_BUDGET_OVERRIDE`：

- provider/model：OpenAI-compatible / `deepseek-v4-pro`；
- hypothesis：`angle == 0` 分支计算 `z` 时遗漏 `z2 / 3`；
- input/output token：1606 / 427（cached input 1536）；
- 模型调用耗时：5.651 s；
- 原始 Patch SHA-256：
  `9b7c93d97cf6f78708be93cadc3b8076aa45afa0da0a73ec3aede054a9b8db3a`；
- A03 在旧的严格 hunk 行号策略下被 `PATCH_POLICY_REJECTED`，未进入 Candidate。

### 修复后真实工具回放

同一原始 Patch 在唯一上下文重定位修复后，产生：

- mode：`REPAIR`；
- baseline：Test Case 1 和 5 失败；
- relocation/metadata normalization：`true`；
- 实际应用 Patch SHA-256：
  `95b88bd5edc86c52c97eb39dc39e5383b1ae3daa9027c2000febd0182489f688`；
- Candidate/Final：`candidate_001` / `candidate_001`；
- Final code SHA-256：
  `a56815c5e37648a2daa80f66a1acd321d0bfcf6699bdbd27bd612be9111faaf7`；
- Final CSim/Synth/CoSim：全部 PASS；
- Vitis 报告 latency 为 0-cycle 组合逻辑。非 optimize 路径现在允许合法的 0-cycle
  latency，并以三项 correctness gate 判定完成，而不误送入 PPA scorer。

这是“历史真实 LLM 输出 + 修复后真实 Vitis 回放”的组合证据，不是单个原子 A04 run。

## STRUCTURAL_FIX：residual_stream_deadlock

Baseline 的真实 CoSim evidence：

- `DEADLOCK=true`、`timeout=false`、`rtl_mismatch=false`；
- `s_f`：full output FIFO，阻塞 `stageC`；
- `s_skip`：empty input FIFO，等待 `stageA`；
- `s_main`：full output FIFO，阻塞 `stageB`。

修复 Patch 把 `s_main` 与 `s_skip` 的 burst 写入改为逐元素交错写入；Patch SHA-256：
`280ffded52f2f7d45e72146bb6cb4838363ccb7c9da4cac70101aec6a4e1ab93`。

本轮同时修正两个真实 Evidence 问题：

1. Vitis 缺少标准 cosim report 时，从有界 XSIM 尾部保留 deadlock/FIFO 事实；
2. `UVM_TIMEOUT=<watchdog>` 只是仿真配置，不再被误判为真实 TIMEOUT；
   module compilation 中出现 `fifo` 也不再冒充 FIFO 故障。

TopInterfaceGuard 确认 top 保持
`void residual(data_t*,data_t*)`，接口未变，Final 三项全新执行并通过。

## SYNTH_FIX：u55c_synthesis_repair

Baseline CSim PASS，真实 Synth 报告：

- `Undefined function operator new[]`，`kernel.cpp:4:19`；
- `Undefined function operator delete[]`，`kernel.cpp:11:5`；
- `Syn check fail` / source synthesis failure；
- failure kind：`SYNTH_ERROR`。

修复 Patch 使用固定大小本地数组替代动态分配。原始/实际应用 Patch SHA-256：

- 原始：`a729327e041946ee0666af959bdc19e5d0a6d8ab6ccf08f1b861542ca38ffbd4`；
- 实际：`acf4264171589901bacd82e510923d7d5dc525bef24b9a1a26d4cd7de2578ea3`；
- metadata normalization：`true`。

Evidence 现在不会把 `RESOURCE_REPORT_UNAVAILABLE` 当成资源超限，且 source-synthesis
错误优先于外层的 constraint wrapper。TopInterfaceGuard 确认函数签名
`void vector_add_buffered(const int*,const int*,int*)` 未改变。Final CSim/Synth/CoSim
全部全新执行并通过，Final latency 为 39 cycles。

## 仍未完成的真实模型验收

唯一外部阻塞是本进程没有 OpenAI-compatible 配置：

```text
OPENAI_BASE_URL=UNSET
OPENAI_API_KEY=UNSET
LLM4HLS_MODEL=UNSET
```

配置恢复后应使用全新 `A04/A01/A01` 目录分别运行 projection、residual、synth-fix，
不得复用以上回放目录。真实模型成功率与 Token 统计必须来自那些新 run，不能从本报告推断。
