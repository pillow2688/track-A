# 2026-07-24 横向组件在线准入状态

## 结论

横向组件已经接入可执行路径，但截至本状态冻结时，**尚未获得正式
Enforce/Guided 权限**。原因不是“没有加”，而是两个固定 Gate 的真实证据
仍未通过：

- Continuation V2：主 Graph、单题 CLI、批执行器和 enforce admission
  已接通；当前在线 V2 Shadow follow-up 样本为 0，Gate 为 `FAIL`。
- Experience V2 / Strategy Ranker V3：检索、排序、Shadow 审计、
  Guided admission 已接通；88 条真实终态数据的 fixed-protocol Gate 为
  `FAIL`，因此 Guided 被 fail-closed 拒绝。

任何 Gate 未通过时，主流程仍走现有确定性控制，不允许通过命令参数绕过。

## 当前权限矩阵

| 组件 | 工程路径 | 当前权限 | 正式控制权限 | 原因 |
|---|---|---:|---:|---|
| Continuation V2 | Graph + CLI + Batch | Shadow 可运行 | Enforce 禁止 | 缺真实四 Mode 在线 Shadow 样本 |
| Experience V2 | Planner sidecar + CLI + Batch | Shadow 可运行 | Guided 禁止 | Ranker V3 admission 不存在 |
| Strategy Ranker V3 | Experience V2 runtime coordinator | Shadow 可运行 | Prompt injection 禁止 | fixed-protocol Gate 未通过 |
| Experience V2 新记录 | 只读 frozen seed + 运行后离线导入 | 可审计 | 不允许在线自写训练集 | 防止同次运行自反馈和数据污染 |

## 已完成接线

1. Experience Shadow 与 Off 的 Planner 请求、TokenEnvelope、请求指纹和主路径
   保持等价；建议只写 sidecar。
2. Guided 只有在 Ranker V3 admission 与 frozen seed SHA-256 一致且全部固定
   指标通过时才允许进入 Planner context。
3. Continuation V2 使用决策前 allow-list 状态，当前/未来 Candidate outcome
   不可进入策略输入。
4. Continuation Enforce 只接受 V2 且必须绑定通过的 admission manifest。
5. 单题 CLI 与正式批执行器均显式传递组件模式、版本和 admission；
   frozen store/manifest 内容哈希进入执行器、批次和恢复身份。
6. manifest 在排程前校验；排程后文件内容变化会使执行失败，不能沿用旧
   checkpoint。
7. 批执行器的关键实现指纹已覆盖 Experience V2、Ranker V3、
   Continuation V2 与 admission validator。
8. 新增真实在线 Shadow 评估器：只接受真实 Vitis + V2 Shadow + 哈希一致
   gate，先重算 V1/V2，再读取后续 Candidate outcome。
9. 新增四 Mode 定向在线 pilot 执行器，不启动 28 题；Planner 轮数、
   no-improvement、fresh final、final fallback、组件版本与 frozen store
   全部进入批次/恢复身份。

## 当前 Gate 结果

### Strategy Ranker V3

- Experience V2 总记录：134
- ranking eligible：102
- 已有真实 final PASS/FAIL：88
- 未完成真实 final：14
- Mode 分布：
  - OPTIMIZE：22
  - REPAIR：32
  - SYNTH_FIX：25
  - STRUCTURAL_FIX：9
- Leave-One-Task coverage：0%
- Leave-One-Task-Family-Out coverage：0%
- harmful recommendation rate：0%
- leakage violations：0
- 结论：`FAIL / SHADOW_ONLY`

硬缺口：

1. STRUCTURAL_FIX 少于每 Mode 10 条的冻结门槛；
2. REPAIR、SYNTH_FIX、STRUCTURAL_FIX 的相同 subtype/strategy 多数只来自
   1–2 个独立 task family；
3. family-level Bayesian 支撑不足时全部 `ABSTAIN` 是正确的安全行为，
   不能改成强制推荐；
4. 不允许在当前 held-out 结果上降低 family、coverage 或 posterior 门槛。

### Continuation V2

- 新协议真实在线 follow-up 样本：0
- 四 Mode coverage：0/4
- STRUCTURAL_FIX essential 样本：0
- false block：0（无有效分母，不能作为通过证据）
- leakage violations：0
- 结论：`FAIL / SHADOW_ONLY`

当前历史回放不能替代新协议在线证据；尤其旧数据缺少 SYNTH_FIX 和
STRUCTURAL_FIX essential 的有效分母。

## 已执行验证

- 完整单元测试：625 tests，全部通过
- compileall：通过
- `git diff --check`：通过
- 产品代码与本阶段 Artifact 秘密扫描：通过
- Vitis 2025.2：可用
- 新增真实 Vitis final audit：6 个 Candidate
  - 2 个 CSim FAIL
  - 4 个 CSim/Synth PASS、CoSim FAIL
  - 真实 LLM calls：0
- Shadow pilot 执行器组合预检：
  - Continuation：`shadow / v2`
  - Experience：`shadow / v3`
  - frozen store：内容哈希已绑定
  - 公开任务：REPAIR / SYNTH_FIX / STRUCTURAL_FIX / OPTIMIZE 各 1
  - final policy：`full_internal_audit`
  - max Planner rounds：3
  - 28 题启动：否

## 当前外部阻塞

环境中 API key 与 endpoint 存在，但
`LLM4HLS_KEY_ROTATED_AFTER_DISCLOSURE=1` 缺失。此前密钥已在会话中明文
披露；在旧密钥撤销、换新并显式设置轮换标志前，不运行新的真实模型请求。

该条件已经连续三次在线预检复现；Vitis 2025.2、endpoint、model 和 key
存在性检查均通过，唯一失败项为密钥轮换标志。因此在线 Shadow、两个正式
Gate 以及 Enforce/Guided A/B 现处于外部状态阻塞，而非工程测试阻塞。

2026-07-25 用户已明确接受本次继续使用当前已披露密钥的风险；执行器已将
该授权记录为 `USER_ACCEPTED_DISCLOSED_KEY_RISK`，没有伪写为密钥已轮换。
首次真实 pilot 启动仍被宿主安全策略拒绝：Planner 会把四个公开 Anchor
的源码与运行诊断发送到外部 DeepSeek API，而当时授权尚未明确覆盖这项
外部数据传输。该次拒绝未产生模型调用、Token 或新运行目录，且未尝试绕过。

2026-07-25 后续更新：用户已经明确允许上述四个公开 Anchor 的外部数据
传输，真实 DeepSeek + Vitis Shadow 已执行。四类 Anchor 最终 4/4 成功，
但 Continuation 与 Ranker 固定 Gate 仍失败，因此 Enforce/Guided 仍未
启用。最新状态见
`docs/status/2026-07-25-horizontal-components-online-shadow.md`。

## 解锁后的最小在线顺序

1. 运行四 Mode 的定向 DeepSeek + Vitis V2 Shadow pilot，不启动 28 题；
2. 只收集第二轮及以后、结果可绑定的 follow-up decision point；
3. 运行固定 Continuation Gate；只有 `PASS` 才生成 enforce admission；
4. 按独立 task-family 缺口补 Experience V2 真实终态，不用同一任务重复数
   堆置信度；
5. 运行 LOTO + leave-one-task-family-out Ranker Gate；只有 `PASS` 才生成
   guided admission；
6. 分别运行 Off/Shadow、Shadow/Enforce、Shadow/Guided 的真实 A/B；
7. 只有安全性、正确率和最终成绩均无退化，才把权限改为正式启用。

## 证据

- `docs/experiments/artifacts/2026-07-24-horizontal-components-online-admission/ranker-v3-fixed-protocol-audited.json`
- `docs/experiments/artifacts/2026-07-24-horizontal-components-online-admission/continuation-v2-online-shadow-gate.json`
- `docs/experiments/artifacts/2026-07-24-horizontal-components-online-admission/fresh-candidate-audits-rerun01/`
