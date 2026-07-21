# 5 分钟 Demo 讲稿草案

> 自动生成于 2026-07-20T17:29:13+00:00，事实来源：`evidence_manifest.json` 指定的 run JSON。
> 不读取 hidden/golden，不把脚本 Patch replay 计作真实 LLM。`TODO` 表示缺少实验，绝非 0。
> 2026-07-21 的真实验收与矩阵段落为人工核对补充；当前 generator 不会重建这些补充，
> 不可变事实入口是 `llm4hls_harness/releases/v3d-overnight-daytime-summary-2026-07-21.{md,json}`。

## 0:00–0:40 问题与限制

Track A 不是一次生成代码，而是在 Token、Credit 和时间受限条件下，用 CSim、Synth、CoSim 搜索正确且更快的 HLS kernel。工具成本分别是 1、4、20 Credits，因此验证顺序直接影响成绩。

## 0:40–1:20 PhaseRouter 与 Evidence

展示 baseline 验证如何路由到 `REPAIR / SYNTH_FIX / STRUCTURAL_FIX / OPTIMIZE`。展示结构化 Evidence 只保留错误类型、源码位置、loop II/TripCount、资源和有界日志，不把完整日志、hidden 或 golden 交给模型。

## 1:20–2:10 LLM Patch、Candidate 与 Budget

模型只输出 hypothesis、strategy bundle 和 unified diff；Patch Validator 与 TopInterfaceGuard 保护文件路径和顶层接口。CandidateManager 保存父子关系，BudgetLedger 是硬约束，模型不能批准工具、晋升 Candidate 或透支预算。

## 2:10–3:00 CoSim gate 与 fresh final closure

Candidate 先 CSim，再 Synth。没有严格性能提升就拒绝且不花 CoSim。stream/DATAFLOW/interface 或高风险 bitwidth 修改必须 CoSim。最终答案无条件 fresh 运行 CSim、Synth、CoSim，不能复用探索缓存。

## 3:00–3:50 真实 dotProduct

真实 DeepSeek + Vitis：1027 → 38 cycles，27.03×，Tokens=8128，Credits=35。
重点解释 baseline top transaction interval 约 1025，但 loop achieved II=1；瓶颈是单次 top transaction 内的 1024 次串行 loop accumulation/归约，不是“缺 PIPELINE”。模型采用 array partition、unroll 和多部分和并行归约。

## 3:50–4:30 Task-aware 三模式

- projection：A01–A03 暴露旧 Patch 行号策略问题；修复后的 A04 由真实模型生成 Patch，并完成 fresh final 全 PASS。重复矩阵为 3/3。
- structural：residual baseline CoSim 真实 deadlock，模型修复后 fresh final 全 PASS；重复矩阵为 3/3。
- synth-fix：dynamic allocation baseline CSim PASS、Synth FAIL，模型修复后 fresh final 全 PASS；重复矩阵为 3/3。

## 4:30–5:00 总结

收束到三点：任务阶段路由、预算感知验证、可审计证据分级。DeepSeek 12 次矩阵全部 fresh final PASS；当前缺口是 Qwen serving、公平消融和 hidden grader，而不是继续增加 Agent 架构。
