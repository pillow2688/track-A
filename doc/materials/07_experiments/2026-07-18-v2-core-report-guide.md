# V2 核心报告阅读与更新指南

- Date: 2026-07-18
- Status: current team guide
- Scope: V2 optimize 单次运行、逐轮团队复盘与 Git 发布边界

## 1. 这次发布了什么

这次只把两份核心 Markdown 复盘加入 Git：

- [官方 `dotProduct_optimize` 真实运行报告](../../../llm4hls_harness/releases/v2-team-reports-2026-07-18/official-dotproduct.md)；
- [本地 `vector_add` 真实运行报告](../../../llm4hls_harness/releases/v2-team-reports-2026-07-18/local-vector-add.md)。

它们来自真实 Vitis 2025.2 的 CSim、Synth 和 CoSim，但本轮 Proposal Provider 是本地确定性规则，不是线上 LLM。因此两份报告中的 Token 都是 0；它们适合验证 V2 的控制流和报告质量，不应被描述为“真实大模型优化成绩”。

精确工具链版本由本地源 run 的配置和 backend fingerprint 记录；官方 dotProduct 身份由受版本控制的 `task_corpus` provenance 哈希在 run 外核验。报告中的 `Task provenance=NOT_PERSISTED_IN_RUN` 是诚实限制：只看报告或 task ID，不能独立证明题目来源。

`runs/`、Vitis/XSIM 工作目录、原始工具日志、证据 JSON、锁文件和生产环境配置没有加入 Git。核心报告正文可以公开复盘；原始证据仍留在本地不可变运行目录中。

## 2. 两次真实运行得到的结果

| 项目 | 官方 dotProduct | 本地 vector_add |
|---|---:|---:|
| Baseline -> final Candidate | `candidate_000 -> candidate_000` | `candidate_000 -> candidate_001` |
| 探索结果 | Candidate 被拒绝 | Candidate 晋升 |
| Synth worst latency | `1027 -> 1027` cycles | `513 -> 258` cycles |
| Synth speedup | `1.000x` | `1.988x` |
| CoSim max latency | `1025 -> 1025` cycles | `511 -> 256` cycles |
| CoSim speedup | `1.000x` | `1.996x` |
| public proxy | `2.2125 -> 2.2125` | `1.475 -> 1.5491` |
| 总 Credits | `55` | `75` |
| Token | `0` | `0` |

官方 dotProduct 的任务预算是 40 credits，但本地为了完成 baseline、探索和 final closure，明确使用了 75 credits 的严格本地覆盖；实际消耗 55 credits。这不是一次“官方 40 credits 内合规提交”。它的 Candidate 在 Synth 后没有改善 public proxy，CoSim gate 因而跳过本轮探索 CoSim，最终保留 baseline。

本地 vector_add 的 Candidate 将保守的流水线配置改为更积极的配置，Synth 和 CoSim 都测得约 2 倍加速；它通过比较后晋升，并在独立 final closure 中再次通过 CSim、Synth 和 CoSim。

## 3. V2 中数据是怎样流动的

```text
已验证 baseline
  -> Selector 依据当前指标选择一种优化类
  -> Proposal Provider 返回假设、预期、风险和 Patch
  -> Harness 检查 Patch 范围并创建 Candidate
  -> Harness 调 CSim 验证公开功能
  -> Harness 调 Synth 得到 latency、II、clock 和资源
  -> CoSim gate 判断是否值得消费 CoSim credits
  -> 必要时由 Harness 调 CoSim 验证 RTL 行为和周期
  -> scorer/comparator 产生分数并决定晋升或拒绝
  -> 下一轮继续，或进入独立 final closure
```

这里有三个必须分开的角色：

- Provider 的 hypothesis/expected effect 是“提案”，不是硬件实测事实；
- CSim、Synth、CoSim 是 Harness 根据状态机调用的工具，不是 LLM 自己调用；
- Candidate 晋升、拒绝、停止和 fallback 是确定性机器决策。

## 4. 怎样读一份报告

建议按以下顺序阅读：

1. **30 秒结论**：先看终态、停止原因、baseline/best/final、latency、代理分、Token 和 Credits。
2. **职责与任务预判**：确认题型、`requires_cosim`、part、clock 和预算覆盖，避免把本地实验误说成官方成绩。
3. **Baseline 数据流**：确认原始代码是否真的通过 CSim、Synth、CoSim，以及起始性能是多少。
4. **全局轮次时间线**：查看每轮 parent、Candidate、工具状态、gate、决策和 best-after。
5. **逐轮详细卡片**：把“为什么选这个分支、Provider 说了什么、Patch 实际改了什么、Vitis 测到什么、机器为何晋升/拒绝”连起来。
6. **Candidate tree**：确认 Candidate 谱系，特别是拒绝后是否继续从当前 best 出发。
7. **CoSim 专项复盘**：区分 `EXECUTED`、`SKIPPED_BY_GATE` 和 `NOT_REACHED`。
8. **Final closure**：最终提交对象必须独立重跑，不可拿探索期缓存冒充最终结果。
9. **预算守恒与附录**：核对每阶段 Credits、Tokens、Prompt、diff 和证据引用。

## 5. 核心字段如何理解

- **Synth latency**：HLS 综合估计的周期数；同一题、同一接口和时钟约束下通常越低越好。
- **CoSim latency**：生成 RTL 后由仿真测得的周期数；它比 CSim 更接近硬件实现行为。
- **public proxy**：根据公开信息计算的搜索信号，用于本地选择 Candidate，不是官方 hidden 最终分。
- **PPA**：Performance、Power、Area。本项目当前可直接观察 latency/II/clock 和资源占用；没有真实板级功耗测量时，不能声称已经测到完整 Power。
- **Credits**：Harness 对 CSim、Synth、CoSim 和 Provider 调用使用的成本单位，用来控制搜索预算。
- **Token**：Provider 的模型输入/输出用量。本地规则不调用模型，所以是 0。
- **CoSim gate**：先用较便宜的 CSim/Synth/代理分筛选，再决定是否支付昂贵的 CoSim 成本。

## 6. 本地如何重新生成报告

以下命令只读已封存的运行证据，不会重新调用 Provider 或 Vitis，也不能把输出写回原 run：

```bash
cd llm4hls_harness

python3 -m llm4hls_agent report-v2-run \
  --run-dir runs/v2-real-official-dotproduct-20260718-retry1 \
  --output /tmp/v2-real-official-dotproduct.md

python3 -m llm4hls_agent report-v2-run \
  --run-dir runs/v2-real-local-vector-add-20260718 \
  --output /tmp/v2-real-local-vector-add.md
```

离线报告会先验证源 run 的 Artifact Manifest，并且只读取 Manifest 白名单内的证据。发布到 Git 的核心 Markdown 是报告副本，不代替源 run，也不构成源 run 的新 Manifest。

## 7. Git 提交边界

允许提交：

- 核心 Markdown 报告；
- 设计、实施、阅读和复盘文档；
- 不含生产路径或秘密的确定性测试 fixture。

禁止提交：

- `runs/` 整体；
- `actions/*/work/`、`.Xil/`、XSIM、Vitis 生成工程和大型二进制；
- API key、Authorization header、原始私密模型响应；
- 本机绝对路径、生产环境变量和生产配置；
- 为精简包复制但无法通过完整校验的残缺源 `artifact_manifest.json`。

如果以后确实要共享完整证据，应单独设计脱敏、可校验的 evidence bundle；不能把部分原始文件称为完整、不可变 run。

## 8. 下一步怎么优化

- 用真实 OpenAI-compatible Provider 重跑至少一个题目，验证非零 Token、模型声明和 Vitis 实测偏差。
- 扩充官方题目覆盖，分别测试 `requires_cosim=false` 与 `requires_cosim=true`。
- 对 dotProduct 改进 Selector/Prompt：本次简单增加 pipeline pragma 没有降低 transaction interval，说明需要识别归约依赖、数据类型和展开策略。
- 保留 CoSim gate：本次无改善 Candidate 被提前拦截，节省了 20 credits。
- 报告继续作为团队复盘入口；官方提交分数必须由比赛 hidden grader 独立计算。

## 9. 相关设计文档

- [V2 逐轮团队复盘报告设计](../../../docs/superpowers/specs/2026-07-18-v2-team-run-report-design.md)
- [V2 团队报告实施计划](../../../docs/superpowers/plans/2026-07-18-v2-team-run-report-implementation.md)
- [Experiments 文档约定](README.md)
