# Phase C0：Mode-Specific Continuation V2 离线 Shadow 试验报告

## 1. 阶段目标

本阶段只回答一个问题：在不改变现有主框架、不调用真实 LLM/Vitis、不赋予正式停止权限的前提下，能否用按 Mode 区分的确定性规则，减少 Continuation V1 对有益 follow-up 的误杀，同时保持可审计的信息边界。

最终结果是：V2 的离线纯函数和回放框架已完成，但现有真实样本不足，且 waste block 指标退化，阶段准入为 `INSUFFICIENT_EVIDENCE`，V2 Shadow Pilot 为 `NOT_READY`。

## 2. 当前 V1 能力

V1 位于 `llm4hls_harness/llm4hls_agent/v3_continuation.py`，schema 为 `v3.continuation-decision.v1`。它具备：

- 确定性 canonical JSON 和 decision hash；
- bounded Evidence fingerprint 与 Delta；
- Patch 静态扫描优先的 observed Strategy atoms；
- Strategy duplicate/novelty；
- Token、Credit、final reserve 和剩余轮数硬条件；
- Performance-Area-aware advisory；
- `ALLOW/BLOCK/DEFER_TO_FINAL` 三态输出；
- 主路径 `off/shadow/enforce` 支持。

但 V1 的核心是跨 Mode 共用的 value score。REPAIR 与 SYNTH_FIX 没有独立停止条件；STRUCTURAL_FIX 只有 subtype refinement 加分；OPTIMIZE 虽有 bottleneck 与 Performance-Area 条件，也未把 latency、II、interval、clock、resource、8× cap 和低边际收益组织成完整的 Mode-specific 决策树。运行时默认仍是 `off`，当前接受权限只到 Shadow，Enforce 未准入。

## 3. 数据来源

### 3.1 纳入来源

| 来源 | 用途 | 绑定结果 |
|---|---|---:|
| 旧 V3-F R02 | 已有 follow-up 回放基线 | 11 |
| 72-run Token Policy A/B/C | 当前审计批次的真实二次 Planner 调用 | 9 |
| Phase B1/B1.1/B1.2 Anchor | 检查是否存在 follow-up | 0 |
| Experience Candidate records | 交叉核对 Candidate/Outcome，避免重复计数 | 0 个新增点 |
| 当前 Continuation candidate dataset | 绑定 Phase A 9 点及 1 个排除 | 9 |

Experience 的 81 条记录与 72-run Candidate 重叠，只作支持证据，不能把一个 Candidate 或完整 run 再算成新的 Continuation decision point。

### 3.2 绑定漏斗

- 来源筛查记录：101。
- 可识别真实 follow-up 候选点：21。
- 完整绑定：20。
- follow-up 排除：1（`OUTCOME_NOT_BINDABLE`）。
- 全部来源筛查排除：81，其中 `NO_FOLLOW_UP=74`。

绑定集的 Mode 分布为 REPAIR=0、SYNTH_FIX=0、STRUCTURAL_FIX=3、OPTIMIZE=17；Outcome 分布为 BENEFICIAL_PERFORMANCE=8、HARMFUL=12。

## 4. 信息泄漏隔离

Policy 调用只有：

```text
mode
pre_state
```

统一数据把 outcome 物理放在独立对象中。Policy 不读取 follow-up Planner response、follow-up Patch、future Candidate、promotion、future tool result、final、outcome label、future latency 或 future resource。单测还向 `pre_state` 注入这些未知字段，确认输出和 digest 完全不变。

旧 R02 builder 的 `pre_state.budget` 来自终态 `v3_prototype_result.json`，属于严格 V2 口径下的 future-information leakage 风险。当时 evaluator 的 estimated next cost 为 0，所以该字段未改变已归档 V1 决策；本阶段没有继续使用它，而是把 R02 的 decision-time budget 保留为 `UNKNOWN/null`。未知值没有被当成 0。

Phase A 的结构二次输入没有完整复制上一 Candidate 的 CoSim failure evidence。本阶段只从第二次 Planner 调用前已经落盘的 `candidate_001.validation` 及其 action result 补充 FIFO/process signature；没有读取 `candidate_002` 的 Patch 或验证结果作为 Policy 输入。

## 5. Mode-specific V2 规则

V2 位于 `llm4hls_harness/llm4hls_agent/v3_continuation_v2.py`，schema 为 `v3.continuation-decision.v2`。它是独立纯函数，未知字段经过 allow-list 后被忽略，输出包含稳定 `decision_digest` 和固定 fallback `FOLLOW_EXISTING_MAIN_POLICY`。

### 5.1 通用规则

- `final_reserve_available=false` 时只能 `DEFER_TO_FINAL`，不能 ALLOW。
- Evidence 冲突时低置信度 fallback；有 incumbent 则 DEFER，否则保守 ALLOW。
- Evidence 不完整时不得高置信度 BLOCK。
- 没有已验证 incumbent 时，不因节省 Token 直接把未知 Candidate 当作 final。

### 5.2 REPAIR

subtype/location 改变、error count 下降、新 actionable evidence、correctness progress 或新 repair strategy 均 ALLOW。只有 same signature、same location、same strategy、连续至少两次无进展同时成立时，才 BLOCK；已有正确 incumbent 且 reserve 紧张时改为 DEFER。一次失败不会 BLOCK。

### 5.3 SYNTH_FIX

stage advance、unsupported construct 消失、新 scheduling/memory/actionable evidence 或新 synth strategy 均 ALLOW。只有相同 unsupported/failure、相同位置、相同策略且连续无进展时才倾向停止。

### 5.4 STRUCTURAL_FIX

该 Mode 最保守。新 FIFO、producer/consumer imbalance、deadlock location、topology understanding、transaction mismatch specificity 或新 structural strategy 均优先 ALLOW。BLOCK 必须同时满足 same structural signature/location/FIFO/strategy、无 actionable evidence、reserve safe 和连续无进展；BLOCK confidence 最高 MEDIUM。

### 5.5 OPTIMIZE

规则分别观察 latency、II、transaction interval、clock、resource、bottleneck、策略和 8× cap。显著 latency gain、II/interval/clock 改善、bottleneck 改变或新策略均 ALLOW。已有 verified incumbent 且达到 8× cap，或低收益叠加重复策略时 DEFER。只有连续无收益、证据和策略均重复，且没有更好 verified candidate 等待 final 时才 BLOCK。缺失 resource 保持 UNKNOWN。

## 6. 固定评估协议

规则完全来自 Phase C0 冻结说明，没有在 outcome 上反复调阈值，也没有 calibration。由于样本小，采用：

- Leave-one-run-out：20/20 决策与全量运行一致。
- Leave-one-task-out：9/9 task group 一致。
- Split digest：`a86232bb46d3c28286af0505c99d07fe1a2c268a2af82a5f1302ecdaad5cb6f4`。

V2 不训练、不读取 task ID，因此 leave-one-group-out 的一致性主要证明确定性和无组依赖，不能被解释为统计泛化或模型准入证据。

## 7. V1 / V2 对比

| 指标 | V1 | V2 | 变化 |
|---|---:|---:|---:|
| Bindable decisions | 20 | 20 | 0 |
| Beneficial/essential retention | 50.0%（4/8） | 100.0%（8/8） | +50.0pp |
| Essential structural retention | INSUFFICIENT_EVIDENCE（0/0） | INSUFFICIENT_EVIDENCE（0/0） | 不可比较 |
| Waste block rate | 41.7%（5/12） | 0.0%（0/12） | -41.7pp |
| False block | 4/8 | 0/8 | -4 |
| Potential Token saving | 12,189 | 0 | -12,189 |
| Potential Credit saving | 73 | 0 | -73 |
| REPAIR coverage | 0/20 | 0/20 | 无证据 |
| SYNTH_FIX coverage | 0/20 | 0/20 | 无证据 |
| STRUCTURAL_FIX coverage | 3/20 | 3/20 | 仅 harmful |
| OPTIMIZE coverage | 17/20 | 17/20 | 主要证据来源 |

“Potential saving”只统计被 BLOCK/DEFER 的 HARMFUL/NEUTRAL follow-up 且后续成本已知的记录，是 offline potential savings，不是真实节省。

## 8. 按 Mode 评估

### REPAIR

Decision coverage 0/20。规则行为有单测，但没有真实可绑定 follow-up，所有 retention、waste、false-block 结论均为 `INSUFFICIENT_EVIDENCE`。

### SYNTH_FIX

Decision coverage 0/20。与 REPAIR 相同，不能由单测替代真实回放分母。

### STRUCTURAL_FIX

Decision coverage 3/20，三条 outcome 全为 HARMFUL。V1 BLOCK 3/3，V2 因上一 Candidate 已产生新的 FIFO 角色证据而保守 ALLOW 3/3；V1/V2 waste block 分别为 3/3 和 0/3。没有 ESSENTIAL_STRUCTURAL 正样本，所以不能声称“100% structural essential retention”。

### OPTIMIZE

Decision coverage 17/20，其中 BENEFICIAL_PERFORMANCE=8、HARMFUL=9。V2 ALLOW 17/17，beneficial retention=8/8，waste block=0/9；策略极度保守，尚未形成节省。

## 9. 失败与限制

1. 数据严重偏向 OPTIMIZE。
2. REPAIR、SYNTH_FIX 无真实 follow-up。
3. STRUCTURAL_FIX 只有三条 harmful，没有 essential 正样本。
4. 旧 R02 缺少干净的 decision-time budget，只能保留 UNKNOWN。
5. V2 在现有数据上全 ALLOW，说明 false-block 保护有效，但 stop/waste 识别未得到证据。
6. 当前指标不能代表 28 题、多模型或 hidden 成绩。

## 10. Shadow Pilot 建议

当前不允许 Shadow Pilot。下一阶段只能先做数据补强设计：在不启用 Enforce 的前提下，收集真实 Shadow decision records，优先补齐 REPAIR、SYNTH_FIX 和 ESSENTIAL_STRUCTURAL；预注册固定规则与分组，待 structural essential retention 有非零分母且 waste block 不退化后再申请准入。

## 11. 正式权限边界

- Continuation V1：IMPLEMENTED / SHADOW。
- Continuation V2：OFFLINE_ONLY。
- V2 Shadow Pilot：NOT_READY。
- Continuation Enforce：DISABLED。
- Experience Guided：NOT_ADMITTED。
- Ranker Training：NOT_READY。

主 Graph、BudgetLedger、Candidate promotion、Patch Validator、TopInterfaceGuard 和任务语料均未修改。

验证结果：聚焦 Continuation V1/V2/replay 测试 27/27 PASS；全量 unittest 572/572 PASS；compileall PASS；`git diff --check` PASS。五个核心生成 Artifact 在相同输入下重跑 hash 5/5 一致。

## 12. 证据索引

证据目录：`docs/experiments/artifacts/2026-07-23-phase-c0-continuation-v2/`

- `current-continuation-audit.json` / `.zh-CN.md`
- `dataset-inventory.json`
- `replay-dataset-v2.jsonl`
- `replay-exclusions-v2.jsonl`
- `policy-v1-results.jsonl`
- `policy-v2-results.jsonl`
- `policy-comparison.json` / `.zh-CN.md`
- `unit-tests.txt`
- `compileall.txt`
- `git-diff-check.txt`
- `initial-git-state.txt`
- `final-git-state.txt`
- `acceptance-metadata.json`

本阶段真实 LLM calls=0、真实 Token=0、CSim/Synth/CoSim=0/0/0、Tool Credits=0；未访问 hidden/reference/golden。
