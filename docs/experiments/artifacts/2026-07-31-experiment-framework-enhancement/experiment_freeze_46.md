# Experiment freeze: RC2 46-task FULL_REF

- Frozen at: `2026-07-31T06:36:17.452173+00:00`
- Git commit: `6f1e31a86159dd26fa8f7c161b77e3aaaa25a0b4`
- Branch: `release/track-a-rc2-candidate`
- Worktree: intentionally dirty before freeze; the full `git status --short` and patch hashes below are part of this receipt.
- Runtime/Executor fingerprint: `39e1e925a2b0a15809a8b342a2cde177dd779d2bba62bdcebbaaf3a51f4e2435`

## Fixed execution contract

- Provider/model: `openai-compatible` / `deepseek-v4-pro`.
- Backend: `vitis`; `LLM4HLS_VITIS_HLS_ROOT=/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis`.
- A1 Structured Evidence Memory: on; A2 Budget-Aware Continuation: enforce (`v3.continuation-policy.v3`); A3 Experience Strategy Advisor: guided (ranker v3).
- Token policy: fixed; planner rounds: at most 4; CSim/Synth/CoSim and B2 independent final certification enabled; 100 MHz gate enabled by the runtime manifest/task contract.
- Each task must use a fresh run directory.  The two corpus roots are supplied in one serial batch invocation; no historical candidate or artifact is reused.

## Environment

- OS/kernel: `Linux-7.0.13-200.fc44.x86_64-x86_64-with-glibc2.43`
- Python: `3.12.13 (main, Mar  3 2026, 14:59:34) [Clang 21.1.4 ]`
- Vitis root: `/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis`
- Vitis version marker: `UNKNOWN`
- Device and clock constraint are task/runtime-defined; the formal acceptance gate is 100 MHz.

## Corpus

- Total tasks: **46**.
- Mode distribution: REPAIR 14; SYNTH_FIX 10; STRUCTURAL_FIX 11; OPTIMIZE 11.

| Corpus | Manifest status | SHA-256 |
| --- | --- | --- |
| v3d-fast | UNKNOWN | `9e374fa49e4c68315080ab93ba2f395deea5ae682dd145b791e1db52c2198691` |
| v3d-expanded | READY_FOR_FULL_46 | `2e7497b52bc1a6144c74e47fce5a6282f287b78aaef58970d8935d202cf6c6cc` |

| Corpus | Task ID | Mode | Difficulty |
| --- | --- | --- | ---: |
| v3d-fast | v3d_fast_001 | REPAIR | 1 |
| v3d-fast | v3d_fast_002 | REPAIR | 1 |
| v3d-fast | v3d_fast_003 | REPAIR | 1 |
| v3d-fast | v3d_fast_004 | REPAIR | 2 |
| v3d-fast | v3d_fast_005 | REPAIR | 2 |
| v3d-fast | v3d_fast_006 | REPAIR | 2 |
| v3d-fast | v3d_fast_007 | REPAIR | 2 |
| v3d-fast | v3d_fast_008 | REPAIR | 2 |
| v3d-fast | v3d_fast_009 | SYNTH_FIX | 2 |
| v3d-fast | v3d_fast_010 | SYNTH_FIX | 3 |
| v3d-fast | v3d_fast_011 | SYNTH_FIX | 3 |
| v3d-fast | v3d_fast_012 | SYNTH_FIX | 2 |
| v3d-fast | v3d_fast_013 | SYNTH_FIX | 3 |
| v3d-fast | v3d_fast_014 | SYNTH_FIX | 3 |
| v3d-fast | v3d_fast_015 | STRUCTURAL_FIX | 4 |
| v3d-fast | v3d_fast_016 | STRUCTURAL_FIX | 4 |
| v3d-fast | v3d_fast_017 | STRUCTURAL_FIX | 4 |
| v3d-fast | v3d_fast_018 | STRUCTURAL_FIX | 4 |
| v3d-fast | v3d_fast_019 | STRUCTURAL_FIX | 4 |
| v3d-fast | v3d_fast_020 | STRUCTURAL_FIX | 4 |
| v3d-fast | v3d_fast_021 | OPTIMIZE | 2 |
| v3d-fast | v3d_fast_022 | OPTIMIZE | 2 |
| v3d-fast | v3d_fast_023 | OPTIMIZE | 2 |
| v3d-fast | v3d_fast_024 | OPTIMIZE | 2 |
| v3d-fast | v3d_fast_025 | OPTIMIZE | 2 |
| v3d-fast | v3d_fast_026 | OPTIMIZE | 3 |
| v3d-fast | v3d_fast_027 | OPTIMIZE | 3 |
| v3d-fast | v3d_fast_028 | OPTIMIZE | 3 |
| v3d-expanded | expanded_optimize_01 | OPTIMIZE | 3 |
| v3d-expanded | expanded_optimize_02 | OPTIMIZE | 3 |
| v3d-expanded | expanded_optimize_03 | OPTIMIZE | 3 |
| v3d-expanded | expanded_repair_01 | REPAIR | 2 |
| v3d-expanded | expanded_repair_02 | REPAIR | 2 |
| v3d-expanded | expanded_repair_03 | REPAIR | 2 |
| v3d-expanded | expanded_repair_04 | REPAIR | 2 |
| v3d-expanded | expanded_repair_05 | REPAIR | 2 |
| v3d-expanded | expanded_structural_01 | STRUCTURAL_FIX | 4 |
| v3d-expanded | expanded_structural_02 | STRUCTURAL_FIX | 4 |
| v3d-expanded | expanded_structural_03 | STRUCTURAL_FIX | 4 |
| v3d-expanded | expanded_structural_04 | STRUCTURAL_FIX | 4 |
| v3d-expanded | expanded_structural_05 | REPAIR | 4 |
| v3d-expanded | expanded_structural_06 | STRUCTURAL_FIX | 4 |
| v3d-expanded | expanded_synth_01 | SYNTH_FIX | 3 |
| v3d-expanded | expanded_synth_02 | SYNTH_FIX | 3 |
| v3d-expanded | expanded_synth_03 | SYNTH_FIX | 3 |
| v3d-expanded | expanded_synth_04 | SYNTH_FIX | 3 |

## Frozen runtime artifacts

| Artifact | Path | SHA-256 |
| --- | --- | --- |
| a2_gate | `docs/experiments/artifacts/2026-07-30-structural-search-continuation-runtime-overlay/a2/continuation-v3-gate.json` | `8c7321a91c8a7851e0d12fdec501f7106b792a8f58474e0693113c6bba8446f4` |
| a2_admission | `docs/experiments/artifacts/2026-07-30-structural-search-continuation-runtime-overlay/a2/continuation-v3-admission.json` | `40d55864b6442c0342d3db4c6f1d882088dd232afd6baeb0703590efb65ddcba` |
| a3_store | `docs/experiments/artifacts/2026-07-30-structural-search-continuation-runtime-overlay/a3/train-only-experience-store.jsonl` | `59ea3f89da638405e231181fc158347f1e4c0385201c52720e049dcfc5395f06` |
| a3_gate | `docs/experiments/artifacts/2026-07-30-structural-search-continuation-runtime-overlay/a3/ranker-v3-gate.json` | `740059de3fffd81860519a34f990d2602e5a40d7bd8b706c09b29d898ffbeed6` |
| a3_admission | `docs/experiments/artifacts/2026-07-30-structural-search-continuation-runtime-overlay/a3/admission.json` | `909e1d5baf4fb508296be908c8c12cf33e8b5e55b4cbc6c984e035e53de97d92` |

## Source-state evidence

```text
M docs/status/a2_a3_runtime_contract.md
 M docs/status/checkpoint_report.md
 M docs/status/code_change_inventory.md
 M docs/status/final_git_status.md
 M docs/status/framework_failure_root_cause_audit.md
 M docs/status/obligation_and_proposal_design.md
 M docs/status/search_control_refactor_design.md
 M docs/status/structural_fix_evidence_report.md
 M docs/status/targeted_regression_report.md
 M docs/status/test_results.md
?? docs/experiments/artifacts/2026-07-30-goal-020-structural-liveness/
?? docs/experiments/artifacts/2026-07-30-rc2-fresh-28-code-freeze/
?? docs/experiments/artifacts/2026-07-30-rc2-search-control-freeze/
?? docs/experiments/artifacts/2026-07-30-structural-search-continuation-runtime-overlay/
?? docs/experiments/artifacts/2026-07-31-experiment-framework-enhancement/
?? docs/experiments/artifacts/2026-07-31-rc2-main-28-freeze/
?? docs/status/2026-07-30-rc2-goal-completion-audit.md
?? docs/status/2026-07-30-rc2-main-and-ablation-execution-plan.md
?? docs/status/2026-07-30-rc2-search-control-targeted-regression-progress.md
?? llm4hls_harness/llm4hls_agent/config/track_a_rc2_search_control_targeted_regression.json
?? llm4hls_harness/llm4hls_agent/config/track_a_rc2_structural_search_continuation_020.json
?? llm4hls_harness/scripts/run_rc2_real_host_a02.sh
?? llm4hls_harness/scripts/run_rc2_real_host_smoke.sh
?? llm4hls_harness/task_corpus/v3d-expanded/
?? llm4hls_harness/tools/
```

```text
diff --git a/docs/status/a2_a3_runtime_contract.md b/docs/status/a2_a3_runtime_contract.md
index 746cf99..bbfc2e0 100644
--- a/docs/status/a2_a3_runtime_contract.md
+++ b/docs/status/a2_a3_runtime_contract.md
@@ -15,3 +15,15 @@ A3 仍为 soft-guided：最多一条 Top-1 Strategy Card、低置信度 ABSTAIN
 ## 当前冻结边界

 仓库 HEAD 为 RC2，但现有正式 A2 Admission 仍绑定 RC1 commit，因此 Enforce 正确 fail-closed。它不能被当作本次代码的正式授权；在代码冻结并生成新的、独立可审计 Gate/Admission 前，不能用它运行正式 Enforce 定向回归。
+
+### RC2 当前绑定更新
+
+代码提交 `201af1209aa4afab6004cafa2d0ddf36a080e7c6` 后，A2 Gate 已离线重放为 PASS，A2
+Admission 与 A3 Admission 均曾重新绑定该提交；完整 manifest
+`llm4hls_agent/config/track_a_rc2_search_control_targeted_regression.json` 的加载状态为
+`READY`。随后 Prompt 合同修复提交 `6d04ac9d182afd4a2d236e6aede9a66b2afb2a5d` 已使这些旧 binding
+失效。新的离线冻结目录为
+`docs/experiments/artifacts/2026-07-30-rc2-prompt-contract-freeze/`：A2 Gate/Admission 与 A3
+Gate/Admission 均 PASS，新的 manifest
+`llm4hls_agent/config/track_a_rc2_prompt_contract_targeted_regression.json` 已加载为 `READY`。该 binding
+仅用于本 Goal 的定向回归，不构成新的 Release 或全量实验许可。
diff --git a/docs/status/checkpoint_report.md b/docs/status/checkpoint_report.md
index f218172..4d99f97 100644
--- a/docs/status/checkpoint_report.md
+++ b/docs/status/checkpoint_report.md
@@ -1,33 +1,32 @@
-# 框架稳定性检查点
-
-> 更新：2026-07-30，本检查点记录本地冻结前的代码与测试证据。
+# HLS Agent 框架稳定性检查点

 ## 已完成

-- 历史 012/016/017/020 轨迹审计完成，根因与原始 Artifact 一一对应；
-- 统一失败事实、义务、Proposal 实验与 parent 选择记录已加入；
-- task-aware Provider 现在要求真实的 `target_obligation`、`action_family`、
-  `action_parameters`、`validation_plan`、`failure_criteria` 与 `fallback`；
-- Candidate Portfolio 显式保存 Verified Best、Active Probe 与 Fallback Parent；
-  默认不会从失败 Candidate 继续分支；
-- Planner context 对大 kernel 使用确定性原始行切片，不默认发送整个源文件；
-- CoSim Evidence 新增 xsim 启动、编译/展开/运行阶段、transaction progress、
-  log/output 增长等保守事实；
-- 机械 patch 失败不再触发固定两次语义无改进停止；
-- STRUCTURAL_FIX 获得保守 stream/DATAFLOW 事实与 CoSim 进度证据；
-- 020 的 `UNKNOWN` 汇总归类通用修复完成；
-- A2/A3 关闭隔离、Candidate 安全与 B2 边界未改动；
-- 当前框架相关 130 项本地测试通过；完整发现测试已执行至第 140 项，
-  因 RC2 工作树缺失发布快照引用的历史 A2/A3 Artifact 而 fail-closed，
-  具体见 `test_results.md`。
+- 阶段 0 历史根因审计、统一 failure 事实、Obligation/Proposal、三引用 Portfolio、紧凑 Planner
+  context、CoSim 结构/运行 Evidence、A2/A3 隔离和 Admission fail-closed 路径均已实现并记录。
+- 提交 `201af1209aa4afab6004cafa2d0ddf36a080e7c6` 进一步阻止无新 Evidence 下的动作族别名绕过。
+- 当前 A2 Gate 离线重放 PASS，A2/A3 Admission 与 RC2 manifest 已绑定当前 HEAD，manifest load 为
+  `READY`。Prompt 合同修复后的新冻结目录为
+  `docs/experiments/artifacts/2026-07-30-rc2-prompt-contract-freeze/`，绑定 commit
+  `6d04ac9d182afd4a2d236e6aede9a66b2afb2a5d`。
+- 012、016、020 与 009/015/021 获得完整 fresh 认证成功；017、001 与授权后的 004 获得完整、可归因的
+  普通失败终态；004 的四个 Provider failure 均被 Ledger、Package 和 Provenance 完整封装。没有未完成
+  action、Ledger/Package/Provenance 不一致或当前 run 残留进程。
+- 聚焦与完整本地单元测试均成功结束。

-## 未完成 / 不可伪造
+## 仍然成立的限制

-- 真实定向 fresh HLS 回归尚未运行；
-- 现有 RC1 Admission 不授权 RC2 / 当前改动；本地冻结后仍须重新签发
-  与新 commit 绑定的 A2 Admission；
-- 因此不能声称 012/016/017/020 已通过，不能声称 24/28 已提升，也不能声称 A2/A3 有因果收益。
+- 017 的两个当前协议失败显示模型仍可能在不同结构策略下不能解除 DEADLOCK；这是已保留 Artifact 的
+  能力边界，不是框架成功。
+- 旧 001 与旧 004 的 provider/Proposal 失败 Artifact 均保留；004 暴露的 Prompt 未明确完整
+  `validation_plan`、而解析器严格要求该序列的合同不一致已修复。
+- 修复后，新指纹的 `slot_010_v3d_fast_004_prompt_contract_regression` 用 1 次 Planner 形成
+  `candidate_001`，B2 CSim/Synth/CoSim 和 100 MHz Gate 全部 PASS；它补齐 REPAIR 成功哨兵，且没有覆盖旧
+  失败证据。
+- 020 当前只有一次当前提交成功，不能称为稳定复现或总体收益。
+- 本 Goal 没有执行 28 题、Holdout、38 题或正式消融。

 ## 下一入口

-先由人工决定代码冻结策略；冻结后使用新的 commit 生成独立 A2 Gate/Admission，再按已固定的 4 个失败任务（最多两次）+4 哨兵协议执行真正 Vitis 回归。
+允许后续更大规模实验前，应先由人工评审本报告和 `targeted_regression_report.md`；不需要在本 Goal
+内继续扩展架构或自动重跑失败题。
diff --git a/docs/status/code_change_inventory.md b/docs/status/code_change_inventory.md
index 079cce8..6305e31 100644
--- a/docs/status/code_change_inventory.md
+++ b/docs/status/code_change_inventory.md
@@ -23,4 +23,15 @@
   `tests/test_v3_batch_benchmark.py`：补充 CoSim runtime、机械 hunk、
   Candidate 安全、Admission fail-closed、CoSim 归类测试。

+- `llm4hls_agent/v3_search_control.py`：补充动作族别名归一与无新 Evidence 的
+  `forbidden_action_families`/`required_action_family` 合同。
+- `llm4hls_agent/v3_prototype.py`：将动作族合同用于 Live Planner Proposal 准入，并把
+  该类拒绝计入语义无改进而非机械恢复。
+- `tests/test_v3_search_control.py`：覆盖 FIFO 深度别名、stream 环拓扑要求及违约拒绝。
+
+- `llm4hls_agent/openai_provider.py`：将 task-aware 模式/`requires_cosim` 的确定性验证序列提取为
+  单一规则，并把精确 `validation_plan` 写入 Planner Prompt 与输出 schema；严格解析与执行顺序未放宽。
+- `tests/test_openai_provider.py`：覆盖 REPAIR/SYNTH_FIX/STRUCTURAL_FIX 的精确 Prompt 合同、REPAIR 的
+  CoSim 风险序列，以及部分 `validation_plan` 继续被 fail-closed 拒绝。
+
 备份位于 `llm4hls_harness/.codex/backups/2026-07-30-framework-search-control-before-change/`。
diff --git a/docs/status/final_git_status.md b/docs/status/final_git_status.md
index cb6ae2d..bfd0f9c 100644
--- a/docs/status/final_git_status.md
+++ b/docs/status/final_git_status.md
@@ -1,14 +1,21 @@
-# Git 冻结前状态（本阶段）
+# 框架稳定性阶段 Git 状态

-- 分支：`release/track-a-rc2-candidate`
-- 开始 HEAD：`d2cc309de44d6b74d86576aac34d43d65ffc9cd3`
-- 在本记录生成时，尚未执行 commit、merge、push、reset 或历史 Artifact 覆盖；
-  后续本地 commit 仅冻结本记录列出的框架改动，不会 push。
-- 修改仅为搜索控制、失败归因、Proposal schema、Prompt 投影、Candidate
-  Portfolio、CoSim 运行事实、测试和阶段文档；`min/` 未处理。
-- 产品文件修改前的只读备份：`llm4hls_harness/.codex/backups/2026-07-30-framework-search-control-before-change/`。
-
-框架相关本地测试 130 项已通过；完整发现运行至第 140 项时，被缺失的历史
-Release Metadata Artifact 正确 fail-closed 中断，详见 `test_results.md`。冻结后
-仍须重新签发与该 commit 绑定的 Admission；当前不具备“已授权 Enforce 正式回归”
-的条件。
+- 分支：`release/track-a-rc2-candidate`。
+- 当前框架代码提交：`6d04ac9d182afd4a2d236e6aede9a66b2afb2a5d`，信息为
+  `clarify task-aware validation plan contract`。该提交修复了 004 所暴露的 Prompt/严格 Proposal
+  验证计划合同不一致。
+- 前一搜索控制提交：`201af1209aa4afab6004cafa2d0ddf36a080e7c6`，信息为
+  `enforce distinct terminal action families`。
+- 前序相关提交：`fed61db`（provider rejection provenance）与 `f550304`（unknown provider
+  usage fail-closed）。
+- 本阶段没有 push、merge、reset 或覆盖历史 run Artifact；`min/` 未处理。
+- 现有 RC2 Gate/Admission 与 runtime manifest 是未跟踪的运行侧车 Artifact，仍绑定前一 HEAD；
+  新提交后它们必须重新生成，才能作为 `6d04ac9` 的 Enforce 运行授权。不得把旧绑定冒充为新代码
+  fingerprint 的 Admission。
+- 已离线生成 `docs/experiments/artifacts/2026-07-30-rc2-prompt-contract-freeze/` 与
+  `llm4hls_agent/config/track_a_rc2_prompt_contract_targeted_regression.json`；A2/A3 Gate 与 Admission
+  均 PASS，manifest 加载为 `READY`。这些侧车未提交，以避免之后再提交时使 `current_commit` 绑定失效。
+- 此新绑定已用于 fresh `v3d_fast_004` Prompt 合同回归；结果为 `DONE/REPAIR_FINALIZED`、B2 PASS、
+  100 MHz Gate PASS，且 B2 未改写 Agent Ledger。没有 push、merge 或覆盖历史 Artifact。
+- 本报告生成后工作树可能包含未跟踪的 Gate/Admission、manifest、状态报告和 fresh run 目录；它们是
+  可审计证据，不是对既有提交的隐藏修改。提交前必须重新生成与新 HEAD 一致的 Admission。
diff --git a/docs/status/framework_failure_root_cause_audit.md b/docs/status/framework_failure_root_cause_audit.md
index b03c071..92d771f 100644
--- a/docs/status/framework_failure_root_cause_audit.md
+++ b/docs/status/framework_failure_root_cause_audit.md
@@ -41,6 +41,27 @@ llm4hls_harness/runs/full-agent-current-fresh-28-20260729-a01/

 该计数达到 2 后，统一由 `task_repair_no_improvement_limit` 拒绝后续搜索，blocker 为 `no_improvement:2>=2`。它不要求第二次 Proposal 具有不同的 hypothesis、action family、fallback 或 parent。因此 A2 能安全放行/停止，但无法保证 Planner 在做不同实验。

+## RC2 定向验收后的审计结论
+
+这个历史根因已由 `search_control` 的动作族合同收紧：相同 failure signature 下，失败动作族进入
+`forbidden_action_families`，stream 环中的 FIFO 失败要求拓扑替代或安全收口。020 的当前 fresh run
+证实该路径实际从 FIFO 深度转为 `DATAFLOW_TOPOLOGY_OR_ORDER` 并经 B2 通过；017 的当前 run 也从 FIFO
+容量转为 `STREAM_ACCESS_ORDER`、从 baseline 重新分支，但仍 CoSim DEADLOCK。故“重复策略”框架缺陷已
+修复；“模型一定能找对第二个结构修复”仍未被证明。
+
+### 004 fresh REPAIR 替代哨兵新增发现
+
+授权后的 `v3d_fast_004` fresh run 没有证明 REPAIR 成功路径：Baseline CSim 后，四次
+DeepSeek 输出都给出了同一类可应用的 `source_index=(i+1)%N → i` 最小修复，但 Proposal 的
+`validation_plan` 写为 `['csim']`。当时 Prompt 只笼统说“从 csim/synth/cosim 中选择有序阶段”，
+而确定性 Provider 解析器要求 REPAIR 精确填写 `['csim','synth']`，因此四次都在 Candidate 创建前
+被 `RepairProviderError` 拒绝。这是 Prompt 与严格 Proposal 合同不一致，不是 Patch 语义、Vitis、
+Ledger、Package 或 Provenance 异常。
+
+当前修复将相同的模式/`requires_cosim` 确定性规则同时用于 Prompt 和解析器：Prompt 现在明确要求
+精确序列，解析器仍对简写 fail-closed。这样没有放宽 CSim/Synth/CoSim、Candidate 或 B2 条件；只是避免
+一个本已正确的 Patch 因未告知的元数据格式而被拒绝。
+
 另一个共性问题是 failure 的语义被混合：工具 phase、failure kind、evidence fingerprint、continuation reason 和 terminal stop reason 不在稳定字段中分别保存。020 的真实最后工具事实是 candidate CoSim no-progress timeout，但主汇总最终得到 `UNKNOWN`，说明汇总层没有把 `phase=timeout`/`failure_kind=TIMEOUT` 映射为 CoSim failure stage。

 ## 逐题轨迹结论
diff --git a/docs/status/obligation_and_proposal_design.md b/docs/status/obligation_and_proposal_design.md
index 090c223..b76b6c0 100644
--- a/docs/status/obligation_and_proposal_design.md
+++ b/docs/status/obligation_and_proposal_design.md
@@ -26,3 +26,7 @@ Planner-visible 状态另外包含 CandidateState（验证层级、CSim/Synth/Co
 资格成为后续 parent。

 父节点默认是 Verified Best；结构性失败后若没有其他已验证候选，明确记录从 baseline/Verified Best 重新分支，而不会从失败 Candidate 叠加 Patch。所有 Candidate 仍保持 `parent_id`、代码 hash 和 provenance。
+
+在无新 Evidence 的终态 follow-up 中，Proposal 还必须满足 `search_control` 的动作族合同；例如已有
+FIFO 容量失败的 stream 依赖环不接受另一种深度措辞。这个约束是确定性准入规则，不是 Prompt 建议，且
+不会让 A3 Strategy Card 借用 A2 或当前工具 Evidence 的权威性。
diff --git a/docs/status/search_control_refactor_design.md b/docs/status/search_control_refactor_design.md
index 5d538be..345040b 100644
--- a/docs/status/search_control_refactor_design.md
+++ b/docs/status/search_control_refactor_design.md
@@ -19,8 +19,25 @@
   只改变其中一个维度的提案仍可以作为可证伪 follow-up；脚本化 fixture
   不启用此拒绝，以保留既有回放/恢复测试的语义。

+## RC2 补充：无新 Evidence 的动作族边界
+
+提交 `201af1209aa4afab6004cafa2d0ddf36a080e7c6` 收紧了上述边界：当终态失败的
+failure signature 与上次 Planner 输入相同，已尝试动作族进入
+`forbidden_action_families`。`STREAM_DEPTH_INCREASE` 等 FIFO 深度别名统一归为
+`FIFO_CAPACITY_OR_PROTOCOL`；名称变化不能绕过重复保护。若源码有 stream 依赖环且
+FIFO 类已经失败，下一轮要求 `DATAFLOW_TOPOLOGY_OR_ORDER` 或安全收口。违约 Proposal
+不会物化 Candidate，且按语义无改进计数，不再作为机械恢复无限重试。
+
 ## 行为边界

+### Proposal 验证计划合同对齐
+
+`v3d_fast_004` 的 fresh run 证明，严格 Proposal 解析器与 Prompt 之间曾存在一处无意义的接口鸿沟：
+解析器要求 REPAIR（无 CoSim 风险）精确声明 `csim,synth`，而 Prompt 没有写出该必填序列，模型遂反复
+只写 `csim`。现在 `openai_provider.py` 用同一纯函数计算模式/`requires_cosim` 对应的验证序列，并把
+精确 JSON 数组写入 `VALIDATION PLAN (MANDATORY)` 和输出 schema；解析器仍必须精确匹配，不能用
+缩写绕过安全检查。执行权仍属于确定性控制层。
+
 补丁 hunk/Provider 输出问题会产生 `CONTINUE_WITHOUT_LLM` 建议，表示可以先走现有确定性 header/count/relocation 修复；无法安全归一化时仍必须让 Planner 重新生成 diff，绝不静默改写模型 Patch。CSim、Synth、CoSim 的真实新失败则为 `CONTINUE_WITH_LLM`；有更优已验证 incumbent 时记录 `SWITCH_PARENT` 候选语义。最后是否放行仍由 B1 预算和 A2 Admission 控制。

 ## 没有做的事
diff --git a/docs/status/structural_fix_evidence_report.md b/docs/status/structural_fix_evidence_report.md
index 3905361..f478725 100644
--- a/docs/status/structural_fix_evidence_report.md
+++ b/docs/status/structural_fix_evidence_report.md
@@ -9,3 +9,11 @@ CoSim failure evidence 现在额外记录 `cosim_progress`、`no_progress_second
 避免把“启动了 xsim”错误等同于“发现了死锁”。

 同时修正批次汇总：若终态有 `v3c.cosim-failure-evidence.v1`，即使随后 stop reason 是 `TASK_REPAIR_NO_IMPROVEMENT_LIMIT`、最后通用 phase 是 `timeout`，失败阶段仍归类为 `COSIM`。这正是历史 020 被误写为 `UNKNOWN` 的通用根因修复。
+
+## RC2 真实回归补充
+
+`v3d_fast_020` 的 fresh run `012e76ec9e6c` 先观察到 FIFO 深度方案 CoSim TIMEOUT，随后
+在相同 failure signature 下改为 `DATAFLOW_TOPOLOGY_OR_ORDER`，移除反馈环后 Candidate
+CoSim 与 B2 均 PASS。`v3d_fast_017` 的两次当前协议 run 都保留 `COSIM/DEADLOCK`，第二轮
+分别换为不同动作族并从 `candidate_000` 重新分支；失败仍是模型修复能力边界，不是
+`UNKNOWN` 汇总或失败 Candidate 链式叠加。
diff --git a/docs/status/targeted_regression_report.md b/docs/status/targeted_regression_report.md
index a1eab71..201593c 100644
--- a/docs/status/targeted_regression_report.md
+++ b/docs/status/targeted_regression_report.md
@@ -1,7 +1,82 @@
-# 定向回归状态
+# RC2 定向回归报告

-目标任务是 `v3d_fast_012`、`016`、`017`、`020` 以及四个跨 Mode 成功哨兵；协议仍规定每个历史失败任务最多两次 fresh run、串行、无 Resume、无人工中途改代码。
+本报告仅覆盖框架稳定性定向运行，不是 28 题汇总、Holdout 或正式消融。全部 run 使用
+fresh 目录、串行 Vitis、DeepSeek V4 Pro、A1 on、A2 enforce、A3 guided、fixed token、B1/B2
+on；没有复用历史 Candidate 或 Resume。当前代码为
+`201af1209aa4afab6004cafa2d0ddf36a080e7c6`，运行 manifest 为
+`llm4hls_agent/config/track_a_rc2_search_control_targeted_regression.json`。其后，004 暴露的
+Prompt/Proposal 验证计划合同缺陷已修复并提交为 `6d04ac9d182afd4a2d236e6aede9a66b2afb2a5d`；004 的
+Prompt 合同回归使用新 manifest
+`llm4hls_agent/config/track_a_rc2_prompt_contract_targeted_regression.json`。

-本阶段**没有启动** DeepSeek、Vitis HLS 或任何 fresh 公共任务。原因不是模型失败：当前 A2 Enforce Admission 绑定 RC1，而当前产品已是 RC2，运行前 preflight 必须 fail-closed。强行关闭 A2 或伪造 Admission 会破坏本 Goal 的验收口径。
+## 失败题

-已完成的本地替代验证包括：hunk 机械恢复路径、任务路由 smoke、STRUCTURAL_FIX 证据、终态封存、Candidate 回退、A2/A3 关闭隔离和批次 CoSim 归类。获得新的冻结 commit 与有效 Admission 后，才可按预定协议执行真实定向回归。
+| task | 采用证据目录 | Mode | Planner | Token / Credits | 结果 | 关键事实 |
+|---|---|---:|---:|---:|---|---|
+| 012 | `slot_001r_v3d_fast_012_attempt_1` | SYNTH_FIX | 1 | 3776 / 15 | B2 PASS | `SYNTH_FIX_FINALIZED`；当前样本没有第二次 Planner。 |
+| 016 | `slot_002_v3d_fast_016_attempt_1` | STRUCTURAL_FIX | 1 | 3943 / 75 | B2 PASS | `STRUCTURAL_FIX_FINALIZED`；当前样本没有第二次 Planner。 |
+| 017 | `slot_003rr_v3d_fast_017_attempt_3_user_authorized_persistent` | STRUCTURAL_FIX | 2 | 8341 / 75 | FAILED | 两轮都从 `candidate_000`；FIFO 容量后改为 `STREAM_ACCESS_ORDER`；两次 CoSim DEADLOCK；stage=`COSIM`，无 B2（没有可认证 Candidate）。 |
+| 020 | `slot_004r_v3d_fast_020_attempt_2_persistent_session` | STRUCTURAL_FIX | 2 | 9646 / 100 | B2 PASS | FIFO 深度 Candidate TIMEOUT 后，第二轮为 `DATAFLOW_TOPOLOGY_OR_ORDER`；B2 CSim/Synth/CoSim 与 100 MHz PASS。 |
+
+017 的 `slot_003r...attempt_2` 也形成了完整普通失败终态；早先误判的“会话中断”不能覆盖其已有
+Artifact。用户随后明确授权的额外 fresh run 是表中的 `slot_003rr...attempt_3`。两条失败记录均
+为 `failure_stage=COSIM`，不是系统错误或 UNKNOWN。
+
+## 跨 Mode 哨兵
+
+| task | Mode | Planner | Token / Credits | 结果 | 备注 |
+|---|---:|---:|---:|---|---|
+| 001 | REPAIR | 4 | 11493 / 1 | FAILED | 四次 `PROVIDER_OUTPUT_REJECTED`；Ledger、Package、Provenance 完整；这是 provider 输出质量失败，不是零用量或封装缺失。 |
+| 004 | REPAIR | 4 | 12360 / 1 | FAILED | 授权后的 fresh 替代哨兵；Baseline CSim 后四次 Planner 均为 `PROVIDER_OUTPUT_REJECTED:RepairProviderError`，没有创建 Patch Candidate，故没有 B2 入口。Ledger 的四个 LLM action 均为 STARTED/COMPLETED 配对，Package/Provenance 完整，且无 Vitis/XSim 残留。 |
+| 004（Prompt 合同回归） | REPAIR | 1 | 3114 / 11 | B2 PASS | `6d04ac9` + 新 Manifest；`FUNCTIONAL_REPAIR` 的 `validation_plan=[csim,synth]` 被准入，`candidate_001` 完成 B2 CSim/Synth/CoSim 及 100 MHz Gate。Agent Ledger 在 B2 前后 hash 相同。 |
+| 009 | SYNTH_FIX | 1 | 3177 / 15 | B2 PASS | 旧会话中断记录已保留；采用 `slot_006r...effective_rerun` 的完整终态。 |
+| 015 | STRUCTURAL_FIX | 1 | 3869 / 75 | B2 PASS | 无明显正确性退化。 |
+| 021 | OPTIMIZE | 2 | 4677 / 20 | B2 PASS | `BASELINE_FINALIZED_NO_IMPROVEMENT`，合法返回 baseline。 |
+
+因此四个 Mode 都完成了成功的真实路径回归：004（新 Prompt 合同）、009、015、021 都通过 B2。001 与
+旧 004 的失败 Artifact 继续保留，它们分别证明 provider/Patch 元数据失败的 fail-closed 封装；旧 004 没有
+执行 B2 并非认证漏跑，而是没有任何可冻结 Candidate。不得把旧失败覆盖、删除或包装成策略收益。
+
+## Goal 字段索引
+
+下列路径是本报告要求字段的机器可读权威来源：每个 run 的 `v3_prototype_result.json` 记录 obligation、
+hypothesis、action family、parent、failure stage/kind/signature、终态 reason 和 Candidate 状态；
+`budget_ledger.jsonl` 记录 Planner、Token、Credit；`v3_certified_result.json` 和
+`certification/*/receipt.json` 记录 search/final/B2/100 MHz。采用的 fresh run 目录为：
+
+```text
+012: slot_001r_v3d_fast_012_attempt_1/runs/v3d_fast_012--deepseek-v4-pro--r001--bd182be80d8c
+016: slot_002_v3d_fast_016_attempt_1/runs/v3d_fast_016--deepseek-v4-pro--r001--2f8096923eff
+017: slot_003rr_v3d_fast_017_attempt_3_user_authorized_persistent/runs/v3d_fast_017--deepseek-v4-pro--r001--589432e9d098
+020: slot_004r_v3d_fast_020_attempt_2_persistent_session/runs/v3d_fast_020--deepseek-v4-pro--r001--012e76ec9e6c
+004: slot_010_v3d_fast_004_prompt_contract_regression/runs/v3d_fast_004--deepseek-v4-pro--r001--fdaea1fab5aa
+009: slot_006r_v3d_fast_009_sentinel_effective_rerun/runs/v3d_fast_009--deepseek-v4-pro--r001--2c417235dab5
+015: slot_007_v3d_fast_015_sentinel/runs/v3d_fast_015--deepseek-v4-pro--r001--b9021258731b
+021: slot_008_v3d_fast_021_sentinel/runs/v3d_fast_021--deepseek-v4-pro--r001--2e53680ea6a1
+```
+
+004 的完整字段摘要为：Mode=REPAIR、obligation=`FUNCTIONAL_CORRECTNESS`、action family=
+`FUNCTIONAL_REPAIR`、parent=`candidate_000`、Planner=1、Token=3114、Credit=11、搜索 CSim/Synth=PASS、
+search CoSim=按低风险 REPAIR 不要求、search final=true、B2 CSim/Synth/CoSim=PASS、100 MHz=PASS
+（1.346 ns）、e2e=true、failure stage/kind/signature=无、stop reason=`REPAIR_FINALIZED`、wall=74.39 s。
+
+## 搜索控制验收
+
+- 新 Evidence：020 第一轮 CoSim TIMEOUT 被持久化为第二轮输入；017 第一轮 CoSim DEADLOCK
+  同样进入第二轮输入。
+- 不同实验：020 从 `FIFO_CAPACITY_OR_PROTOCOL` 切到 `DATAFLOW_TOPOLOGY_OR_ORDER`；017 从
+  FIFO 容量切到 `STREAM_ACCESS_ORDER`。
+- parent：017/020 的第二轮父节点均为已验证 `candidate_000`，没有在失败 Candidate 上叠加。
+- failure 归因：当前失败 run 的 `failure_stage` 均为证据支持的 `COSIM`；020 的历史 UNKNOWN
+  汇总缺陷未在当前运行复现。
+- 020 的稳定性：当前仅有一条当前提交的成功复现，不能声称稳定成功；它证明的是“结构改写被执行且
+  通过”，不是模型在所有采样下都会找到该方案。
+- SWITCH_PARENT：本次真实失败 Parent 保持 baseline，且单元/最小路径测试覆盖 `SWITCH_PARENT`
+  决策；本次 run 没有已验证的非 baseline incumbent，因此不应虚构一次真实切换。
+
+## 结论与限制
+
+框架已消除“同一 FIFO 深度策略换措辞后继续重复”的控制缺口，并在 020 真实通过时验证了拓扑替代。
+017 仍是 Agent 能力边界：框架给出了不同动作族、相同 Evidence 和安全 parent，却没有保证模型第二个
+结构修复一定正确。当前版本可进入后续更大规模实验的候选评审，但不应根据这 8 条记录宣称 100% 成功、
+整体成功率提升或 A2/A3 的因果收益。
diff --git a/docs/status/test_results.md b/docs/status/test_results.md
index 648dbae..e5d06cc 100644
--- a/docs/status/test_results.md
+++ b/docs/status/test_results.md
@@ -1,59 +1,45 @@
-# 测试结果
+# RC2 框架稳定性测试结果

-## 本次直接相关测试
+本轮只记录当前工作树的本地测试，不把历史 green run 当作当前证据。

-命令：
+## 通过的本地集合

 ```bash
-../../track-A/.venv/bin/python3 -m unittest \
-  tests.test_v3_task_aware_smoke \
-  tests.test_v3_terminal_latency_fix \
-  tests.test_v3_prototype \
-  tests.test_v3_search_control -q
-```
-
-历史阶段曾有一组 147 项核心测试通过；那是本次继续接线前的
-阶段性结果，不能替代当前工作树的验证。
-
-当前新增/改动后已重新执行并通过的最新框架相关集合为：
-
-```bash
-../../track-A/.venv/bin/python3 -m unittest \
+/home/ying/CompetitionTrackA/track-A/.venv/bin/python3 -m unittest -q \
   tests.test_v3_search_control \
-  tests.test_v3_failure_evidence \
-  tests.test_openai_provider \
-  tests.test_v3_openai_planner \
-  tests.test_v3_batch_benchmark \
-  tests.test_v3_continuation \
-  tests.test_v3_continuation_admission \
-  tests.test_v3_experience_v2_runtime \
+  tests.test_v3_prototype \
   tests.test_v3_planner_action \
-  tests.test_v3_prototype -q
+  tests.test_v3_openai_planner \
+  tests.test_final_certification \
+  tests.test_v3_batch_benchmark
+
+/home/ying/CompetitionTrackA/track-A/.venv/bin/python3 \
+  -m unittest discover -s tests -t . -q
 ```

-结果：**130 passed**。覆盖 structured Proposal、Obligation/Candidate/Portfolio
-投影、failure stage 归一化、CoSim 进度、紧凑代码切片、严格 Provider schema、
-Candidate 回退、完整原型路径及 A2/A3 当前 Admission/ABSTAIN 边界。未调用
-DeepSeek，未启动 Vitis HLS。
+两条命令均以成功状态结束，quiet runner 没有输出失败。聚焦集合覆盖：失败 stage/kind/signature、
+Obligation/Proposal、动作族边界、五类 Continuation、Candidate parent/回退、B2、A2 Admission、
+A3 guided/ABSTAIN、batch provenance 与 provider rejection 封装。

-## 完整发现
+新增的 `tests.test_v3_search_control` 还明确验证：

-当前已执行：
+- `STREAM_DEPTH_INCREASE` 归一为 FIFO 动作族；
+- 无新 Evidence 的 FIFO 深度 follow-up 被拒绝；
+- stream 依赖环时只允许拓扑/顺序动作族；
+- 不同拓扑动作族仍可作为可证伪的后续实验。

-```bash
-../../track-A/.venv/bin/python3 -m unittest discover -s tests -t . -f -v
-```
+没有在上述本地测试中调用 DeepSeek 或启动 Vitis。

-第 140 项 `test_runtime_fingerprint_excludes_report_only_tools` 报错后停止；
-此前测试均通过，另有 1 项因历史不完整 020 Artifact 不可用而跳过。错误是
-`release_tools/full_agent_release_metadata.py` 故意 fail-closed：RC2 工作树没有
-发布快照所引用的历史 A2/A3 Gate、Admission 和 Store 文件。这不属于本次
-搜索控制代码的回归，也不会通过伪造 Artifact 或放宽验证来消除。
+## 004 发现后的 Prompt 合同回归

-仍有已知独立环境/历史问题：
+```bash
+/home/ying/CompetitionTrackA/track-A/.venv/bin/python3 -m unittest -v \
+  tests.test_openai_provider tests.test_v3_openai_planner tests.test_v3_search_control

-1. RC1 A2/A3 artifact 路径在 Release metadata 测试中不存在；
-2. token-policy ABC matrix 声明 `search_closeout_reserve_credits=0`，校验器仍要求正数；
-3. Full Agent manifest 引用的 A2 Admission 绑定 RC1 commit，当前 HEAD 为 RC2，故正确 fail-closed。
+/home/ying/CompetitionTrackA/track-A/.venv/bin/python3 \
+  -m unittest discover -s tests -t . -q
+```

-这些问题均未通过放宽验证、修改历史 Artifact 或伪造 Admission 处理。
+聚焦集合共 48 项通过；完整套件以成功状态结束。新增断言确认 Prompt 明确写出
+`validation_plan` 的精确数组，且只填写 `['csim']` 的响应仍被拒绝。两次命令均未调用 DeepSeek 或
+启动 Vitis。
```
