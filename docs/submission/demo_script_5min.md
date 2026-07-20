# 5 分钟 Demo 讲稿草案

> 自动生成于 2026-07-20T16:43:58+00:00，事实来源：`evidence_manifest.json` 指定的 run JSON。
> 不读取 hidden/golden，不把脚本 Patch replay 计作真实 LLM。`TODO` 表示缺少实验，绝非 0。

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
重点解释 baseline transaction interval 约 1025，但 loop achieved II=1；瓶颈是 1024 次串行 transaction/accumulation，不是“缺 PIPELINE”。模型采用 array partition、unroll 和多部分和并行归约。

## 3:50–4:30 Task-aware 三模式

- projection：真实模型 A01–A03 找到功能错误，但旧 Patch policy 拒绝；post-fix 历史 Patch replay 已完成真实 Vitis fresh closure。不得称为新的真实 LLM 成功。
- structural：residual deadlock 的脚本 Patch replay 已完成真实 Vitis closure；真实 LLM 仍为 TODO。
- synth-fix：dynamic allocation 的脚本 Patch replay 已完成真实 Vitis closure；真实 LLM 仍为 TODO。

## 4:30–5:00 总结

收束到三点：任务阶段路由、预算感知验证、可审计证据分级。最后明确当前缺口是重复真实模型矩阵和 hidden grader，而不是用 demo/replay 冒充结果。
