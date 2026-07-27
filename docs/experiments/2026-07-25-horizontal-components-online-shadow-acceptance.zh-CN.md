# 2026-07-25 横向组件真实在线 Shadow 验收报告

> 历史冻结快照：本文记录的是 admission 生成前的 Shadow 验收。当前结论请
> 查看
> [2026-07-26 E2E correctness 收口验收报告](2026-07-26-e2e-correctness-recovery-acceptance.zh-CN.md)。

## 验收结论

本阶段验收分成两个层次：

1. **真实在线接线验收：PASS**
   - 四类公开 Anchor 均完成 DeepSeek + Vitis 2025.2 E2E；
   - Continuation V2、Experience V2、Ranker V3 均在真实运行中产生
     Shadow 证据；
   - Shadow 不改变 Planner 输入和主路径；
   - Gate 失败时无法生成 admission，不能绕过。
2. **正式控制准入验收：FAIL**
   - Continuation 固定 Gate 未通过；
   - Ranker 固定 Gate 未通过；
   - Enforce/Guided 均未启用。

因此，本阶段不能验收为“横向组件已全部正式启用”，但可以验收为“真实在线
Shadow 闭环已跑通，准入保护有效，并已准确定位剩余数据缺口”。

## 验收项

| 验收项 | 结果 | 证据 |
|---|---:|---|
| 仅运行四个公开 Anchor | PASS | pilot plan 与 batch selection |
| 未启动 28 题 | PASS | `does_not_launch_28_tasks=true` |
| DeepSeek 真实调用 | PASS | 5 calls / 10,738 tokens |
| Vitis 2025.2 真实执行 | PASS | CSim 13 / Synth 11 / CoSim 5 |
| REPAIR 最终闭环 | PASS | `task_contract` 补跑 E2E PASS |
| SYNTH_FIX 最终闭环 | PASS | fresh CSim/Synth/CoSim PASS |
| STRUCTURAL_FIX 最终闭环 | PASS | baseline CoSim FAIL，修复后 PASS |
| OPTIMIZE 最终闭环 | PASS | 严格性能改善，fresh final PASS |
| Experience Shadow 等价 | PASS | prompt injection=false |
| Continuation V2 固定 Gate | FAIL | 1 个 OPTIMIZE follow-up，其余 Mode 缺样本 |
| Ranker V3 fixed protocol | FAIL | coverage 2.17%，positive hit 0% |
| Continuation admission | PASS（拒绝生成） | Gate FAIL 时 fail-closed |
| Ranker admission | PASS（拒绝生成） | Gate FAIL 时 fail-closed |
| hidden/reference/golden 隔离 | PASS | 均未访问 |
| 密钥写入产物 | PASS | 未写入 |
| 标准完整单元测试 | PASS | 626 tests |
| compileall / diff check | PASS | 均通过 |

## 关键发现

### 1. Anchor 成功不等于组件准入

四类任务最终都成功，只能证明主 Graph 和 Shadow 接线可运行。Continuation
是否有资格阻断下一轮、Ranker 是否有资格把建议注入 Planner，需要独立的
安全性和泛化证据。

### 2. Continuation 的剩余问题是可辨识性

同一 OPTIMIZE Anchor 的两次在线运行出现不同后续结果：

- 一次第二轮有害，V2 `ALLOW`；
- 一次第二轮显著有益。

试图根据“首轮显著改进”直接阻断，会误杀第二种情况。因此不能用一次样本
写一个特判后宣布 Gate PASS。

### 3. Ranker 的剩余问题是独立 family 多样性

新增 Anchor 使 STRUCTURAL_FIX 样本达到 10，解决了最小 Mode 数量 Gate，
但没有解决策略跨 family 泛化。V3 在证据不足时选择 ABSTAIN，安全但 coverage
仅 2.17%。若强制推荐或降低阈值，虽然数字会升高，却不满足固定协议。

## 下一步准入条件

继续正式准入前，需要用户明确允许把额外的公开任务源码与诊断发送给
DeepSeek。随后只运行定向的小任务集合，不启动 28 题：

1. 选择能自然产生二轮的 REPAIR、SYNTH_FIX、STRUCTURAL_FIX 公开任务；
2. 获得至少一条 STRUCTURAL_FIX essential follow-up；
3. 继续补充独立 task-family 的 final PASS/FAIL Experience；
4. 冻结新增数据后重跑同一固定 Gate；
5. 只有两个 admission 都生成后，才运行 Enforce/Guided A/B。

## 证据

- [在线 Shadow 状态](../status/2026-07-25-horizontal-components-online-shadow.md)
- `artifacts/2026-07-25-horizontal-online-shadow/continuation-v2-current-gate.json`
- `artifacts/2026-07-25-horizontal-online-shadow/ranker-v3-fixed-protocol.json`
- `artifacts/2026-07-25-horizontal-online-shadow/combined-experience-manifest.json`
- `artifacts/2026-07-25-horizontal-online-shadow/final-verification.json`
