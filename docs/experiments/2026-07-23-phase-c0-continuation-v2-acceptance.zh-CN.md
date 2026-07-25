# Phase C0 验收：Mode-Specific Continuation V2 离线 Shadow 试验

## 1. 验收结论

阶段实现结论：**DONE**  
数据与准入结论：**INSUFFICIENT_EVIDENCE**  
V2 Shadow Pilot：**NOT_READY**  
Continuation Enforce：**DISABLED**

V2 纯函数、回放数据、泄漏隔离、V1/V2 对照和单元测试均已实现；但真实回放没有覆盖 REPAIR/SYNTH_FIX，也没有 ESSENTIAL_STRUCTURAL 正样本，且 waste block rate 从 V1 的 5/12 退化为 V2 的 0/12。因此不能验收为 `V2_SHADOW_PILOT_CANDIDATE`。

## 2. 阶段目标验收

| 验收项 | 结果 | 证据 |
|---|---|---|
| 审计当前 V1 | PASS | `current-continuation-audit.json` |
| 构造 decision-point 数据 | PASS | 21 候选点、20 绑定、1 follow-up 排除 |
| 严格分离 pre_state/outcome | PASS | `replay-dataset-v2.jsonl` |
| 实现独立 V2 | PASS | `v3_continuation_v2.py` |
| V1 保留 | PASS | V1 文件未修改 |
| 主 Graph 不变 | PASS | `v3_prototype.py` 未修改 |
| V1/V2 离线对照 | PASS | `policy-comparison.json` |
| 四 Mode 真实回放 | FAIL / DATA GAP | REPAIR=0、SYNTH_FIX=0 |
| Shadow Pilot 准入 | FAIL | essential structural 0/0；waste 指标退化 |
| Enforce | DISABLED | 本阶段禁止 |

## 3. 数据来源与绑定

纳入来源为旧 V3-F R02、72-run Token Policy A/B/C、Phase B1/B1.1/B1.2 Anchor、Experience Candidate records 和当前 Continuation candidate dataset。只把“一个可绑定的 follow-up Planner decision point”计为样本。

| 项目 | 数量 |
|---|---:|
| 来源筛查 | 101 |
| follow-up 候选点 | 21 |
| 完整绑定 | 20 |
| follow-up 排除 | 1 |
| 全部筛查排除 | 81 |
| REPAIR / SYNTH_FIX / STRUCTURAL_FIX / OPTIMIZE | 0 / 0 / 3 / 17 |

排除原因全部来自允许枚举；没有为扩大样本填补未知字段。Experience 的 81 条 Candidate records 只作交叉核对，没有重复计数。

## 4. 信息泄漏验收

| 检查 | 结果 |
|---|---|
| Policy 只接收 `mode + pre_state` | PASS |
| follow-up response/Patch 不可见 | PASS |
| future Candidate/promotion/tool/final 不可见 | PASS |
| outcome/future metrics 不可见 | PASS |
| 注入 future 字段不改变 decision/digest | PASS |
| hidden/reference/golden 未访问 | PASS |

旧 R02 的终态 budget 风险已经明确记录并从 V2 数据中剔除；缺失值保持 UNKNOWN，不以 0 或终态值替代。

## 5. V2 规则验收

### REPAIR

- error subtype/location/progress/new strategy 后 ALLOW：PASS（单测）。
- 同错误、位置、策略且连续无进展才停止：PASS（单测）。
- 一次失败不会 BLOCK：PASS（单测）。
- 真实回放：INSUFFICIENT_EVIDENCE（0）。

### SYNTH_FIX

- stage advance/unsupported 消失/新 scheduling-memory evidence 后 ALLOW：PASS（单测）。
- 相同 unsupported/location/strategy 连续无进展才停止：PASS（单测）。
- 真实回放：INSUFFICIENT_EVIDENCE（0）。

### STRUCTURAL_FIX

- 新 FIFO、deadlock location 或结构 Evidence 后仍可 ALLOW：PASS（单测）。
- 只有全部重复且 reserve safe 才可能 BLOCK：PASS（单测）。
- BLOCK confidence 不高于 MEDIUM：PASS（单测）。
- 真实 essential retention：INSUFFICIENT_EVIDENCE（0/0）。

### OPTIMIZE

- 显著 latency、II/interval/clock、bottleneck 或新策略后 ALLOW：PASS（单测）。
- 低边际收益+重复策略+incumbent 后 DEFER：PASS（单测）。
- 8× cap 后 DEFER：PASS（单测）。
- resource 缺失保持 UNKNOWN：PASS（单测）。

## 6. V1/V2 指标验收

| 指标 | V1 | V2 | Gate |
|---|---:|---:|---|
| Beneficial/essential retention | 4/8（50.0%） | 8/8（100.0%） | PASS |
| Essential structural retention | 0/0 | 0/0 | FAIL / INSUFFICIENT |
| Waste block rate | 5/12（41.7%） | 0/12（0.0%） | FAIL / REGRESSION |
| False block | 4/8 | 0/8 | PASS |
| Offline potential Token saving | 12,189 | 0 | REGRESSION |
| Offline potential Credit saving | 73 | 0 | REGRESSION |

V2 避免了当前数据上的有益误杀，但没有证明能停止无效调用。不能只依据 8/8 retention 忽略 waste 和结构正样本分母。

## 7. Leave-one-group-out

- Calibration：NONE。
- Leave-one-run-out：20/20 一致。
- Leave-one-task-out：9/9 task group 一致。
- Split digest：`a86232bb46d3c28286af0505c99d07fe1a2c268a2af82a5f1302ecdaad5cb6f4`。

规则没有使用 task/run/outcome 调参；但由于它本身是 context-free 纯函数，此结果只能作为稳定性与无组依赖证据。

## 8. Shadow Pilot 最低条件核对

| 最低条件 | 结果 |
|---|---|
| 有益或必要调用 false BLOCK=0 | PASS（0/8） |
| STRUCTURAL essential retention=100% | FAIL（0/0，不可测） |
| beneficial retention 不低于 V1 | PASS（100% > 50%） |
| waste block 不退化 | FAIL（0% < 41.7%） |
| 无 future leakage | PASS |
| leave-one-group-out 稳定 | PASS（限定解释） |
| 所有测试通过 | PASS：聚焦 27/27；全量 572/572 |
| 正式权限保持 Shadow | PASS |

综合结果：`INSUFFICIENT_EVIDENCE`，不得写 `V2_SHADOW_PILOT_CANDIDATE`。

## 9. 工程边界

- 新增产品代码：独立 `v3_continuation_v2.py`。
- 新增测试：`test_v3_continuation_v2.py`。
- V1：未修改。
- 主 Graph：未修改。
- BudgetLedger、Candidate lifecycle/promotion、Patch Validator、TopInterfaceGuard、task corpus：未修改。
- Continuation V2 没有接入主路径。

## 10. 资源与合规

本阶段真实 LLM calls=0、真实 Token=0、CSim/Synth/CoSim=0/0/0、Tool Credits=0。没有启用 Continuation Enforce、Experience Guided 或 Ranker Training；没有启动 28 题、多模型或 hidden 评测；没有 commit、push 或 PR。

## 11. 未完成项与下一 Gate

1. 获取 REPAIR 和 SYNTH_FIX 的真实 follow-up decision records。
2. 获取非零 `ESSENTIAL_STRUCTURAL` 正样本。
3. 预注册不读取 outcome 的停止规则，使 waste block 至少不低于 V1。
4. 重做按 task/run 分组的离线对照。
5. 只有全部最低条件满足后，才申请“真实 Shadow 记录试点”；仍不得直接启用 Enforce。
