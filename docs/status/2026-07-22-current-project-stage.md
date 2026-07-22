# 2026-07-22 项目当前阶段：V3-F

项目的目标是在有限 Token、Credit、工具调用和时间预算中，让 LLM 根据公开任务和真实 Vitis HLS 证据提出 kernel patch；Python/LangGraph 负责验证、预算、Candidate 晋升和 fresh final，LLM 不拥有这些权限。

## 已完成

- V3 的 baseline、四种 task-aware mode（REPAIR、SYNTH_FIX、STRUCTURAL_FIX、OPTIMIZE）、Candidate、Budget、ToolServer、Patch/Top Interface guard 与 fresh final 闭环。
- 真实 DeepSeek 与真实 Vitis 的实验闭环及可复盘 artifact。
- Experience Store、Quality Gate、Attribution 的工程层；默认仍不让 Experience 改写主控制流。
- TokenEstimator、TokenEnvelope、stable output cap，以及 terminal summary 在 latency 缺失时的稳定性修复。
- Token Policy A/B/C 和 Hybrid V2 的真实对照。结论是：稳定 cap 已经工程可用，但单次输出 cap 不是主要浪费来源；OPTIMIZE 的无价值 follow-up 调用才是当前优先问题。

## 正在进行：V3-F

本阶段名为 **Performance-Area-aware Value-Gated Continuation**，不是完整 PPA 优化。

- 在既有 Budget/Stop gate 内生成可哈希的 ContinuationDecision：`ALLOW`、`BLOCK`、`DEFER_TO_FINAL`。
- 只对已经产生、且在下一次 Planner 调用前可见的证据做决定；不读取 follow-up response、Candidate、final 或未来指标。
- 区分普通数值波动与可操作的语义变化，例如 bottleneck、critical loop、memory/scheduling、stream/FIFO 或 failure subtype 改变。
- 为 Synth PASS Candidate 记录 latency、transaction interval、clock、LUT/FF/DSP/BRAM/URAM 的 Performance–Area advisory；默认 Comparator 仍是 latency-first。
- `shadow` 只记录决定，不改变路径；离线 replay 通过后才考虑 `enforce`。

Power 没有可信来源，因此当前不采集或声称 Power/PPA score。`area_proxy` 是基于可用 FPGA 资源比例和版本化权重的工程代理，不是芯片物理面积。

## 当前不做

- Strategy Ranker、CoSim Risk 或 Continue Predictor 训练。
- RL、Bandit、RAG、多 Agent 或新的主 LangGraph node。
- 改动 BudgetLedger、CandidateManager、ToolServer、fresh final、正确性定义或 latency-first 正式晋升规则。
- 28 题正式评测；必须先完成 leakage-safe replay 和小规模 pilot 准入。

## V3-F 退出条件

- 离线 replay 保留全部 essential correctness 和全部 beneficial performance/area follow-up。
- OPTIMIZE 的无价值 follow-up 能被大比例阻止，且 STRUCTURAL_FIX 的新 CoSim 根因不会被过早挡住。
- 无 future leakage、无重复策略放行，预计 Token/Credit/时间节省为正。
- Shadow 和受控 enforce pilot 中 final correctness 不下降，Performance–Area advisory 不发生明显退化。

## 组员阅读顺序

1. `doc/docs/README.md`：团队入口与报告索引。
2. `docs/experiments/token-policy-hybrid-v2-real-evaluation-20260722.md`：为什么 V3-F 不继续调 output cap。
3. `llm4hls_harness/llm4hls_agent/v3_prototype.py`：主流程和既有 gate。
4. `llm4hls_harness/llm4hls_agent/v3_continuation.py`：V3-F 的纯 Python 决策与 Performance–Area 代理。
