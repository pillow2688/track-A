# Phase C2：Bayesian Strategy Ranker V2 离线评估报告

## 1. 实验目标

在不改变主流程和正式权限的前提下，验证一个 Mode-specific、条件化、带安全
ABSTAIN 的 Bayesian Strategy Ranker V2，判断它是否比现有全 ABSTAIN Shadow
baseline 更接近可审查候选。

C2 使用 C1 已验证的 103 条 Experience V2 冻结记录，其中 76 条满足真实证据和
ranking eligibility。

## 2. 实现

V2 对每个策略 atom 计算 Beta posterior，并优先使用
`mode + subtype + algorithm family` 条件证据；当算法族支持不足时只回退到
`mode + subtype`。候选还必须同时满足：

- 至少 3 次尝试、3 个独立 run；
- 至少 2 个 task family 和 2 个 patch digest；
- 成功证据至少跨 2 个 family；
- posterior ≥ 0.65；
- 80% 单侧近似可信下界 ≥ 0.45；
- 第一与第二候选可信下界 margin ≥ 0.05。

这些值在第一次正式 holdout 评估前固定，之后没有因结果而放宽。

## 3. 泄漏隔离

每个 held-out query 都先从 source/problem/structure 构造查询并完成排序，然后才
读取 held-out 的 observed strategy 与 outcome 评分。查询不含 validation、
performance、cost、promoted、rejected 或 outcome。

分别执行 Leave-One-Run、Leave-One-Task 和 Leave-One-Task-Family。task audit
hash 只用于剔除 support，不进入 Ranker 特征。三种评估的 group leakage 均为 0。

## 4. 结果

V1 正式 guidance baseline 在同一数据上为全 ABSTAIN。V2 的 Shadow coverage：

```text
Leave-One-Run         31/76 = 40.79%
Leave-One-Task        33/76 = 43.42%
Leave-One-Task-Family 33/76 = 43.42%
```

Leave-One-Task 与 Leave-One-Task-Family 均得到：

```text
positive strategy hit = 23/62 = 37.10%
harmful duplicate recommendation = 1/33 = 3.03%
failed strategy suppression = 13/14 = 92.86%
```

但总平均掩盖了 Mode-specific 风险：

```text
OPTIMIZE recommendations = 8/26
OPTIMIZE positive hits = 1/12 = 8.33%
OPTIMIZE harmful duplicate recommendations = 1/8 = 12.50%
STRUCTURAL_FIX recommendations = 0/6
```

因此总体 coverage 和总体 harmful rate 虽达到门槛，单模式 harmful Gate 失败。

## 5. 负结果判定

C2 判定为 `NEGATIVE_RESULT`，不是 `PASS_PROMOTABLE`：

- Ranker 是 Mode-specific，必须逐 Mode 检查风险；
- OPTIMIZE 12.5% 超过固定 5% Gate；
- STRUCTURAL_FIX 样本数与 coverage 均不足；
- 继续调 held-out threshold 会破坏实验有效性。

产品候选和测试保留用于人工分析，但不连接主 Graph，不晋升 Shadow Pilot，不启用
Guided。回退继续使用 V1、现有确定性策略和 ABSTAIN。

## 6. 验证

```text
C2 focused tests = 19/19 PASS
Full suite = 579/579 PASS
compileall = PASS
git diff --check = PASS
holdout leakage = 0
```

## 7. 后续数据需求

优先补充：

1. STRUCTURAL_FIX 至少补到 10 条可排名记录，且需跨多个 task family；
2. OPTIMIZE 的失败/无提升反例，尤其同 subtype 下策略替代证据；
3. 非 OPTIMIZE 模式的失败样本，降低仅成功记录造成的选择偏差。

不得用当前 76 条 held-out 记录继续调阈值；新增独立数据后重新冻结评估协议。

## 8. 当次预算

```text
真实 LLM calls = 0
真实 Token = 0
CSim = 0
Synth = 0
CoSim = 0
Tool Credits = 0
```

