# 2026-07-25 横向组件真实在线 Shadow 状态

## 结论

四类公开 Anchor 已完成真实 DeepSeek + Vitis 2025.2 闭环，修正 REPAIR
的 final policy 后，`REPAIR / SYNTH_FIX / STRUCTURAL_FIX / OPTIMIZE`
最终均为 `DONE`。这证明横向组件已经接入真实执行路径，不是“没有加”。

但是，截至本状态冻结时，Continuation V2 与 Experience/Strategy Ranker V3
仍然只有 `SHADOW` 权限，**没有获得 Enforce/Guided admission**：

- Continuation 固定 Gate：`FAIL`；
- Ranker V3 固定 Gate：`FAIL`；
- Continuation admission：未生成；
- Ranker admission：未生成；
- 主 Graph 在线控制权限：未放开。

不能把“四类 Anchor 主流程成功”误写成“横向组件 Gate 已通过”。Gate
失败时继续 fail-closed，是当前实现符合设计的表现。

## 本次授权与边界

- 用户明确允许本次继续使用已披露的 API key，并允许把约定的四个公开
  Anchor 的源码、公开测试信息和 Vitis 诊断发送给外部 DeepSeek API。
- 密钥值没有写入运行报告、Experience、状态文件或 admission。
- 未读取或发送 hidden、reference、golden。
- 未启动 28 题矩阵。
- Experience/Ranker 在运行中保持 Shadow；没有修改 Planner 请求或主路径。

## 四类 Anchor 最终结果

| Mode | Task | 最终状态 | LLM calls | Tokens | Credits | fresh final |
|---|---|---:|---:|---:|---:|---:|
| OPTIMIZE | `dotProduct_optimize` | DONE | 2 | 4,825 | 40 | PASS |
| REPAIR | `projection_bugfix` | DONE | 1 | 2,040 | 11 | PASS |
| STRUCTURAL_FIX | `residual_stream_deadlock` | DONE | 1 | 2,143 | 71 | PASS |
| SYNTH_FIX | `u55c_synthesis_repair` | DONE | 1 | 1,730 | 35 | PASS |

合计：

- 真实 LLM calls：5；
- Tokens：10,738；
- Credits：157；
- CSim：13 次；
- Synth：11 次；
- CoSim：5 次；
- 四类最终成功：4/4。

### REPAIR 预算修正

第一次 pilot 对全部任务统一使用 `full_internal_audit`，`projection_bugfix`
在任何工具或模型调用前被
`BASELINE_AND_FINAL_CLOSURE_UNAFFORDABLE` 正确拦截。该题官方预算只承诺
`task_contract`，因此只补跑这一题并改用 `task_contract`：

- 没有重跑另外三题；
- 没有扩大任务预算；
- 1 次真实 DeepSeek 调用；
- CSim/Synth fresh final 均 PASS；
- 最终 E2E PASS。

## Continuation V2 固定 Gate

当前正式证据只使用与现行 V2 逻辑一致的四个成功 Anchor：

- 有效 follow-up 样本：1；
- OPTIMIZE：1；
- REPAIR：0；
- SYNTH_FIX：0；
- STRUCTURAL_FIX：0；
- STRUCTURAL_FIX essential：0；
- leakage violations：0。

唯一 follow-up 样本中，V2 为 `ALLOW`，后续候选没有严格改善，因此结果
标记为 `HARMFUL`。固定 Gate 失败项：

1. `PER_MODE_SAMPLE_GATE`；
2. `STRUCTURAL_ESSENTIAL_SAMPLE_GATE`；
3. `STRUCTURAL_ESSENTIAL_RETENTION_GATE`；
4. `WASTE_BLOCK_RATE_REGRESSION`。

### 已否决的一次过度修复

曾试验“首轮已有显著改进且待 final 时直接 `DEFER_TO_FINAL`”。聚焦测试
通过后，对同一公开 OPTIMIZE Anchor 做了独立在线 Shadow 复核。该次第二轮
实际又产生有效性能提升，达到 27.026× baseline；新规则会形成 false block。

因此该规则已撤销，产品代码没有保留这个过度拟合修复。两次真实运行说明：
仅凭当前这组 pre-state，未来 Planner 提案既可能有害，也可能显著有益；在
没有新增可判别的决策前事实或更多独立样本时，不能安全 Enforce。

## Experience V2 与 Ranker V3 固定 Gate

四类 Anchor 通过既有脱敏导入器生成 5 条 Experience V2：

- 4 条真实 final PASS；
- 1 条未成为 final 的 OPTIMIZE 候选；
- 0 次额外 LLM；
- 0 次额外 Vitis；
- 不保留源码、Patch 正文、Prompt、日志、密钥或本地绝对路径。

与冻结审计池合并后：

- 总记录：139；
- ranking eligible：107；
- 真实 final PASS/FAIL：92；
- 未验证记录：15；
- verified by mode：
  - OPTIMIZE：23；
  - REPAIR：33；
  - SYNTH_FIX：26；
  - STRUCTURAL_FIX：10。

STRUCTURAL_FIX 已从 9 增至 10，`minimum_records_per_mode` Gate 已通过。
固定 LOTO + leave-one-task-family-out 结果：

| 指标 | LOTO | Leave-one-family-out | Gate |
|---|---:|---:|---:|
| coverage | 2.17% | 2.17% | ≥ 40% |
| harmful rate | 0% | 0% | ≤ 5% |
| positive strategy hit | 0% | 0% | ≥ 27.27% |
| leakage | 0 | 0 | 0 |

最终结论：`FAIL / SHADOW_ONLY`。失败原因是跨独立 task-family 的安全策略
支撑不足，不是开关未接通。禁止通过降低 fixed threshold、把 `NOT_RUN`
改标为成功、重复计算同一 family 或强制非安全推荐来制造 PASS。

## 当前权限矩阵

| 组件 | Shadow | 正式权限 | 当前状态 |
|---|---:|---:|---|
| Continuation V2 | 已真实运行 | Enforce | Gate FAIL，禁止 |
| Experience V2 | 已真实运行并离线导入 | Guided | Gate FAIL，禁止 |
| Strategy Ranker V3 | 已固定协议重评 | Prompt injection | Gate FAIL，禁止 |
| Candidate / Budget / Final Gate | 已真实运行 | 主路径控制 | 保持原正式逻辑 |

## 尚缺的真实证据

1. Continuation 需要 REPAIR、SYNTH_FIX、STRUCTURAL_FIX 的自然 follow-up
   decision point，并至少需要 1 条 STRUCTURAL_FIX
   `ESSENTIAL_FOR_CORRECTNESS` 样本。
2. 当前四个 Anchor 中后三类均首轮成功，不会自然产生第二轮样本；不能为了
   过 Gate 强迫一次无必要的 Planner 调用。
3. Ranker 需要更多跨独立 task-family、真实 final PASS/FAIL 的高置信策略
   记录；重复同一 Anchor 不能增加 family-level 支撑。
4. 在当前只允许四个公开 Anchor 外发的边界内，无法合法补齐上述数据。

## 工程验证

- 标准完整单元测试：626 tests，全部通过；
- `compileall`：通过；
- `git diff --check`：通过；
- 本阶段状态、Artifact 和三次在线运行密钥扫描：未发现 API key 落盘；
- 两个 Gate 均为 FAIL 时，对应 admission 文件均不存在。

## 证据地址

- `llm4hls_harness/experiments/horizontal_online_shadow_20260725_a01/`
- `llm4hls_harness/experiments/horizontal_online_shadow_20260725_repair_a01/`
- `llm4hls_harness/experiments/horizontal_online_shadow_20260725_optimize_v2fix_a01/`
- `docs/experiments/artifacts/2026-07-25-horizontal-online-shadow/continuation-v2-current-gate.json`
- `docs/experiments/artifacts/2026-07-25-horizontal-online-shadow/ranker-v3-fixed-protocol.json`
- `docs/experiments/artifacts/2026-07-25-horizontal-online-shadow/combined-experience-manifest.json`
- `docs/experiments/artifacts/2026-07-25-horizontal-online-shadow/four-anchor-experience-v2/`
