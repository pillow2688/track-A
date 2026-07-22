# V3-F：Performance-Area-aware Value-Gated Continuation

日期：2026-07-22。分支：`feat/v3f-value-gated-pa-continuation`。

## 目的与边界

V3-F 处理的是“第二次及以后 Planner 调用值不值得”的问题。它没有新增 Agent 或 LangGraph node：决定被嵌入既有的 Budget/Stop gate。LLM 仍然只生成 hypothesis、strategy 和 patch；Budget、验证、Candidate promote/reject 和 fresh final 仍由 Harness 控制。

该阶段的准确能力名称是 **Performance-Area-aware optimization**。Performance 使用 latency、transaction interval、achieved II、clock 和 acceleration；Area proxy 使用 LUT、FF、DSP、BRAM、URAM 的 FPGA 可用资源比例。Power 没有可信数据，因此未采集、未计算、也未声称完整 PPA 或比赛 PPA score。

## Hybrid V2 的出发结论

Hybrid V2 的真实负结果说明 stable output cap 和极简 Token prompt 已经工程可用，但不是主要 Token 成本来源。主要浪费来自 OPTIMIZE 的 follow-up Planner 调用：历史观察中有许多后续调用没有带来可验证改善；另一方面，STRUCTURAL_FIX 不能因为名称仍是 deadlock 就被粗暴提前停止，因为更精确的 FIFO/stream/producer-consumer 证据仍有价值。

## 本次已实现

- `v3_continuation.py`：版本化 `v3.continuation-decision.v1`，稳定 SHA-256，三种结果 `ALLOW/BLOCK/DEFER_TO_FINAL`。
- 确定性 Evidence Fingerprint/Delta：忽略路径、时间戳、Candidate ID 和纯 latency 波动；识别 bottleneck、loop、memory/scheduling、failure subtype、stream/FIFO 等可操作变化。
- Patch-observed Strategy Novelty：具体 pragma/patch 行为优先于 Planner 自报，识别重复 bundle、patch、PIPELINE_ONLY、partition/FIFO 类策略。
- `performance_area_policy_v1.json`：版本化资源权重（LUT 0.25、FF 0.15、DSP 0.25、BRAM 0.25、URAM 0.10）。
- `v3.performance-area-delta.v1`：保留 raw metrics、utilization、area proxy、latency×area proxy、Pareto relation 和 delta class；默认 Comparator 仍是 `latency_first`，Area 只作 advisory。
- `--continuation-policy off|shadow|enforce`：默认 CLI 为 shadow；off 不改变原有路由；enforce 只使用既有 stop/final edge。
- candidate Synth PASS 后和 Planner call 前分别写入 `performance_area/` 与 `planner/call_gates/` artifact。

## 当前 Area/PA 公式

`area_proxy = 0.25*LUT_util + 0.15*FF_util + 0.25*DSP_util + 0.25*BRAM_util + 0.10*URAM_util`。

`performance_area_proxy = latency * area_proxy`，并在 artifact 中记录 reference。资源缺失时 proxy 为 `UNKNOWN`，不会被当作 0。Pareto 关系为 `DOMINATES`、`DOMINATED`、`NON_DOMINATED`、`EQUAL` 或 `UNKNOWN`。

## Leakage-safe Replay

历史 V3 run 的 follow-up 在 `pre_state`（决定时已经可见）和 `outcome`（仅供 evaluator 打标签）之间物理分离。Policy evaluator 不读取 follow-up response、patch、Candidate、final、future latency 或 future area。

本次扫描 83 个已有 V3 run，R02 dataset 得到 11 个可完整绑定的真实 Vitis OPTIMIZE follow-up：5 个事后表现为 `BENEFICIAL_PERFORMANCE`、6 个为 `HARMFUL`。排除项为 4 个非真实 Vitis、1 个缺 Synth 指标和 1 个重复记录。

R02 当前结果：beneficial retention = 60%，waste block rate = 16.7%。它不满足阶段准入要求（beneficial/essential retention 应为 100%，OPTIMIZE waste block rate 应至少 75%）。因此：

- 不运行 Shadow Pilot；
- 不运行 enforce Pilot；
- 不运行 12 题或 28 题真实矩阵；
- 不声称 Token、Credit、时间或竞赛分数提升。

这不是 V3-F 实验失败的结论，而是当前 11 条可用历史样本不足且 policy 误杀有益 follow-up 的明确证据。下一步唯一推荐项是：完善历史 follow-up 的**决策时** Synth/Failure evidence 回放覆盖和 outcome 标注，再在 calibration split 上调整规则；不能在 held-out 上调阈值。

## 验证

已通过：

- `test_v3_continuation.py`：10 项。
- `test_v3_continuation_replay.py`：1 项。
- `test_v3_prototype.py` + `test_v3_fast_experiment.py`：52 项。
- `compileall` 和 `git diff --check`。

真实外部模型/Vitis 的 V3-F shadow/enforce 尚未运行，因为离线准入未通过。
