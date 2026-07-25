# Phase D1/D2 实验报告：终态绑定与无效 Latency 修复

日期：2026-07-24  
实验结论：`ACCEPTED`

## 1. 阶段目标与边界

本阶段处理正式 28×1 暴露出的四个执行器 P0，不以“让四题 HLS 全部成功”为目标：

1. 016/020：真实结构修复流程结束后，终态 Candidate provenance 封装失败。
2. 021/022：候选 CSim/Synth 已通过，但 invalid worst latency 使 Comparator 抛异常。

修改范围限定为通用执行器逻辑和测试。没有修改任务答案、testbench、header、metadata、Router、Planner prompt、Token 策略或横向组件 Gate。

## 2. 正式矩阵失败基线

| Task | 原状态 | 原错误 | 旧 Ledger |
|---|---|---|---|
| 016 | ERROR | terminal result does not bind the last Candidate decision | 4345 Token；2 LLM；2 CSim；1 Synth；2 CoSim；46 Credits |
| 020 | ERROR | terminal result does not bind the last Candidate decision | 4764 Token；2 LLM；2 CSim；1 Synth；2 CoSim；46 Credits |
| 021 | ERROR | candidate worst latency is invalid | 4345 Token；2 LLM；3 CSim；3 Synth；0 CoSim；15 Credits |
| 022 | ERROR | candidate worst latency is invalid | 5540 Token；2 LLM；3 CSim；3 Synth；0 CoSim；15 Credits |

旧 run 均未完成 package manifest/terminal sealing。旧目录保持只读。

## 3. 016/020 根因

旧流程把两个语义不同的引用放在同一个 `decision_ref`：

- Candidate 生命周期的耐久 committed journal；
- proposal duplicate/policy rejection 的过程记录。

016/020 的 materialized Candidate 已因 CoSim FAIL/TIMEOUT 被真实 REJECT；随后 round 2 proposal 在 materialize 前被拒绝，并把共享 `decision_ref` 覆盖为 `control/proposal_rejections/round_002.json`。Registry 的最后一次耐久 Candidate operation 仍指向 committed journal。报告封装要求两者一致，于是把本应为 FAILED 的任务升级成 ERROR。

这不是“最后分配的 Candidate 应成为 final”。正确语义是：没有 final 时保留 best/incumbent；终态 provenance 必须绑定 Registry 的耐久 Candidate journal。

## 4. 021/022 根因

旧候选的 CSim 和 Synth 均 PASS，但 Vitis synthesis report 给出 worst latency=0。原 `_worst_latency` 路径把它视为不可接受值并抛异常，异常发生在 Candidate 决策和终态封装之前，因此：

- 已获得的 CSim/Synth 正确性事实没有进入终态；
- incumbent 没有被正常保留；
- 没有写出明确的性能不可比状态；
- run 以 ERROR 结束。

零值在 OPTIMIZE 性能比较中不能用于 acceleration；正确行为是 `INVALID/NOT_COMPARABLE`，保留正确性，拒绝该性能候选，不伪造数值。

## 5. 实现修改

### 5.1 TerminalCandidateBinding

新增终态绑定结构，字段覆盖：

- Candidate ID、parent ID；
- source ref 和 source SHA-256；
- binding source；
- required validation actions 和逐阶段状态；
- promotion status、selection reason；
- committed Candidate decision ref；
- Registry revision；
- binding payload SHA-256。

终态选择顺序固定为：

```text
final verified candidate
→ best / incumbent
→ baseline
→ no legal candidate / UNKNOWN
```

封装时重新加载耐久 Registry，通过 `v3_last_operation_id + v3_revision` 解析 committed journal。过程 proposal rejection 不再被解释为 Candidate decision。若 state ref 与耐久 journal 不同，则记录 `terminal_reconciliation`，同时把 result 顶层 `decision_ref` 重绑定到耐久 journal。

### 5.2 LatencyObservation

新增显式分类：

- `VALID`
- `MISSING`
- `INVALID`
- `NOT_REPORTED`

Comparator 输出独立的 `COMPARABLE/NOT_COMPARABLE`。以下输入不会再抛出未封装异常：

- 字段 absent；
- null；
- worst 缺失；
- 字符串；
- NaN、+Inf、-Inf；
- 负数；
- OPTIMIZE 中的零 latency。

不可比较时：

- `latency_worst = null`
- `acceleration_vs_baseline = null`
- 写入 reason codes
- 保留 Candidate CSim/Synth 结果
- 保留 incumbent
- 继续生成真实终态

## 6. 测试

新增 18 条专项测试，覆盖：

- 所有无效 latency 类型；
- 无效 latency 的完整 Graph 降级；
- selected final、preserved incumbent、无合法候选三类终态绑定；
- 016/020 类 round 2 proposal rejection 覆盖场景；
- 报告封装中断/恢复且不重放工具；
- binding digest 确定性；
- 禁止 task ID 硬编码。

结果：

| 验证 | 结果 |
|---|---|
| 聚焦测试 | 79/79 PASS，42.860s |
| 完整测试 | 605/605 PASS，111.636s |
| compileall | PASS |
| git diff --check | PASS |

## 7. 旧 Artifact 确定性 Replay

Replay 只读取旧 run，不改写原结果。

### 7.1 016/020

两题均有足够耐久事实：

- Registry best/incumbent=`candidate_000`
- committed Candidate journal 存在且 Registry hash/revision 可绑定
- baseline CSim/Synth PASS
- baseline CoSim 分别 FAIL/TIMEOUT
- 无 final Candidate

因此 replay 可确定真实终态为 FAILED，acceleration=null。

### 7.2 021/022

旧异常发生在 candidate_002 的 latency 比较阶段，Registry 仍处于未完成更新状态。Replay 可确定：

- latency raw=0
- latency status=`INVALID`
- performance=`NOT_COMPARABLE`
- acceleration=null
- candidate_002 的 CSim/Synth PASS
- candidate_001 incumbent 必须保留

但旧产物没有异常之后的决策和终态封装，所以 replay 返回 `PRETERMINAL_NOT_COMPARABLE / INSUFFICIENT_ARTIFACT`，不伪造 DONE 或 FAILED。

## 8. 新冻结基线

用户禁止自动 commit，因此本实验没有生成新 commit。

| 项 | 值 |
|---|---|
| Git HEAD | `4a05763b593a527878a0056f64763126c58ee63b` |
| 产品补丁 SHA-256 | `cad2cd88a37bbef02b6ad4126992f98ae4890408a3fee37a41ec236255ba621c` |
| 产品文件 SHA-256 | `3972b4919e113a138d10dce81b7f2b54c5f11922bcdfa74478ad3c89ee9eaaa3` |
| Snapshot ID | `4a05763+d1-cad2cd88` |
| 运行前后文件 Gate | 49/49 一致 |

该组合是本次四题实验的冻结身份，不应称为新的 commit SHA。

## 9. 四题真实回归协议

```text
model                  deepseek-v4-pro
backend                Vitis 2025.2
repeat                 1
validation profile     fast-experiment
final                  task_contract
continuation           off
experience             off
ranker                 off
token policy           fixed
run token limit        32768
planner rounds         2
max output tokens      4096
temperature / top_p    0.0 / 1.0
final reserve          25 credits
CSim/Synth/CoSim cost  1 / 4 / 20
CSim/Synth/CoSim limit  300 / 900 / 600 s
run limit              3600 s
primary retry          disabled
failure isolation      enabled
```

只选择 016/020/021/022，使用全新 batch/run 目录。

## 10. 四题真实结果

### 10.1 016

- 终态：FAILED / MAX_TASK_REPAIR_ROUNDS
- Router：STRUCTURAL_FIX
- 绑定：candidate_000 / PRESERVED_INCUMBENT
- correctness：CSim PASS、Synth PASS、CoSim FAIL
- 候选性能：NOT_REPORTED → NOT_COMPARABLE
- acceleration：null
- Manifest：66 条 Artifact 全部 hash/size 通过
- Ledger：4320 Token；2 LLM；2 CSim；1 Synth；2 CoSim；46 Credits

### 10.2 020

- 终态：FAILED / MAX_TASK_REPAIR_ROUNDS
- Router：STRUCTURAL_FIX
- state 曾指向 round 2 proposal rejection；封装时真实重绑定 committed Candidate journal
- 绑定：candidate_000 / PRESERVED_INCUMBENT
- correctness：CSim PASS、Synth PASS、CoSim TIMEOUT
- 候选性能：NOT_REPORTED → NOT_COMPARABLE
- acceleration：null
- Manifest：64 条 Artifact 全部 hash/size 通过
- Ledger：4796 Token；2 LLM；2 CSim；1 Synth；2 CoSim；46 Credits

### 10.3 021

- 终态：DONE / BASELINE_FINALIZED_NO_IMPROVEMENT
- 绑定：candidate_000 / FINAL_VERIFIED_CANDIDATE
- final CSim/Synth：新鲜 PASS
- 本次候选 worst latency=18，为有效但无严格改善
- acceleration：候选比较 1.0×，未作为改善晋升
- Manifest：82 条 Artifact 全部 hash/size 通过
- Ledger：4408 Token；2 LLM；3 CSim；3 Synth；0 CoSim；15 Credits

### 10.4 022

- 终态：DONE / CANDIDATE_PROMOTED_AND_FINALIZED
- 绑定：candidate_001 / FINAL_VERIFIED_CANDIDATE
- final CSim/Synth：新鲜 PASS
- worst latency=2，性能可比
- acceleration：4.5×，来自有效正 latency
- Manifest：104 条 Artifact 全部 hash/size 通过
- Ledger：5029 Token；2 LLM；4 CSim；4 Synth；0 CoSim；20 Credits

021/022 本次随机 Planner 输出没有再次产生零 latency。报告不把它伪装成“真实 invalid 分支再次触发”；invalid 分支的准入证据由旧 Artifact replay、专项测试和完整测试共同提供。

## 11. 聚合预算与 Manifest

| 项 | 聚合 |
|---|---:|
| LLM calls | 8 |
| Token | 18,553 |
| CSim | 11 |
| Synth | 9 |
| CoSim | 4 |
| Credits | 127 |
| Wall time | 1,434.105s |

四题均满足：

- Budget result 与 ledger 的非时间字段完全一致；
- runtime 仅在封装后单调增加不足 5 秒，四题实际约 0.1 秒；
- 无 pending token/credit/tool reservation；
- Tool count 与 cost 公式一致；
- Manifest 文件 digest、逻辑 digest、逐条 Artifact digest/size 全部通过；
- Manifest 覆盖终态 source 和 committed Candidate decision。

## 12. 安全边界

- 任务代码：未修改。
- Router/Planner/Token policy：未修改。
- C0/C1/C2 runtime：关闭。
- API key：环境注入，不落盘。
- hidden/reference/golden：无 Manifest 路径访问。
- 旧正式 run：未改写。
- 新运行：没有 retry，没有复用正式矩阵目录。
- P0 标记：新运行目录中均未出现。

## 13. P0 验收结论

结论：`ACCEPTED`

四题均有唯一明确终态；016/020 不再因 Candidate binding 进入 ERROR；021/022 不再因 latency 处理进入 ERROR；正确性事实、性能可比性、Ledger、Manifest 和冻结边界全部通过。

本阶段立即停止，不启动修复后 28×1。下一阶段为 Phase D3：经授权提交修复形成新 HEAD，重新冻结后执行正式 28×1。
