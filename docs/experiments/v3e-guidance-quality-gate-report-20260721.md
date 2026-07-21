# V3-E Experience Guidance Quality Gate 验收报告

更新时间：2026-07-21

## 结论

本阶段已完成 Guidance Quality Gate、推荐归因和 Leave-One-Run-Out 离线评估，没有增加 Agent、RAG、RL 或主 Graph 节点，也没有修改 LangGraph、BudgetLedger、CandidateManager、ToolServer、Checkpoint 和 fresh final。

当前经验层已经从“检索到就给 Planner”变为：

```text
当前公开 Evidence
        +
真实 train Candidate 经验
        |
相似检索与策略排序
        |
Guidance Quality Gate
   |             |
INJECT        ABSTAIN
<= 600 tokens   原 Planner Prompt
   |             |
   +------ Planner ------+
             |
       原验证与 final
             |
     Recommendation Attribution
```

## 实现内容

| 组件 | 实际作用 |
|---|---|
| `GuidanceQualityGate` | 检查来源、mode、语义上下文、相似度、支持数、Evidence 冲突、历史失败、当前已尝试策略和 Prompt 上限 |
| Recommendation Attribution | 逐轮记录建议 ID、Planner 策略、FOLLOWED/PARTIALLY_FOLLOWED/IGNORED/CONTRADICTED、Patch/工具/final、晋升、加速和开销；重复 replay 幂等 |
| Leave-One-Run-Out Evaluator | 每次移除整个来源 run，用剩余经验预测该 Candidate，避免同 run 泄漏 |
| CLI postprocess | Candidate 经验导入和推荐归因使用独立 fail-open 边界，任一失败不会吞掉另一类证据 |

Gate 的硬规则：只消费 `REAL_LLM_VITIS + eligible_for_ranking + train`；mode 必须一致；failure type 或 primary bottleneck 至少一个精确匹配；默认至少两条支持；task ID 不参与；已尝试/纯失败策略被抑制；与当前 Evidence 冲突时 ABSTAIN；数据不足时保持原 Planner Prompt。

## Experience 数据规模

当前冻结经验库共 18 条可训练真实 Candidate：

| Mode | 数量 |
|---|---:|
| REPAIR | 4 |
| SYNTH_FIX | 4 |
| STRUCTURAL_FIX | 4 |
| OPTIMIZE | 6 |
| 合计 | 18 |

其中 16 条按 mode 目标成功，2 条为 OPTIMIZE 负例；4 条 OPTIMIZE Candidate 产生严格 latency 改善。Fixture、Oracle 和不可排名记录不计入上述数字。

## Leave-One-Run-Out 结果

正式结果位于 `llm4hls_harness/experiments/v3e/quality_gate_loro_20260721_A02/`。

| 指标 | 结果 |
|---|---:|
| Coverage | 22.22% |
| Success strategy hit rate | 25.00% |
| Harmful recommendation rate | 0.00% |
| Abstain rate | 77.78% |
| Duplicate-failure suppression rate | 100.00% |
| Average actual guidance tokens | 108.17 |
| High-confidence injections | 0 |

按 mode：

| Mode | 样本 | Coverage | 成功策略命中 | 有害建议 | Abstain |
|---|---:|---:|---:|---:|---:|
| REPAIR | 4 | 0% | 0% | 不可测（无负例） | 100% |
| SYNTH_FIX | 4 | 0% | 0% | 不可测（无负例） | 100% |
| STRUCTURAL_FIX | 4 | 100% | 100% | 不可测（无负例） | 0% |
| OPTIMIZE | 6 | 0% | 0% | 0% | 100% |

STRUCTURAL_FIX 的 `STRUCTURAL_REPAIR` 是目前唯一稳定有帮助的建议：4 次 leave-one-run-out 均命中，支持数为 3。其 confidence 为 0.375，仍未达到 guided pilot 所要求的高置信阈值 0.5。

没有观察到被实际注入的有害建议。OPTIMIZE 的 memory/parallel bundles 因支持分散或与当前 bottleneck 冲突被正确 ABSTAIN；REPAIR 和 SYNTH_FIX 在移除一个 run 后只剩 1 条满足完整相似条件的支持，也被正确 ABSTAIN。

## 12 题 Shadow Pilot

计划任务为 REPAIR `001/002/003`、SYNTH_FIX `009/010/011`、STRUCTURAL_FIX `015/016/017`、OPTIMIZE `021/022/028`，模型为真实 `deepseek-v4-pro`，backend 为真实 Vitis 2025.2。

| Attempt | 状态 | 结果 |
|---|---|---|
| A01 | INCOMPLETE | 沙箱禁止网络 socket；6 题在真实 baseline 后 Planner dispatch 失败，1 题 baseline 后中断，5 题未启动 |
| A02 | BLOCKED BEFORE START | 平台拒绝向外部模型发送标记为 hidden-like 的本地派生任务上下文；未创建目录 |

A01 前 6 题的 Gate 均为 ABSTAIN，证明这些失败不是经验注入造成的。独立最小 API 探测在允许网络时返回 HTTP 200，证明 endpoint、key 和模型有效。由于没有 terminal run，本阶段不能报告 shadow final success、真实 provider usage 或 recommendation outcome；这些字段保持不可测。

## 是否执行 Guided

不执行。离线策略要求 harmful rate 不超阈值、平均 guidance 不超过 600 tokens，并至少有若干高置信可注入案例。前两项通过，但 high-confidence injections 为 0，所以机器决策为 `SKIP_GUIDED`。这不是实验失败，而是 Quality Gate 按预设条件阻止低证据建议影响 Planner。

## Attribution 闭环

每个 terminal run 结束后会从不可变 artifacts 重建：

- recommendation ID 与 round；
- Planner strategy bundle；
- FOLLOWED / PARTIALLY_FOLLOWED / IGNORED / CONTRADICTED；
- Patch 是否物化；
- Candidate CSim/Synth/CoSim 与 fresh final；
- promoted、acceleration、Token、Credit、wall time。

Attribution ID 绑定 run、recommendation、round、Candidate 和 Planner bundle；checkpoint replay 再次执行只记 duplicate，不会追加第二条。ABSTAIN 没有进入 Prompt，因此即使 Planner 碰巧选择同一策略，也记为 IGNORED，而不会伪称 FOLLOWED。

## 测试

- `compileall`：PASS。
- 全量 unittest：436/436 PASS，31.925 秒。
- 覆盖 INJECT/ABSTAIN、数据不足、Evidence 冲突、hidden split、task ID 无关、fixture/oracle 排除、600-token 上限、checkpoint 幂等归因、off 等价、shadow Prompt 不变、guided 仍受 Budget/Patch/final 约束。

## 下一步判断

下一步应继续积累真实数据，而不是现在训练 Strategy Ranker 或 CoSim Risk。

原因很直接：18 条记录中只有 STRUCTURAL_FIX 形成至少 3 条同语义支持，其他 mode 在 leave-one-run-out 后都缺乏两条匹配证据。此时训练 ranker 容易学习到任务偶然性；训练 CoSim Risk 也缺少高风险失败负例。优先为 REPAIR、SYNTH_FIX 和不同 OPTIMIZE bottleneck 各补充真实成功与失败 Candidate，达到可分层评估的数据量后再训练 Strategy Ranker。
