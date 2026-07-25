# Phase C1.1 短收口验收报告

## 验收结论

**通过，按 Fail-Closed 规则关闭正式矩阵中的 Experience/Ranker Runtime。**

这不是 Experience 或 Ranker 获得正式准入，而是它们的安全边界已被证明并固化。
当前最重要的验收结果是：不能只凭“Shadow 不注入提示词”就宣称与 off 完全等价。

## 验收项

| 验收项 | 结果 | 说明 |
| --- | --- | --- |
| 安全白名单 | PASS | Query 顶层与嵌套结构特征均为 allow-list |
| 标签分层 | PASS | 决策前、历史 Support、Held-out、终态归因四层隔离 |
| Cost Scope | PASS | 当前 Cost 只由 Ledger/Policy 授权 |
| Provenance 分池 | PASS | 103 = 76 Support + 27 Observation |
| 固定策略 off/shadow | PARTIAL | Request 相同；fingerprint/副作用不同 |
| 动态策略 off/shadow | FAIL_EQUIVALENCE | TokenEnvelope、Context、Request 不同 |
| Ranker V2 主路径隔离 | PASS_BY_NON_INTEGRATION | Runtime import 为 0 |
| C2 阈值保持 | PASS | 固定阈值 SHA-256 未变化 |
| 正式矩阵 Runtime Gate | PASS_FAIL_CLOSED | Continuation/Experience/Ranker 均 off |

## 为什么不能保留 Experience Shadow

固定 Token 策略下，当前测试确实证明了 Provider Request 相同；但正式矩阵要求的是
“完全不改变 Planner 输入和主路径”，还需要考虑运行身份、Checkpoint/Resume
兼容性和审计副作用。Shadow 会改变 Planner fingerprint，并写 Recommendation
审计记录，已经不满足完全等价。

更直接的是，动态 Token 策略会为 Shadow guidance 预留 Token，即使 Guidance
最终没有注入 Prompt，仍会改变 TokenEnvelope 和最终 Provider Request。因此按用户
指定的 Gate，只能关闭。

## 固定后的矩阵边界

```text
正式矩阵：
  Continuation off
  Experience off
  Ranker off

矩阵结束后：
  新 Experience Records 统一导入
  Continuation offline replay
  Ranker fixed-protocol reevaluation
  同批矩阵数据不用于调阈值后再报同批成绩
```

## 证据入口

- 机器审计：
  `docs/experiments/artifacts/2026-07-24-phase-c11-short-closure/c11-audit-result.json`
- 可复现实验脚本：
  `docs/experiments/artifacts/2026-07-24-phase-c11-short-closure/c11_offline_audit.py`
- 行为测试日志：
  `docs/experiments/artifacts/2026-07-24-phase-c11-short-closure/c11-focused-tests.txt`

