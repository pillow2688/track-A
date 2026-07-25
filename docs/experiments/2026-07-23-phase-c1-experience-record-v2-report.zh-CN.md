# Phase C1：Experience Record V2 离线审计报告

## 1. 目标与范围

C1 的目标不是重写既有 Experience 系统，而是确认当前
`v3e.experience.v2` 是否具备稳定 Schema、可复现记录、公开边界和可供 C2
离线使用的数据契约。

本阶段只新增 C1 专用审计脚本、冻结 Artifact、测试和文档。没有修改 Experience
运行时、主 Graph、BudgetLedger、Candidate、final 或任何策略权限。

## 2. 已有实现核对

当前 HEAD 已包含：

- 严格的 `v3e.experience.v2` Schema 与四模式 taxonomy；
- 稳定的 canonical record ID 和完整字段校验；
- V1→V2 normalizer、backfill、quarantine 与 snapshot；
- 检索、数据质量、ML export、LORO 和 learning readiness；
- 对秘密、绝对路径、task identity 和非声明字段的拒绝。

因此 C1 复用并审计现有实现，没有建立第二套不兼容 Schema。

## 3. 数据审计结果

103/103 条记录通过 `validate_experience_v2`。record ID 与
`run_id/candidate_id/round_index` 组合均无重复；原 Store 与逐条 canonical
重新序列化的 SHA-256 都是：

```text
ba452e1f57e433fae2727e614cb6ab2986ff4ae301762f852559bb909e004cb7
```

数据覆盖 REPAIR、SYNTH_FIX、STRUCTURAL_FIX、OPTIMIZE 四种 Mode，来自 72 个
历史运行和 25 个 task family。76 条记录可用于离线检索/排名。

现有质量缺口没有被隐藏：

- STRUCTURAL_FIX 可排名记录只有 6 条；
- 103 条记录全部标记为 train；
- 历史数据中 provider/model/prompt 与 failure/bottleneck subtype 仍存在 UNKNOWN；
- 数据可用不等于 Guided 效果已经证明。

## 4. 泄漏边界

C1 是记录质量审计，因此会读取完整历史 outcome 来验证记录一致性，但没有产生
策略决策。为避免 C2 future leakage，冻结摘要显式规定：查询特征不能包含
`validation`、`performance`、`cost`、outcome、promoted 或 rejected；held-out
label 只能在排序决策生成后用于打分。

记录与 Artifact 引用中未发现秘密标记、绝对 `/home` 路径或
hidden/reference/golden 目录引用。本阶段没有访问这些目录或内容。

## 5. 测试与复现

正式结果：

```text
C1 focused: 41/41 PASS
Full suite: 574/574 PASS
compileall: PASS
git diff --check: PASS
```

审计器初版出现两个实现级误报：把普通 `task-` 子串误当作 `sk-` 密钥，并在
canonical bytes 比较中使用了错误类型。它们在一个局部修复循环内完成修正；
Experience 原数据没有改动。

## 6. 状态判定

C1 判定为 `PASS_PROMOTABLE`：

- Schema 和 canonical digest 稳定；
- 数据全部有效且身份唯一；
- 四 Mode 都有真实历史记录；
- 专用与完整测试、compileall、diff check 全部通过；
- 没有秘密或禁止数据边界问题；
- C2 所需的离线输入有效。

该状态只授权人工审查/Shadow 数据使用。Experience authority 保持 `SHADOW`，
Experience Guided 保持 `NOT_ADMITTED`。

## 7. 当次预算

```text
真实 LLM calls = 0
真实 Token = 0
CSim = 0
Synth = 0
CoSim = 0
Tool Credits = 0
```

