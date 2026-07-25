# 2026-07-23 横向组件统一状态看板

冻结 HEAD：`4a05763b593a527878a0056f64763126c58ee63b`  
C3 状态：`PASS_PROMOTABLE`（文档与证据层通过，不改变组件权限）

| 组件 | 状态 | 测试 | 数据 | 离线效果 | 是否晋升 | 当前权限 | 阻塞原因 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| C0 Continuation V2 | `INSUFFICIENT_EVIDENCE` | 聚焦 27/27；全套 572/572 | 21 候选点、20 完整绑定；OPTIMIZE 17、STRUCTURAL_FIX 3 | beneficial 8/8 保留；waste block 5/12→0/12；structural essential 0/0 | 否 | SHADOW；Enforce DISABLED | Mode/essential 样本不足且 waste 指标退化 |
| C1 Experience Record V2 | `PASS_PROMOTABLE` | 聚焦 41/41；全套 574/574 | 103/103 有效；76 可排名；72 runs；25 families | canonical/唯一性/公开边界全通过 | 只进入人工数据审查 | SHADOW；Guided NOT_ADMITTED | 无工程阻塞；STRUCTURAL_FIX 仅 6 条可排名 |
| C2 Bayesian Strategy Ranker V2 | `NEGATIVE_RESULT` | 聚焦 19/19；全套 579/579 | 使用 C1 的 76 条可排名记录 | LOTO coverage 43.42%；总体 harmful 3.03%；OPTIMIZE harmful 12.5% | 否 | SHADOW；Learned TRAINING_NOT_READY | OPTIMIZE 超过 5% Gate；STRUCTURAL_FIX 0/6 推荐 |
| C3 统一审计 | `PASS_PROMOTABLE` | 最终全套 579/579 | 汇总 C0/C1/C2 Artifact | 权限、泄漏、预算与补丁索引已冻结 | 文档层完成 | 不新增运行时权限 | 无 |

## 正式权限

```text
Continuation authority = SHADOW
Continuation Enforce = DISABLED

Experience authority = SHADOW
Experience Guided = NOT_ADMITTED

Bayesian Ranker authority = SHADOW
Learned Ranker = TRAINING_NOT_READY
```

## 全局安全

- future outcome：C0 V2 输入已剔除旧终态 budget；C2 query 不含 held-out
  strategy/outcome，label 在 decision 后读取。
- hidden/reference/golden：没有读取或用作 support。
- 秘密扫描：未发现长格式 `sk-` Token、API Key 或 Authorization 被写入新 Artifact。
- 主 Graph、BudgetLedger、Candidate correctness Gate、fresh final：未修改。
- 当天离线队列真实 LLM/Vitis/Token/Tool Credits：全部 0。

## 2026-07-24 正式矩阵后固定协议更新

这部分是新增证据，不回写或覆盖 2026-07-23 的冻结统计。正式 28×1 运行期间三项
横向组件均为 off；运行结束后才做隔离的离线更新。

| 组件 | 新数据 | 固定协议结果 | 权限 |
| --- | --- | --- | --- |
| C0 Continuation | 正式矩阵新增 7 个可绑定 decision；合并后 27 个 | `INSUFFICIENT_EVIDENCE`；无 SYNTH_FIX follow-up、无 essential structural outcome | V1 SHADOW；V2 OFFLINE_ONLY；Enforce DISABLED |
| C1 Experience | 新增 31 条；与旧 103 条分池合并为 134 条；102 eligible | `PASS_PROMOTABLE`；134/134 valid，四 Mode 覆盖 | SHADOW；Guided NOT_ADMITTED；Planner ingestion=false |
| C2 Strategy Ranker | 使用合并后的 102 eligible，阈值不变 | `NEGATIVE_RESULT`；LOTO/LO-family coverage 38.24%，harmful 15.38% | SHADOW；Learned TRAINING_NOT_READY；runtime off |

新数据的 Mode 分布：

```text
all records: REPAIR 43 / SYNTH_FIX 25 / STRUCTURAL_FIX 11 / OPTIMIZE 55
rank eligible: REPAIR 32 / SYNTH_FIX 25 / STRUCTURAL_FIX 9 / OPTIMIZE 36
```

C2 固定 Gate 仍未满足：两种主要 holdout 的 coverage 低于 40%、有害重复推荐率高于
5%，且 STRUCTURAL_FIX eligible 只有 9 条，低于每 Mode 10 条的最低要求。本轮
没有根据新数据调阈值。

完整机器证据位于
`docs/experiments/artifacts/2026-07-24-post-matrix-horizontal-update/`，中文验收见
`docs/experiments/2026-07-24-formal-matrix-results-and-horizontal-update-acceptance.zh-CN.md`。
