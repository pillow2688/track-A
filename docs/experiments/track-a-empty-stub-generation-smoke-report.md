# Track A：真实空 Stub Generation Smoke（2026-07-22）

## 结论

这次验收通过。`track_a_empty_stub_generation` 是一个公开、development-only 的真正空 Stub 任务：初始 `kernel.cpp` 只有 TODO 和 `(void)` 占位，不含循环、归约或输出赋值。真实 `deepseek-v4-pro` 在一次调用中生成了完整的矩阵乘加、饱和和行和算法；真实 Vitis 2025.2 对 Candidate 与 fresh final 均验证通过。

这不是官方 benchmark，也不应据此推断官方最终成绩。它只证明当前 Track A 主路径能够在不改变固定接口的前提下，处理“需要从空实现生成主要 kernel body”的公开 Generation 入口。

## 版本与范围

| 项目 | 值 |
|---|---|
| Branch | `feat/track-a-empty-stub-generation-smoke` |
| Fixture implementation commit | `a0ed8b25bb4d21769bbfef7f970ca7c82c2ad2e1` |
| Fixture | `llm4hls_harness/task_corpus/development/track_a_empty_stub_generation/` |
| Task type / routed mode | `generate` / `REPAIR` |
| `generation_required` | `true` |
| Official difficulty | `UNKNOWN`（只记录 `development_difficulty=medium`，不冒充官方难度） |
| `requires_cosim` | `false` |
| Planner | 真实 OpenAI-compatible `deepseek-v4-pro` |
| HLS backend | 真实 Vitis 2025.2，`vitis-run --mode hls` |
| Platform / clock | `xcu55c-fsvh2892-2L-e` / 5 ns |
| Validation | `fast-experiment` + `task_contract` fresh final |
| Continuation / Experience | `shadow` / `shadow`（本轮没有可注入经验） |
| Power | `UNSUPPORTED`；未构造功耗 proxy |

本次没有新增 LangGraph 节点或 Agent，也没有修改主 Graph、BudgetLedger、CandidateManager、ToolServer、Checkpoint 或 fresh-final 规则。

## Fixture 为什么是真正的 Generation

公开任务要求实现 `matrix_transform`：

1. 对 4×4 的 `lhs`、`rhs` 做矩阵乘加并加上列 bias；
2. 将每个输出饱和到公开常量区间；
3. 计算每行已饱和输出的 `row_sums`。

初始 kernel 只保留顶层接口和以下占位逻辑：

```cpp
// TODO: generate the complete public matrix-transform implementation.
(void)lhs;
(void)rhs;
(void)bias;
(void)output;
(void)row_sums;
```

公开 testbench 包含 5 组独立生成的输入，并由独立的 C++ reference calculation 比较输出；它不是一个可通过固定答案通过的单样例。任务目录仅包含 `task.toml`、说明、kernel、header 和公开 testbench；没有 hidden、reference 或 golden 文件。

## 真实执行路径

Run：[`track_a_empty_stub_generation_20260722_A01`](../../llm4hls_harness/runs/track_a_empty_stub_generation_20260722_A01)

```text
baseline CSim FAIL（空 stub 未写输出）
  → PhaseRouter = REPAIR
  → DeepSeek Planner（1 次）
  → Patch Validator + TopInterfaceGuard PASS
  → Candidate CSim PASS
  → Candidate Synth PASS
  → fresh final CSim PASS
  → fresh final Synth PASS
  → task_contract：CoSim NOT_RUN（任务不要求）
  → DONE
```

真实 Planner 的假设是“空 stub 没有初始化 `output` 和 `row_sums`”。它新增：

- `clamp_value`：实现饱和；
- `compute_raw_cell`：实现单元格内层乘加归约；
- 顶层双层矩阵循环：写 `output` 并汇总 `row_sums`。

因此这不是 V3-D `v3d_fast_004` 那类一行索引修复，而是对空主体生成主要算法。

## Patch 与接口保护

| 检查 | 结果 |
|---|---|
| Patch | 1 hunk；27 行新增、6 行删除，共 33 行变更 |
| 普通 repair 限制 | 30 行；同样大 patch 被单测拒绝 |
| Generation 例外 | `generation_required=true` 允许受限的大 kernel-body patch |
| 修改范围 | 仅 `kernel.cpp` |
| Header / testbench / metadata | 未修改 |
| TopInterfaceGuard | PASS；baseline 与 candidate 的 canonical top signature 完全一致 |
| Header SHA-256 | `d36e4d516265a269faf647afa745456ad7a79c6a42697e813d49d00c9b640a92` |
| Public TB SHA-256 | `67eec7ce9bd872032bbf8b95c4ecf0588cd83de6b1fba986677e50fefcc95889` |
| Final source SHA-256 | `5786d0cf139ddd793c2da04f097872bb6d0d4a0254c9a8291374ca8f5f3786f6` |

聚焦测试还证明：普通 repair 大 patch 拒绝、generate 大 kernel patch 允许、header/testbench/top signature 修改拒绝、unknown `task_type` fail-closed。Planner-context 测试额外放置一个带唯一 marker 的 forbidden 子目录，确认该 marker 和 testbench 内容均不进入 Planner context。

## 真实性、工具与资源证据

| 阶段 | CSim | Synth | CoSim | 说明 |
|---|---|---|---|---|
| Baseline | FAIL | NOT_RUN | NOT_RUN | 行和仍为 sentinel `777777` |
| Candidate `candidate_001` | PASS | PASS | NOT_RUN | mode=REPAIR 的正确性验证路径 |
| Fresh final `candidate_001` | PASS | PASS | NOT_RUN | `requires_cosim=false` 且 `task_contract`，因此正确跳过 |

Final Synth：

| 指标 | 值 |
|---|---:|
| Worst latency | 21 cycles |
| Transaction interval | 22 cycles |
| Loop achieved II | 2 |
| Trip count | 4 |
| Estimated clock | 3.378 ns（满足 5 ns target） |
| LUT / FF / DSP / BRAM / URAM | 1580 / 1227 / 24 / 0 / 0 |

工具链 receipt 指向 Vitis `vitis-run v2025.2`（SW Build 6295257），并固定在 run artifact 中。`v3_prototype_result.json` SHA-256 为 `a2769ac9e7234e205da4e171c13743dee70fd79a30a7df9dbb11f73ce9263abb`，package manifest SHA-256 为 `f77a4880e1756a899a1cf94b19c8f63cd682cddf1a731611d9e8919ec9876788`。

## Token、Credit 与时间

| 项目 | 值 |
|---|---:|
| Planner calls | 1 |
| Input / output / total Token | 1438 / 657 / 2095 |
| CSim / Synth / CoSim | 3 / 2 / 0 |
| Agent search Credit | 6 |
| Internal fresh-final Credit | 5 |
| Total internal Credit | 11 / 80 |
| Wall time | 50.6 s |
| Stop reason | `REPAIR_FINALIZED` |

没有复用 Planner response、Candidate、Patch、tool action cache 或 final result。经验层处于 shadow，并因 seed 为空而 `ABSTAIN`；它没有改变 Prompt、路由、预算或 Candidate 晋升。

## 回归与边界

已执行：

```bash
PYTHONPATH=llm4hls_harness .venv/bin/python -m compileall -q llm4hls_harness/llm4hls_agent
PYTHONPATH=llm4hls_harness .venv/bin/python -m unittest discover -s llm4hls_harness/tests -p 'test_*.py' -q
git diff --check
```

完整 unittest 通过（此前 550 项，加本阶段 5 项聚焦测试，合计不少于 555）；编译检查与 diff 检查通过。主 Graph、默认 `continuation=shadow`、`experience=shadow`、Power=UNSUPPORTED、`task_contract` final policy 均保持不变。

## 是否进入 28 题单次覆盖

这个 Gate **通过**：真实 Planner 能从公开空 Stub 生成主要算法、通过 Candidate 与 fresh-final 验证，并保持 kernel-only/interface 保护和正确记账。

它只移除了“Generation 入口未被真实验收”的阻塞，不代表 28 题已经具有统计意义的成功率。下一阶段若启动，应使用当前安全基线进行 **28 tasks × 每题 1 次**，不启用 Continuation enforce、Experience guided 或训练型策略。
