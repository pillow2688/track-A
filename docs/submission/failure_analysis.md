# V3-D 失败分析

> 自动生成于 2026-07-20T17:29:13+00:00，事实来源：`evidence_manifest.json` 指定的 run JSON。
> 不读取 hidden/golden，不把脚本 Patch replay 计作真实 LLM。`TODO` 表示缺少实验，绝非 0。

## 失败阶段分布

| Run ID | Task | Mode | Patch provider | Failure stage | Stop reason |
| --- | --- | --- | --- | --- | --- |
| v3c-real-projection-repair-a01-LOCAL_BUDGET_OVERRIDE | projection_bugfix | REPAIR | REAL_LLM | PATCH_POLICY_REJECTED | MAX_TASK_REPAIR_ROUNDS |
| v3c-real-projection-repair-a02-LOCAL_BUDGET_OVERRIDE | projection_bugfix | REPAIR | REAL_LLM | PATCH_POLICY_REJECTED | MAX_TASK_REPAIR_ROUNDS |
| v3c-real-projection-repair-a03-LOCAL_BUDGET_OVERRIDE | projection_bugfix | REPAIR | REAL_LLM | PATCH_POLICY_REJECTED | MAX_TASK_REPAIR_ROUNDS |

## 已观察到的关键问题

- projection 的真实模型 A01–A03 能定位缺失项，但 Patch 在旧的严格 hunk 行号策略处被拒绝；它们不是功能推理失败，也不是 Vitis 失败。
- projection post-fix、residual structural、synth-fix 的 replay 已通过真实 Vitis fresh final，但 replay 不构成新的真实模型成功证据。
- XSIM/CoSim 失败必须区分 RTL deadlock、仿真器内部异常与沙箱环境失败；不能把启动异常写成算法错误。

## Planner hypothesis（仅来自已保存输出）

| Run ID | Model | Hypothesis excerpt |
| --- | --- | --- |
| v3b_fast_dotproduct_live_deepseek_retry2 | deepseek-v4-pro | Applying ARRAY_PARTITION on both input arrays with factor PAR_FACTOR and LOOP_UNROLL with factor PAR_FACTOR, combined with MULTI_PARTIAL_SUM and PARALLEL_REDUCTION, will allow the loop to execute in ceil(1024/32)=32 iterations, each perform |
| v3c-real-projection-repair-a01-LOCAL_BUDGET_OVERRIDE | deepseek-v4-pro | The bug is a missing term in the z calculation for angle==0. The current code computes z = z0/3 + z1/3, omitting z2/3. Adding the missing term restores the correct average. |
| v3c-real-projection-repair-a02-LOCAL_BUDGET_OVERRIDE | deepseek-v4-pro | The bug is a missing term in the z calculation for angle==0. The expression `triangle_3d.z0 / 3 + triangle_3d.z1 / 3` omits `triangle_3d.z2 / 3`. Adding it restores the intended average of all three z coordinates. |
| v3c-real-projection-repair-a03-LOCAL_BUDGET_OVERRIDE | deepseek-v4-pro | The bug is a missing term in the z calculation for angle==0. The expression triangle_3d.z0 / 3 + triangle_3d.z1 / 3 omits triangle_3d.z2 / 3, causing incorrect z values for test cases with angle==0. |
| v3d-projection-postfix-replay-r02-NOT_REAL_LLM-LOCAL_BUDGET_OVERRIDE | operator-supplied-patch-v1 | Evaluate the operator-supplied scripted prototype patch. |
| v3d-residual-structural-replay-r04-NOT_REAL_LLM | operator-supplied-patch-v1 | Evaluate the operator-supplied scripted prototype patch. |
| v3d-synth-fix-dynamic-replay-r02-NOT_REAL_LLM | operator-supplied-patch-v1 | Evaluate the operator-supplied scripted prototype patch. |

## 下一轮所需证据

1. 在 post-fix 代码上完成 projection 新的真实模型 run（新 run ID）。
2. residual 与 synth-fix 各完成至少 3 次真实模型 run，保留全部失败。
3. 为每个失败统一记录 failure stage、Patch rejection、Token、Credit 和 final fresh closure。

## Oracle anchor 降级记录

| Run ID | Task | Reason |
| --- | --- | --- |
| v3d-oracle-vitis-opaque-anchors-a01 | v3d_fast_011 | The former function-pointer mutation synthesized successfully; A02 replaced it with an actual unsupported indirect std::function call. |
| v3d-oracle-vitis-opaque-anchors-a01 | v3d_fast_021 | The former baseline retained enough pragmas to match golden latency; A02 replaced it with a genuinely serial baseline. |
