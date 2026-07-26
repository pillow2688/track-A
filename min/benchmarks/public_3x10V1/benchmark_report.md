# FPT 2026 Track A — Phase V1 最终验收报告

## 结论

`ACCEPTED`。Preflight 与正式 Gate 均通过；正式 30/30 Slot 均为一次性运行、
唯一终态、无 retry、无失败替换。横向组件全部关闭，公开任务源码、header、
testbench 与 metadata 哈希未变。

V1 是基础接口可靠性修复，不是 Agent 智能增强。目标成功为
24/30 (80.0%)，
Wilson 95% CI 为 62.69%–
90.49%；流程成功 30/30。

## 正式 3×10

| 任务 | 流程成功 | 目标成功 | 目标 Wilson 95% CI | Hardware qualified | qualified Wilson 95% CI | 平均 Token | 平均 Credits | 平均时间 | 平均得分 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| projection_bugfix | 10/10 (100.0%) | 10/10 (100.0%) | 72.25%–100.00% | 10/10 (100.0%) | 72.25%–100.00% | 2801.0 | 6.0 | 43.0s | 1.4000 |
| dotProduct_optimize | 10/10 (100.0%) | 4/10 (40.0%) | 16.82%–68.73% | 2/10 (20.0%) | 5.67%–50.98% | 3215.5 | 9.0 | 89.1s | 2.4038 |
| residual_stream_deadlock | 10/10 (100.0%) | 10/10 (100.0%) | 72.25%–100.00% | 10/10 (100.0%) | 72.25%–100.00% | 3075.4 | 50.0 | 118.3s | 3.0978 |
| overall | 30/30 (100.0%) | 24/30 (80.0%) | 62.69%–90.49% | 22/30 (73.3%) | 55.55%–85.82% | 3030.6 | 21.7 | 83.4s | 2.3005 |

DotProduct 的 4 次目标成功中，2 次满足 5 ns hardware qualification；另 2 次
raw worst latency 为 35 cycles，但估算时钟周期为 31.133 ns，因此只计目标成功，
不计 hardware qualified。

## LLM 可靠性

- 请求/成功/receipt：30/30/30
- usage known/unknown：30/0
- 已知 Token 总数与 recorded-token lower bound：90919
- 已知调用 Token 均值：3030.6333
- EMPTY_RESPONSE=0，
  TRUNCATED_JSON=2，
  INVALID_JSON=0，
  INVALID_SCHEMA=0

两次 TRUNCATED_JSON 均保留 envelope、raw content、request ID、finish reason
和完整 usage，并进入 Ledger；没有第二次 LLM 修复调用。这里的 raw content 是
provider message 的原始 content，完整脱敏 response envelope 另行保存。

## Patch 可靠性

- raw strict=5
- normalized strict=7
- exact-context recovery=13
- unique-delete-block recovery=3
- zero-match=3，ambiguous-match=0
- fuzzy recovery=0，最终 Patch 失败=0

28 份 Patch 全部保存 raw/normalized/applied 与 SHA；独立严格重放 28/28
一致。13 次完整 old-side context 与 3 次删除块恢复都只有一个精确命中；
无 first-match、模糊匹配或全文件覆盖。

## Budget 与工具

- Ledger↔final result：30/30
- Ledger↔budget_state：30/30
- state 与 result 全对象相等：30/30
- pending credits/tokens：0/0
- 工具次数：LLM=30，CSim=58，
  Synth=48，CoSim=20
- Credits：650（平均 21.6667）

## 修复前后

| 指标 | 修复前 | V1 | 变化 |
|---|---:|---:|---:|
| Projection 目标成功 | 7/10 | 10/10 | +3 |
| DotProduct 目标成功 | 4/10 | 4/10 | +0 |
| Residual 目标成功 | 10/10 | 10/10 | +0 |
| 整体目标成功 | 21/30 | 24/30 | +3 |
| 流程成功 | 27/30 | 30/30 | +3 |
| Planner JSON 失败 | 3 | 2 | -1 |
| Patch 格式/定位失败 | 4 | 0 | -4 |
| Token usage 未知 | 3 | 0 | -3 |
| Budget 快照不一致 | 原报告 4；复核 7 | 0 | −7（复核口径） |
| Hardware qualified | 21/30 | 22/30 | +1 |
| Credits | 625 | 650 | +25 |

旧报告的 Budget “4 次”只列了部分显性案例。此次直接重放旧 Ledger 后，
排除动态 runtime 字段的稳定记账字段实际为 7/30 不一致；若全对象比较，
旧快照因 runtime 字段为 30/30 不相等。V1 使用更严格的全对象口径，
30/30 相等。

Credits 增加 25 来自 CSim 53→58 与 Synth 43→48；单价未变。更多 Patch
被安全落地后进入了完整候选验证链路。

## 归因

- JSON 可靠性修复：解决的是“失败也有完整响应与 usage、明确分类和正确记账”。
  2 次真实截断仍按正式失败保留；3→2 还包含模型随机波动。
- Patch 落地修复：4→0；28 个 Patch 中 23 个依赖规范化或确定性精确恢复。
  Projection 7/10→10/10 与该修复一致，但新旧是独立随机样本，不能做逐样本
  反事实因果断言。
- 真正策略变化：0。Planner Prompt 模板、轮数、模型、工具策略、成功定义、
  Credits 单价和横向组件状态均未改变。
- 模型随机波动：DotProduct 目标成功仍为 4/10；JSON 截断数、具体 Patch
  形态与 hardware qualification 的变化均包含随机性。

## 最终检查

聚焦测试 34/34、完整 harness 测试 648/648、compileall、diff check 和
Secret 扫描全部 PASS。Secret 扫描覆盖 24,511 个文件、2,235,806,236
字节；未发现 API Key、Authorization bearer 或其他匹配项。未访问
hidden/reference/golden，未 push，未创建 PR。
