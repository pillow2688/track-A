# Phase D1/D2 状态：终态绑定与无效 Latency 修复

日期：2026-07-24  
阶段状态：已完成  
验收结论：`ACCEPTED`

## 阶段目标

本阶段只处理正式矩阵中的四个 P0：

- `v3d_fast_016`、`v3d_fast_020`：终态结果错误绑定“最后一次 proposal decision”，导致真实 FAILED 被包装为 Candidate binding ERROR。
- `v3d_fast_021`、`v3d_fast_022`：Vitis 返回零或无效 worst latency 后抛出异常，导致已经完成的 CSim/Synth 正确性事实丢失。

未重构主框架，未修改 Planner、Router、Token 策略、任务代码、测试平台、头文件或元数据；未启用 Continuation、Experience、Ranker；未访问 hidden/reference/golden。

## 冻结基线

- 开始 HEAD：`4a05763b593a527878a0056f64763126c58ee63b`
- 运行结束 HEAD：`4a05763b593a527878a0056f64763126c58ee63b`
- 分支：`feat/track-a-empty-stub-generation-smoke`
- 自动提交：否
- 冻结执行身份：`4a05763+d1-cad2cd88`
- D1 产品文件 SHA-256：`3972b4919e113a138d10dce81b7f2b54c5f11922bcdfa74478ad3c89ee9eaaa3`
- D1 产品补丁 SHA-256：`cad2cd88a37bbef02b6ad4126992f98ae4890408a3fee37a41ec236255ba621c`
- 运行前后冻结文件检查：49/49 一致

由于任务禁止自动 commit，本阶段没有伪造“新 Git SHA”；真实回归由未变 HEAD、产品补丁 SHA、运行时文件 SHA、四个任务包 SHA 和专用 runner SHA 共同冻结。

## 已完成工作

### P0-1：终态 Candidate 绑定

- 新增结构化 `TerminalCandidateBinding`。
- 终态候选按 `final → best/incumbent → baseline` 选择，不再取最后分配的 Candidate。
- Candidate decision ref 只从 Registry 的耐久 committed journal 解析。
- proposal rejection 可以保留为过程事实，但不会覆盖终态 Candidate provenance。
- result、Registry、source digest、decision journal 和 package manifest 在封装时统一校验。
- 无合法候选时显式输出 `UNKNOWN`，不伪造 Candidate ID。

### P0-2：Latency 可比性

- 新增结构化 `LatencyObservation`。
- 对 absent、null、缺失 worst、字符串、NaN、Inf、负值和 OPTIMIZE 零 latency 做安全分类。
- 性能不可比时输出 `NOT_COMPARABLE` 和原因码。
- 保留 CSim/Synth 正确性结果和当前 incumbent。
- 不计算、不写入伪造 acceleration。
- 有效 latency 仍走原有比较和晋升逻辑。

## 验证结果

- 新增专项测试：18 条。
- 聚焦测试：79/79 PASS，42.860 秒。
- 完整测试：605/605 PASS，111.636 秒。
- `compileall`：PASS。
- `git diff --check`：PASS。
- 旧 Artifact：只读，未改写。
- 新真实回归：仅 016/020/021/022；没有运行 006、018 或完整 28 题。

## 旧 Artifact Replay

| Task | 原终态 | Replay 结果 | 关键结论 |
|---|---|---|---|
| 016 | ERROR | FAILED | 从耐久 Registry/decision journal 绑定 preserved baseline；CSim/Synth PASS、CoSim FAIL 保留 |
| 020 | ERROR | FAILED | 从耐久 Registry/decision journal 绑定 preserved baseline；CSim/Synth PASS、CoSim TIMEOUT 保留 |
| 021 | ERROR | PRETERMINAL_NOT_COMPARABLE | 旧产物在终态前中断；零 latency → INVALID，保留 incumbent 和 CSim/Synth PASS，不声称不存在的终态 |
| 022 | ERROR | PRETERMINAL_NOT_COMPARABLE | 旧产物在终态前中断；零 latency → INVALID，保留 incumbent 和 CSim/Synth PASS，不声称不存在的终态 |

016/020 可从耐久事实确定真实 FAILED。021/022 的旧目录缺少异常之后的决策和封装，因此 replay 正确输出 `INSUFFICIENT_ARTIFACT`，而不是补造 DONE/FAILED。

## 四题真实回归

协议与正式矩阵一致：DeepSeek、Vitis 2025.2、repeat=1、Continuation/Experience/Ranker off、`task_contract` final、fixed Token、primary retry disabled、failure-isolating。

| Task | 唯一终态 | Stop reason | 绑定 Candidate | Latency | LLM | Token | CSim/Synth/CoSim | Credits |
|---|---|---|---|---|---:|---:|---:|---:|
| 016 | FAILED | MAX_TASK_REPAIR_ROUNDS | candidate_000 / preserved incumbent | NOT_REPORTED → NOT_COMPARABLE | 2 | 4320 | 2/1/2 | 46 |
| 020 | FAILED | MAX_TASK_REPAIR_ROUNDS | candidate_000 / preserved incumbent | NOT_REPORTED → NOT_COMPARABLE | 2 | 4796 | 2/1/2 | 46 |
| 021 | DONE | BASELINE_FINALIZED_NO_IMPROVEMENT | candidate_000 / final verified | VALID 18；候选无严格改善 | 2 | 4408 | 3/3/0 | 15 |
| 022 | DONE | CANDIDATE_PROMOTED_AND_FINALIZED | candidate_001 / final verified | VALID 2；4.5× | 2 | 5029 | 4/4/0 | 20 |

聚合：

- 唯一终态：4/4
- DONE / FAILED / ERROR：2 / 2 / 0
- LLM calls：8
- Token：18,553
- CSim / Synth / CoSim：11 / 9 / 4
- Credits：127
- 各题 wall time 合计：1,434.105 秒

成功率 50% 不是本阶段 Gate。016/020 的 HLS 修复仍未成功，但执行器已经产生真实 FAILED，且 provenance 完整，因此 P0 验收通过。

## 安全与可复现性

- 四题 Ledger 全部对账；Token、输入/输出/缓存 Token、工具次数、Credits 和 pending 均一致。
- 四题 Manifest 全部通过逐条文件大小和 SHA-256 校验。
- Manifest 覆盖终态 Candidate source 和耐久 decision journal。
- 运行目录中两个 P0 错误标记均为 0。
- 任务与运行时冻结文件运行前后 49/49 一致。
- API key 只通过环境注入，未写入冻结文件、命令 Artifact 或报告。
- Manifest 路径未包含 hidden/reference/golden。
- 四个 executor command 均只引用公开 `v3d-fast` task corpus。

## 是否允许进入修复后 28×1

P0 技术验收允许进入 Phase D3 的冻结准备，但本阶段按停止条件不启动 28×1。开始 D3 前应由用户授权将本阶段产品代码与专项测试提交成新的 Git HEAD，再重新生成基于该 HEAD 的正式冻结文件；不能把未提交快照冒充 commit SHA。

## 证据

- `docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/old-artifact-reconciliation.json`
- `docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/old-artifact-replay-after-fix.json`
- `docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/focused-tests.txt`
- `docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/full-tests.txt`
- `docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/static-verification.txt`
- `docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/phase-d2-frozen-snapshot.json`
- `docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/phase-d2-prelaunch.json`
- `docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/phase-d2-postrun.json`
- `docs/experiments/artifacts/2026-07-24-phase-d1-terminal-latency-fix/phase-d2-real-run-acceptance.json`
- `llm4hls_harness/runs/phase-d2-4a05763-d1cad2cd88-20260724T124625Z`

## 下一阶段

Phase D3：经授权提交并冻结新的 Git HEAD 后，执行修复后正式 28×1。不得在该矩阵中启用或调优 Continuation、Experience、Ranker。
