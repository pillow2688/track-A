# FPT 2026 Track A：新成员项目总览与当前接手说明

> 更新日期：2026-07-30
> 当前开发根目录：`/home/ying/CompetitionTrackA/track-A-rc1`
> 当前 Harness：`/home/ying/CompetitionTrackA/track-A-rc1/llm4hls_harness`
> 当前分支 / 基础提交：`release/track-a-rc2-candidate` / `d2cc309de44d6b74d86576aac34d43d65ffc9cd3`

## 先读这一段：当前事实与历史证据不能混为一谈

项目同时保留两类东西：

1. **已封存的真实实验 Artifact**：包括 fresh 28 题主实验和 A2/A3 消融。它们是历史运行事实，不能修改、覆盖或拿来伪装成当前未提交代码的结果。
2. **当前 RC2 工作树**：正在进行“搜索控制与失败反馈”重构，存在未提交的代码和测试修改。它还没有完成全部本地验证，也没有用新的、与当前代码指纹绑定的 Admission 跑过定向 Vitis 回归。

因此，下面的 `24/28` 和消融数字可用于理解现状与定位问题，**不能声称已经证明当前 dirty RC2 代码取得同样结果**。在冻结提交、签发新 Admission、完成定向回归以前，也不能声称四个历史失败题已经修复。

## 1. 比赛任务与成功标准

Track A 要求 Agent 在不修改任务平台、头文件或测试平台的前提下，修复或优化 C/C++ HLS kernel，并通过 Vitis 验证。公开 `v3d-fast` Corpus 有 28 道题，包含：

| Mode | 任务目标 |
|---|---|
| `REPAIR` | 修复功能、编译或实现错误。 |
| `SYNTH_FIX` | 让代码可被 HLS 综合。 |
| `STRUCTURAL_FIX` | 修复 DATAFLOW、stream/FIFO、接口、死锁、CoSim 等结构性问题。 |
| `OPTIMIZE` | 在合法性不退化的前提下改善 latency / acceleration。 |

项目的严格成功定义比评分脚本更保守：必须冻结一个 final candidate，并在独立最终认证（B2）中同时通过 CSim、Synth、CoSim 和 100 MHz Gate。仅有 `Agent DONE`、搜索期 PASS 或普通 test fixture PASS 都不算严格成功。

官方评分的核心优先级是：**正确性 > 可综合性 > 加速收益**。任何 hidden functional failure 都会使得分归零；资源数据主要用于合法性和诊断，不能为了资源数字牺牲正确性。

## 2. 主框架：一次任务如何运行

```text
公开任务（task.toml、kernel、公开 testbench）
  → 创建只读 baseline / candidate_000
  → 运行基线 CSim、Synth，必要时运行风险触发 CoSim
  → 根据真实工具事实进入四类 Mode
  → A1：把工具结果整理为结构化 Evidence Memory
  → A3（可选）：从冻结 Experience Store 给出短 Strategy Card 或 ABSTAIN
  → A2（可选）：结合新证据、预算和 closeout 决定是否允许下一次 Planner
  → Planner：输出受严格约束的 unified diff
  → B1：检查 diff、接口、预算、Candidate Registry、Provenance 与回退边界
  → 新 Candidate 搜索期验证：CSim → Synth → 风险触发 CoSim
  → 选择并冻结最优合法 Candidate
  → B2 独立最终认证：CSim → Synth → CoSim → 100 MHz Gate
```

### 两个预算域

- **Agent 搜索期**：遵守每题的 `max_tokens`、`max_credits` 和搜索期 closeout reserve。CSim、Synth 以及风险触发 CoSim 计入唯一的 Agent Ledger。
- **B2 最终认证期**：冻结 final candidate 后独立运行；不扣 Agent Ledger，也不能把认证结果免费回流给同一轮搜索。

题目声明的预算优先于开发 fallback。`32768` 只是在任务没有 `max_tokens` 时可记录的开发回退，不是统一默认额度；`1/4/20` 也只是可配置的开发工具成本参考，不是官方固定价格。

## 3. 组件地图与不可破坏的边界

| 类别 | 组件 | 责任 | 默认 / 边界 |
|---|---|---|---|
| A1 | Structured Evidence Memory | CSim/Synth/CoSim failure、latency/II、Candidate failure memory、Evidence Delta | 可开关；关闭不等于关闭基础 PASS/FAIL 事实。 |
| A2 | Budget-Aware Continuation | 根据新证据、重复失败、预算和 closeout 控制继续、停止或收口 | CLI 兼容名是 `v2`；有效策略版本是 `v3.continuation-policy.v3`。`enforce` 必须有当前版本 Admission，否则 fail-closed。 |
| A3 | Experience Strategy Advisor | 读取冻结 Experience Store，生成 Strategy Card 或 `ABSTAIN` | 可开关；关闭时不得加载 Store/Admission，也不得注入 Strategy Card。 |
| B1 | Candidate and Safety Baseline | immutable baseline/candidate、Registry、diff/接口守卫、ledger、合法 incumbent 回退、Provenance | 始终开启，不是消融变量。 |
| B2 | Independent Final Certification | 冻结后独立 CSim、Synth、CoSim、100 MHz Gate 和 receipt | 始终开启，独立预算域，不能反馈同一轮 Agent。 |

### A2 和 A3 怎样协作，又如何独立

A3 先准备建议，A2 再判断是否值得为 Planner 调用保留预算。A2 放行后，A3 的 advice 才会随 Planner action 被持久化。二者不是互相授权关系：

- A2 关闭时，A3 仍能独立生成 advice；
- A3 关闭时，A2 仍能独立做预算和证据收口；
- 两者关闭都不影响 B1 的候选安全、预算账本和合法回退，也不影响 B2 认证。

## 4. 已封存的实验进度（历史事实）

### 4.1 Fresh 28 题 Full Agent 主实验

数据目录：`llm4hls_harness/runs/full-agent-current-fresh-28-20260729-a01/`
可读汇总：`llm4hls_harness/runs/goal-020-a2a3-main-ablation-reports-20260730-a01/main_28_task_summary.json`

该 campaign 的配置为 A1 on、A2 enforce、A3 guided、fixed token、B1/B2 on、DeepSeek V4 Pro、Vitis。封存汇总为：

- 严格成功：**24/28（85.71%）**；
- Planner 调用：49 次；Token：117,801；Agent Credits：848；
- 普通失败题：`v3d_fast_012`（Synth）、`016`（CoSim）、`017`（CoSim）、`020`（当时汇总误报为 `UNKNOWN`，原始证据实际是 Candidate CoSim no-progress timeout）。

这些失败题的原始 Artifact 不能删除。当前开发工作的目的正是消除它们暴露出的通用搜索缺陷，而不是只为四题写特例。

### 4.2 A2/A3 2×2 消融

当前可用的收口矩阵是六题、四组，A1 始终 on；020 因 baseline CoSim 压力会主导处理效应，被按协议排除。

| Group | A2 | A3 | 严格成功 |
|---|---|---|---:|
| `FULL_REF` | enforce | guided | 6/6 |
| `MINUS_A2` | off | guided | 6/6 |
| `MINUS_A3` | enforce | off | 5/6 |
| `MINUS_A2_A3` | off | off | 6/6 |

数据目录：`llm4hls_harness/runs/a2-a3-ablation-current-6x4-20260730-a01/`
汇总目录：`llm4hls_harness/runs/goal-020-a2a3-main-ablation-reports-20260730-a01/`

唯一成功差异来自 `v3d_fast_012`，每个格子只有一次真实 LLM 轨迹。因此该实验只能说明“没有观察到明显安全破坏，并发现了需调查的差异”，**不能**推出 A2 或 A3 对所有任务具有因果收益或损害。

## 5. 当前正在做的核心工作（不是已完成能力）

根因审计已确认：旧实现把 Provider 输出不完整、unified-diff 坐标拒绝、Candidate 的真实工具失败等不同事件，都压成 `no_improvement_rounds`。累计两次后就停止，因此出现“模型还没有真正完成两次不同的修复实验，却被提前终止”的情况。

当前 RC2 的小型重构方向是：

1. 将终态原因与最后真实失败阶段、失败类型、规范化 failure signature 分离；
2. 将 failure facts、Obligation、Proposal experiment、parent/fallback 选择写成小型结构化状态和 Artifact；
3. 把 patch 格式/Provider 机械问题与真实语义失败分账，避免机械拒绝直接耗尽语义搜索次数；
4. 对 DATAFLOW/stream/CoSim no-progress 生成可读的结构化证据；
5. 要求下一次真正的 Planner 提案显式声明要解决的 Obligation、假设、action family、验证计划和失败条件，从而避免重复同一实验。

已完成的部分代码和设计材料位于：

- `llm4hls_agent/v3_search_control.py`（当前未提交）；
- `docs/status/framework_failure_root_cause_audit.md`；
- `docs/status/search_control_refactor_design.md`；
- `docs/status/obligation_and_proposal_design.md`；
- `docs/status/structural_fix_evidence_report.md`。

**重要状态：此前 `openai_provider.py` 的通用 `propose_patch()` 误读取
task-aware 字段的 2 个错误已经修复，相关聚焦测试已通过；但这轮 schema
接线、完整 fast suite 验收、冻结提交和新 Admission 尚未完成。故当前工作树
仍不应运行真实 DeepSeek/Vitis campaign，也不应被称为可冻结 Release。**

## 6. 020 的特殊性：为什么它慢，但不能当作普通 Agent 失败

`v3d_fast_020` 是 DATAFLOW 反馈环 / FIFO 死锁压力任务。旧实现中 baseline CoSim 会长期占用预算和 wall time，留给 candidate 的时间不足，甚至会留下不完整 Artifact。

已存在的 Executor 保护方向是：

- 单题使用同一个绝对 deadline；
- 工具 timeout 会按剩余时间裁剪，并保留清理余量；
- Vitis/XSIM 使用当前 run 的独立进程组清理，不能误杀历史 run；
- 时间不足时不启动新的 CoSim，并写明“未启动”的原因；
- 外层 timeout 后从部分 Artifact 恢复已知 Token、Credit、Planner 调用；未知值必须写 `UNKNOWN`，不能伪造为 `0`。

020 的历史 smoke 曾完整通过 B2；某次 28 题主实验失败则是新模型 candidate 的 CoSim 未通过，两者应分别解释。详细证据在：
`llm4hls_harness/runs/goal-020-a2a3-main-ablation-reports-20260730-a01/v3d_fast_020_fix_acceptance.md`。

## 7. 新成员最容易踩的坑

1. **不要用 `track-A/` 和 `track-A-rc1/` 的结果混算。** 当前活跃开发目录是 `track-A-rc1/`。
2. **不要覆盖 run 目录、Candidate、Ledger、package 或 certification receipt。** 每次真实运行必须是 fresh run。
3. **不能读取 `hidden/`、`reference/` 或 golden 答案，也不能修改 testbench、header、task metadata。**
4. **不要用 `VITIS_ROOT`。** 本机只能显式设置：

   ```bash
   export LLM4HLS_VITIS_HLS_ROOT=/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis
   ```

5. **不要通过关闭 A2、放宽 Admission 或改历史 Artifact 来绕开 fail-closed。** 旧 RC1 Admission 不授权当前 RC2 / dirty 代码。
6. **不要把脚本化 backend 当成真实 Agent 证据。** 真实证据必须来自记录了 provider、Ledger、Vitis 与 B2 的 fresh run。
7. **不要自动 commit、push、rebase、reset 或清理历史 run。** 工作树当前是 dirty，先保存和审核 diff。

## 8. 推荐阅读顺序与日常入口

先进入 Harness：

```bash
cd /home/ying/CompetitionTrackA/track-A-rc1/llm4hls_harness
```

推荐阅读顺序：

1. `AGENTS.md`：硬约束、预算域、Candidate 安全与工具链规则；
2. `README_CN.md`：基本使用方式；
3. `docs/status/framework_failure_root_cause_audit.md`：四个历史失败的证据化根因；
4. 本文第 4、5 节：历史成果与当前未完成代码的边界；
5. `task_corpus/v3d-fast/README.md` 与具体公开任务的 `task.toml` / `description.md`。

已有 VS Code/终端脚本，但当前 dirty RC2 下不要直接开始真实 campaign：

- `scripts/vitis-2025.2-preflight.sh`：只做工具链预检；
- `scripts/run_full_agent_v09_vscode.sh`：历史 Full Agent 批次入口；
- `scripts/run_a2_a3_ablation_vscode.sh`：历史 A2/A3 消融入口；
- `scripts/build_goal_020_reports.py`：只读地重建现有汇总。

## 9. 接下来正确的工作顺序

1. 完成并审查当前 Provider / Planner Proposal schema 接线；
2. 用聚焦单元测试验证 schema、机械 diff、Candidate 回退、A2/A3 开关隔离和 package/provenance；
3. 运行完整 fast unit suite，区分本次回归与历史 Artifact / 旧 Admission 依赖问题；
4. 冻结代码为新的 commit，并针对该 commit 生成新的 A2 Gate / Admission；
5. 只跑 012、016、017、020 与跨 Mode 哨兵的 fresh 定向回归；
6. 只有这些证据稳定后，才决定是否进行新的 full 28 题 campaign、消融或 Release 冻结。

## 10. 本文的证据出处

| 结论类型 | 一手来源 |
|---|---|
| 比赛/工程硬约束 | `llm4hls_harness/AGENTS.md`；`doc/materials/01_official/` 中的官方资料。 |
| 28 题主实验数字 | `runs/goal-020-a2a3-main-ablation-reports-20260730-a01/main_28_task_summary.json` 与对应 run Artifact。 |
| A2/A3 消融数字 | 同目录的 `ablation_summary.json`、`ablation_matrix.json` 和原始 6×4 run。 |
| 四个失败题根因 | `docs/status/framework_failure_root_cause_audit.md` 以及其中逐条列出的 Ledger、Planner、Evidence、Candidate Artifact。 |
| 当前开发未完成状态 | 当前 `git status`、`llm4hls_agent/` 和 `tests/` 的未提交变更；本文件不把未完成代码写成实验成功。 |

---

如果只记住一句话：**历史数据说明系统已经具备真实 Vitis 全链路能力，但当前优先事项是把“失败后如何继续进行不同且可归因的修复实验”做正确、测透、冻结，再重跑受影响任务。**
