# Track A 当前系统说明与团队月度进展

- 更新时间：2026-07-20
- 当前代码分支：`feat/v3c-task-aware-router`
- Agent 功能代码基线：`d56a2ed`（V3-C task-aware routing）
- 报告与统一测试入口版本：查看 `git log -1 -- doc/docs/2026-07-20-current-system-and-team-progress.md`
- 发布状态：当前仅在本地功能分支提交，尚未合并或推送到远端
- 面向读者：第一次接触项目的组员、需要复盘实验的开发者、论文与演示负责人
- 文档定位：当前事实入口。历史设计文档用于解释“为什么这样设计”，本报告说明“代码现在实际能做什么”

## 1. 先看结论

我们正在做一个自动修改 HLS C++ 的程序。它收到一道题后，会先判断代码哪里有问题，再让大模型提出一个 Patch，最后调用 Vitis 验证修改是否正确、能否综合、是否更快。

截至 2026-07-20，最准确的项目状态是：

1. V0、V1、V2 已经完成各自的工程闭环：V0 保留真实 Vitis baseline 证据，V1/V2 保留真实 LLM + Vitis 验收证据。
2. V3-A 已把工具节点和 scripted Planner 流程迁移到可 checkpoint 的 LangGraph；V3-B 进一步加入真实、不可重放 LLM 请求的 STARTED/COMPLETED action 记录与恢复边界。
3. V3-B 已接入真实 OpenAI-compatible Planner，并在官方 `dotProduct_optimize` 上得到一次真实成功结果：Synth worst latency 从 `1027` 降到 `38 cycles`，最终 CSim、Synth、CoSim 全部通过。
4. V3-C 已加入任务分诊能力，能够区分功能修复、综合修复、RTL 结构修复和性能优化。
5. V3-C 当前通过了完整单元测试和三道官方题目的确定性编排烟测，但还没有用真实 LLM + Vitis 完成 `REPAIR`、`SYNTH_FIX`、`STRUCTURAL_FIX` 三种模式的专项验收。
6. 当前最大的工作重点已经不是继续扩建框架，而是补真实实验、模型对比、提交材料和复现环境。

一句话概括：

> 系统骨架已经能跑，优化路径已经出现真实强结果；现在需要证明它不只会做 dotProduct 优化，也能稳定修复其他类型的官方任务。

## 2. 用最通俗的话理解整个系统

可以把 Agent 看成一家小型医院：

| 项目组件 | 通俗比喻 | 实际职责 |
|---|---|---|
| PhaseRouter | 分诊台 | 判断病人应该去功能修复、综合修复、结构修复还是优化科 |
| LLM Planner | 提方案的工程师 | 根据代码和有限证据提出一个修改假设与 Patch |
| Patch Validator | 安检员 | 禁止修改 header、testbench 和其他文件，并限制 Patch 格式与大小 |
| Candidate Manager | 样品仓库 | 保存 baseline、Candidate、父子关系，以及 Graph 写入的 best 状态 |
| CSim | 软件功能检查 | 检查 C/C++ 输出在公开测试上是否正确 |
| Synth | 硬件生成和体检 | 检查能否生成 RTL，并给出 latency、II、时钟和资源 |
| CoSim | RTL 实机前检查 | 检查生成的 RTL 是否与 C 行为一致，是否死锁 |
| Budget Ledger | 账房 | 记录 Token、Credits、工具次数和时间，预算不足就禁止继续 |
| Checkpoint | 存档点 | 中断后从已完成节点继续，避免重复花钱 |
| Final closure | 出厂质检 | 对最终代码重新运行全新的 CSim、Synth、CoSim |

这里最重要的边界是：

- 大模型只能提建议和 Patch；
- 大模型不能自己调用 Vitis；
- 大模型不能批准预算；
- 大模型不能决定 Candidate 晋升；
- 最终结果只由程序根据真实工具证据确定。

## 3. 一道题实际怎样流动

### 3.1 纵向主流程

“纵向”指一道题从进入系统到输出最终代码所经历的先后步骤：

```text
读取公开题目
  ↓
保存不可变 baseline
  ↓
运行 baseline 验证
  ↓
PhaseRouter 判断任务模式
  ↓
LLM Planner 提出一个 Patch
  ↓
Patch 安全检查
  ↓
创建隔离 Candidate
  ↓
按任务模式运行验证
  ↓
晋升、拒绝或继续下一轮
  ↓
选择当前 best
  ↓
全新的 Final CSim → Synth → CoSim
  ↓
写入机器结果和团队报告
```

PhaseRouter 的四个出口如下：

```text
Baseline CSim FAIL
  → REPAIR

Baseline CSim PASS，但 Synth/时钟/资源 FAIL
  → SYNTH_FIX

Baseline CSim、Synth PASS，但需要的 CoSim FAIL
  → STRUCTURAL_FIX

所需 baseline 检查全部通过
  → OPTIMIZE
```

安全扩展：在 strict profile 中，即使题目没有声明 `requires_cosim=true`，只要系统实际运行了 optional baseline CoSim 且观察到失败，也会保守地进入 `STRUCTURAL_FIX`，原因记为 `OBSERVED_OPTIONAL_BASELINE_COSIM_FAILED`。

各模式的探索验证不同：

| 模式 | 当前目标 | Candidate 探索阶段 |
|---|---|---|
| `REPAIR` | 修复功能、编译或运行结果 | CSim → Synth → 题目要求时 CoSim |
| `SYNTH_FIX` | 修复不可综合、时钟或资源问题 | CSim → Synth |
| `STRUCTURAL_FIX` | 修复 deadlock、stream、FIFO、RTL mismatch | CSim → CoSim |
| `OPTIMIZE` | 在正确前提下降低 latency/PPA | CSim → Synth → 风险/收益 CoSim gate |

无论探索阶段走哪个模式，最终提交对象都必须重新运行完整的 CSim、Synth、CoSim。

### 3.2 横向公共组件

“横向”指它们不只属于某一步，而是从开始到结束一直保护主流程。

| 横向组件 | 它怎样影响纵向流程 |
|---|---|
| Task Loader | 限制系统只能读取公开 kernel、header、description 和公开 testbench |
| Budget Ledger | 每次模型、CSim、Synth、CoSim 前判断是否还有预算 |
| Candidate Manager | 负责代码版本的创建、保存和父子关系；晋升/回退由 Graph 决策节点决定 |
| Patch Validator | 在创建 Candidate 前拒绝越权 Patch |
| ToolServer | 所有 Vitis 调用必须经过它，统一计费、缓存和审计 |
| Evidence Extractor | 把大段工具报告压缩成有限、结构化事实供 Planner 使用 |
| Scoring/Comparator | Synth 后判断优化 Candidate 是否严格优于当前 best |
| CoSim gate | 避免给没有性能提升或低价值的 Candidate 浪费 20 Credits |
| Checkpoint | 每个 LangGraph 节点结束后保存状态，支持恢复 |
| Report/Manifest | 最后把代码、模型、工具、预算和决策串成可复盘证据 |

可以简单理解为：

```text
纵向流程 = 车从起点开到终点

横向组件 = 导航、油表、收费站、仓库、安检和行车记录仪
```

## 4. 当前代码从哪里开始看

项目现在有两套入口并存，这是新人最容易混乱的地方。

| 要运行的阶段 | 命令入口 | 主要代码 |
|---|---|---|
| V0、V1、V2 | `python -m llm4hls_agent ...` | `llm4hls_agent/cli.py` |
| V3-B、V3-C | `llm4hls-v3-prototype` | `v3_prototype_cli.py` + `v3_prototype.py` |

V3-C 不是第三套新程序。它是在 V3-B 的同一张 LangGraph 中加入 PhaseRouter 和修复分支。

当前若只想理解最新系统，建议阅读顺序是：

1. 本报告；
2. 一个官方任务的 `task.toml` 和 kernel；
3. `v3_prototype_cli.py`；
4. `v3_prototype.py` 中的 `build_v3_prototype_graph()`；
5. `v3_phase_router.py`；
6. `test_v3_task_aware_smoke.py`；
7. `budget.py`、`candidate.py`、`tools.py`；
8. `v3_openai_planner.py` 和 `openai_provider.py`。

不要从头通读 `v3_prototype.py`。它是总装文件，应先看 Graph 节点名称，再按节点查函数。

## 5. 什么文件负责什么

### 5.1 启动、任务和主流程

| 文件 | 一句话职责 | 一般什么时候看 |
|---|---|---|
| [`pyproject.toml`](../../llm4hls_harness/pyproject.toml) | 定义 Python 包、V3 可选依赖和命令行入口 | 安装失败或命令找不到时 |
| [`cli.py`](../../llm4hls_harness/llm4hls_agent/cli.py) | V0/V1/V2 的旧入口和验收/report 命令 | 维护旧阶段时 |
| [`v3_prototype_cli.py`](../../llm4hls_harness/llm4hls_agent/v3_prototype_cli.py) | V3 参数入口，读取 task、模型配置、Vitis 路径和验证模式 | 修改 V3 命令参数时 |
| [`task.py`](../../llm4hls_harness/llm4hls_agent/task.py) | 读取公开任务并拒绝 hidden/reference 路径 | 增加任务格式或排查加载失败时 |
| [`workflow.py`](../../llm4hls_harness/llm4hls_agent/workflow.py) | V0 的不可变 baseline 和三工具基础闭环 | 理解底层运行规则时 |
| [`v3_prototype.py`](../../llm4hls_harness/llm4hls_agent/v3_prototype.py) | 当前 V3 LangGraph 总流程、节点、路由、final 和报告 | 修改 V3 数据流时 |

### 5.2 任务分诊和 Evidence

| 文件 | 一句话职责 | 明确不负责什么 |
|---|---|---|
| [`v3_phase_router.py`](../../llm4hls_harness/llm4hls_agent/v3_phase_router.py) | 根据 baseline 工具结果选择四种模式 | 不调用 LLM，不修改代码，不批准工具 |
| [`v3_failure_evidence.py`](../../llm4hls_harness/llm4hls_agent/v3_failure_evidence.py) | 提取 CSim/Synth/CoSim 的有限失败事实 | 不发送完整日志，不猜不存在的错误 |
| [`v3_evidence.py`](../../llm4hls_harness/llm4hls_agent/v3_evidence.py) | 提取 Synth 的 latency、loop II、TripCount、调度和 memory 证据 | 不把 transaction interval 当成 loop II |

### 5.3 Planner 和模型 API

| 文件 | 一句话职责 | 关键区别 |
|---|---|---|
| [`openai_provider.py`](../../llm4hls_harness/llm4hls_agent/openai_provider.py) | 组装 Prompt、调用 OpenAI-compatible API、校验严格 JSON、记录 Token | 真正与模型服务通信 |
| [`v3_openai_planner.py`](../../llm4hls_harness/llm4hls_agent/v3_openai_planner.py) | 把 V3 当前状态、代码、Evidence、历史和预算整理给 Provider | 决定模型能看见哪些 V3 信息 |
| [`v3_planner.py`](../../llm4hls_harness/llm4hls_agent/v3_planner.py) | 定义 Planner 输入、输出、哈希和 scripted Planner | 负责数据契约，不发 HTTP 请求 |
| [`v3_planner_action.py`](../../llm4hls_harness/llm4hls_agent/v3_planner_action.py) | 持久记录模型请求开始、完成、Token 和崩溃恢复状态 | 防止不可重放 LLM 调用被静默重复 |

最容易混淆的一点：

```text
v3_openai_planner.py = 决定给模型看什么
openai_provider.py   = 真正把请求发给模型
```

### 5.4 Candidate、Patch、预算和工具

| 文件 | 一句话职责 | 出问题时的典型现象 |
|---|---|---|
| [`repair.py`](../../llm4hls_harness/llm4hls_agent/repair.py) | 检查、规范化和应用 unified diff | Patch 越权、hunk 不匹配、改动过大 |
| [`candidate.py`](../../llm4hls_harness/llm4hls_agent/candidate.py) | 原子创建不可变 Candidate，维护父子关系和验证记录 | Candidate ID、best、回退不一致 |
| [`budget.py`](../../llm4hls_harness/llm4hls_agent/budget.py) | 追加式记录 Credit、Token、次数和时间 | 预算不足、重复计费、账本不一致 |
| [`tools.py`](../../llm4hls_harness/llm4hls_agent/tools.py) | CSim/Synth/CoSim 唯一受预算控制的调用入口 | 缓存、action 绑定、工具结果审计失败 |
| [`vitis.py`](../../llm4hls_harness/llm4hls_agent/vitis.py) | 真正启动 Vitis 2025.2 并解析报告 | Vitis/XSIM、license、超时、报告解析问题 |
| [`validation.py`](../../llm4hls_harness/llm4hls_agent/validation.py) | V1/V2 共用的整段验证助手 | 旧流程 Candidate 验证问题 |

### 5.5 优化、评分和报告

| 文件 | 一句话职责 | 当前地位 |
|---|---|---|
| [`optimization.py`](../../llm4hls_harness/llm4hls_agent/optimization.py) | V2 优化主循环、旧 Selector 和 CoSim gate | 稳定旧实现，不再扩展其 optimize loop |
| [`scoring.py`](../../llm4hls_harness/llm4hls_agent/scoring.py) | 检查验证等级、时钟、资源和 PPA，比较 Candidate | public proxy，不等于 hidden 最终分 |
| [`artifacts.py`](../../llm4hls_harness/llm4hls_agent/artifacts.py) | 生成和验证 V0-V2 证据 Manifest | 防止报告引用丢失或被修改的文件 |
| [`v2_team_report.py`](../../llm4hls_harness/llm4hls_agent/v2_team_report.py) | 根据 V2 证据生成逐轮团队报告 | V2 复盘使用 |

V3 的报告生成目前仍在 `v3_prototype.py` 内部，没有单独拆文件。这是可读性债务，但在提交截止前不应为了“代码漂亮”重构主流程。

### 5.6 题目、测试、运行和发布目录

| 路径 | 内容 | 能否当真实成绩 |
|---|---|---|
| [`task_corpus/official/`](../../llm4hls_harness/task_corpus/official/) | 三道官方公开题目的本地快照 | 是真实公开题目，但运行结果仍取决于 backend |
| [`examples/`](../../llm4hls_harness/examples/) | 团队自建任务和测试 Patch | 只能证明局部能力 |
| [`tests/`](../../llm4hls_harness/tests/) | 自动单元测试和 deterministic smoke | 不能冒充真实 LLM/Vitis 成绩 |
| `llm4hls_harness/runs/` | 本机原始实验、工具日志、Planner 输入输出 | 可能是真实证据，但默认不进 Git |
| [`releases/`](../../llm4hls_harness/releases/) | 已冻结、适合团队共享的阶段报告 | 以每份 release 声明的证据等级为准 |
| [`doc/materials/`](../materials/) | 官方规则、HLS、环境和实验笔记 | 资料，不是运行证据 |
| [`docs/superpowers/`](../../docs/superpowers/) | 历史设计规范和实施计划 | 解释设计，不代表当前代码已经实现 |

### 5.7 想改某件事时先找哪个文件

| 你想做的事 | 先看这里 | 必须一起看的测试 |
|---|---|---|
| 修改四种任务的判定规则 | `v3_phase_router.py` | `test_v3_phase_router.py` |
| 改失败日志怎样压缩给模型 | `v3_failure_evidence.py` | `test_v3_failure_evidence.py` |
| 改 optimize 的 Synth 证据 | `v3_evidence.py`、`v3_openai_planner.py` | `test_v3_evidence.py`、`test_v3_openai_planner.py` |
| 改模型 System Prompt 或 JSON schema | `openai_provider.py` | `test_openai_provider.py` |
| 改 Graph 节点顺序或条件边 | `v3_prototype.py` | `test_v3_prototype.py`、`test_v3_task_aware_smoke.py` |
| 改 Patch 可修改范围 | `repair.py` | `test_repair.py` |
| 改 Vitis 调用或报告解析 | `vitis.py`、`tools.py` | `test_vitis.py`、`test_toolserver.py` |
| 新增公开题目 | `task_corpus/`、`task.py` | `test_task.py`、`test_task_corpus.py` |

不要为了调一个 Prompt 去改 `BudgetLedger`、Candidate 事务或 ToolServer；这些属于已经验收的公共底座。

## 6. 一次 V3 运行结束后怎样读报告

不要一开始翻几百个文件。按下面顺序即可：

| 顺序 | 文件 | 先看什么 |
|---:|---|---|
| 1 | `v3_team_report.md` | 终态、每轮 Candidate、工具调用、Token/Credit、final |
| 2 | `v3_prototype_result.json` | 程序认定的最终状态、mode、stop reason、best/final |
| 3 | `candidate_registry.json` | Candidate 树、每个版本来自哪个父版本 |
| 4 | `planner/inputs/`、`planner/outputs/` | 每轮模型实际看到了什么、返回了什么 |
| 5 | `actions/<action-id>/result.json` | 每次 CSim/Synth/CoSim 的结构化结果 |
| 6 | `evidence/` | 给 Planner 使用的压缩证据 |
| 7 | `budget_ledger.jsonl` | 每一步实际消耗的 Token/Credit |
| 8 | `trace.jsonl` | 完整节点时间线和路由理由 |
| 9 | `graph_checkpoints.sqlite` | 程序恢复用，一般不人工阅读 |
| 10 | `control/package_manifest.json` | 终态产物是否完整、是否被修改 |

证据等级必须严格区分：

```text
单元测试通过
  ≠ deterministic/demo 路径通过
  ≠ 真实模型参与
  ≠ 真实 Vitis 通过
  ≠ hidden grader 得分
```

## 7. V0 到 V3-C 到底完成了什么

| 阶段 | 通俗目标 | 已完成内容 | 当前状态 |
|---|---|---|---|
| V0 | 先把三种 HLS 工具可靠跑起来 | 公开任务加载、baseline 快照、预算、CSim/Synth/CoSim、缓存和审计 | 已完成，作为底座保留 |
| V1 | 让模型修一个错误 | 结构化诊断、受限 Patch、Candidate 隔离、修复终验和安全拒绝 | REAL/PASS 验收已完成 |
| V2 | 让模型反复优化并保留最好版本 | Candidate 树、PPA 比较、CoSim gate、多轮优化、final closure、团队报告 | 已冻结发布，不再扩展 |
| V3-A | 把流程变成可恢复状态机 | LangGraph 节点、checkpoint、scripted Planner/action 哈希、循环级 Synth Evidence | 已完成 |
| V3-B | 让真实 LLM 自主分析优化 | OpenAI-compatible Planner、live action journal、fast-experiment、多轮拒绝后继续、风险 CoSim gate | optimize 真实闭环已成功 |
| V3-C | 先判断题型，再进入相应流程 | PhaseRouter、三类失败 Evidence、四种 mode、共享 Candidate/Budget/Final | 代码和 smoke 完成；真实修复验收待补 |
| V4 | 变成可提交、可泛化的比赛系统 | hidden-like 测试、多模型矩阵、Docker/复现包、论文和视频 | 尚未完成 |

## 8. 目前最重要的真实结果

### 8.1 V3-B 官方 dotProduct：真实成功

运行目录（仅本机存在，不提交 Git）：

```text
llm4hls_harness/runs/v3b_fast_dotproduct_live_deepseek_retry2/
```

证据属性：

- 官方公开任务：`dotProduct_optimize`；
- Planner：真实 OpenAI-compatible `deepseek-v4-pro`；
- Backend：真实 `VitisBackend:v0.5` / Vitis 2025.2；
- Validation profile：`fast-experiment`；
- 最终 CSim：PASS；
- 最终 Synth：PASS；
- 最终 CoSim：PASS；
- Backend evidence level：`REAL_VITIS_VALIDATED`；
- 结果记录的 canonical Manifest digest：`21791847a8566620c5795c19321177521c6aeefb6311fab7949f3da8caeb01c8`；
- `control/package_manifest.json` 文件 SHA-256：`bdd5a01e603b435b2890b45aba0519567a8521b443c3623507ebb62970a7076e`。

关键数据：

| 指标 | Baseline | Final Candidate |
|---|---:|---:|
| Synth worst latency | 1027 cycles | 38 cycles |
| Top transaction interval | 1025 cycles | 39 cycles |
| Loop achieved II | 1 | 1 |
| 主要 loop TripCount | 1024 | 32 |
| DSP | 2 | 64 |
| FF | 93 | 3512 |
| LUT | 156 | 3711 |

结果：

```text
Acceleration = 1027 / 38 = 27.026×
```

这是原始 latency 比值。当前 reference/public proxy 的 acceleration contribution 按 `8×` 封顶，因此不能把 `27.026×` 直接当成官方计分加速。

资源增加是并行化带来的正常代价，当前占 U55C 可用资源比例仍较低。这个结果说明 Planner 正确识别了“loop II 已经是 1，但 1024 次串行归约仍使整次 transaction 很慢”，没有再次把 transaction interval 误认为 loop II。

Planner 第一轮选择：

```text
ARRAY_PARTITION + LOOP_UNROLL + MULTI_PARTIAL_SUM
```

本次共使用：

| 项目 | 数量 |
|---|---:|
| LLM calls | 3 |
| Input tokens | 5446 |
| Output tokens | 2682 |
| Total tokens | 8128 |
| CSim / Synth / CoSim | 3 / 3 / 1 |
| Credits | 35 / 40 |

后两轮 Planner 提出的 Patch 因超过改动行数限制而在 Candidate 创建前被拒绝，没有再次调用 Vitis。系统保留第一轮的 `candidate_001`，并完成全新的 Final 三阶段验证。

注意：这是公开任务上的真实本地结果，不等于比赛 hidden grader 最终成绩；reference harness 的评分公式也不能当作已公布的最终官方评分公式。

### 8.2 V3-C 三道官方题目：编排 smoke 通过

当前 smoke 使用官方题目源码、scripted Patch 和 deterministic backend，用于证明 Graph 路由与工具顺序，不代表真实模型或真实 Vitis 能力。

| 任务 | Baseline 现象 | 路由 | 最终状态 | CSim/Synth/CoSim | Credits |
|---|---|---|---|---:|---:|
| `projection_bugfix` | CSim FAIL | REPAIR | DONE | 3 / 2 / 1 | 31 |
| `dotProduct_optimize` | CSim、Synth PASS | OPTIMIZE | DONE | 3 / 3 / 1 | 35 |
| `residual_stream_deadlock` | CoSim deadlock | STRUCTURAL_FIX | DONE | 3 / 2 / 3 | 71 |

此外还有一个合成的资源超限 Graph 测试，证明“工具返回 Synth PASS，但资源超出上限”时仍会进入 `SYNTH_FIX`，不会误进入优化。

### 8.3 当前自动测试

截至 Agent 功能代码基线 `d56a2ed`，并包含本报告提交中的 `tests/__init__.py` 测试发现入口：

- 完整回归：286 tests PASS；
- Python 语法检查：PASS；
- `git diff --check`：PASS；
- 旧 V3-B checkpoint 缺少 `mode` 时仍默认按 OPTIMIZE 恢复。

组员统一使用下面的命令，不要各自拼测试模块列表：

```bash
cd llm4hls_harness
PYTHONPATH=. ../.venv/bin/python -m unittest discover -s tests -t . -v
```

`tests/__init__.py` 用于让 discovery 保持 `tests.*` 包名，从而使 V3 测试中的相对 import 正常工作。

## 9. 当前能力和没有完成的事情

### 9.1 已经能做

- 加载三道官方公开任务并保护 public/hidden 边界；
- 用 baseline 结果自动选择四种任务模式；
- 用同一个 LLM Planner 根据 mode 生成不同目标的 Patch；
- 限制模型只能修改 kernel；
- 在预算内多轮创建、验证、晋升或拒绝 Candidate；
- 从 Synth 提取 top latency、transaction interval、loop II、TripCount 和资源；
- 对低价值 Candidate 跳过探索 CoSim；
- 中断后通过 checkpoint 和 action journal 恢复；
- 对最终 Candidate 重新执行完整验证；
- 生成逐节点、逐 Candidate、逐预算的团队报告。

### 9.2 还不能宣布完成

- V3-C 的 REPAIR 尚无真实 LLM + Vitis 最终成功证据；
- V3-C 的 SYNTH_FIX 尚无真实 LLM + Vitis 最终成功证据；
- V3-C 的 STRUCTURAL_FIX 尚无真实 LLM + Vitis 最终成功证据；
- 当前没有独立的函数签名/顶层接口静态 diff gate；接口保持主要依赖 Planner 约束和后续 CSim/Synth/CoSim，不能把 Patch 路径检查描述成完整接口证明；
- 当前只完成 DeepSeek 的重点真实实验，尚未完成三种推荐模型的公平对比；
- 三道公开任务数量太少，尚未充分证明对 hidden-like 任务的泛化；
- Docker、干净环境复现、最终 `.zip`、论文实验表和 5 分钟视频尚未形成完整交付；
- 当前环境没有设置 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`LLM4HLS_MODEL`，不能立即复跑真实模型实验；
- 官方精确评分公式、Token 权重和最终统一 Credit 仍未公开确认。

### 9.3 一个必须提前处理的预算冲突

官方 `projection_bugfix` 样例预算是 20 Credits，但当前不可更改的 fresh final：

```text
CSim 1 + Synth 4 + CoSim 20 = 25 Credits
```

完整 REPAIR 最低需要：

```text
baseline CSim 1
+ Candidate CSim/Synth 5
+ Final closure 25
= 31 Credits
```

因此当前完整 smoke 使用了本地预算覆盖。真实比赛前必须确认：

- 最终评测是否允许本地 final CoSim 不计入 Agent 的任务 Credit；或
- 不要求 CoSim 的任务是否可以采用更轻的 final；或
- 正式任务预算是否会高于公开样例值。

在组织者澄清前，报告必须如实标注本地预算覆盖，不能称为“20 Credits 内完成”。

## 10. 团队月度成绩板

这里的“成绩”是内部工程进度，不是官方比赛得分。

状态定义：

- 绿色：已有真实证据，可以稳定演示；
- 黄色：代码和测试完成，但真实覆盖不足；
- 红色：提交前必须完成，目前还没有可交付证据。

### 2026 年 7 月当前状态

| 工作项 | 状态 | 当前证据 | 距离完成还差什么 |
|---|---|---|---|
| V0/V1/V2 工程底座 | 绿色 | V1 REAL/PASS；V2 冻结发布与真实验收 | 只维护，不再扩架构 |
| V3 LangGraph/Checkpoint | 绿色 | 动作节点、恢复、Manifest、完整回归 | 只修阻断性问题 |
| V3-B optimize | 绿色（单题） | 官方 dotProduct 真实 1027 → 38，Final 三项 PASS | 增加重复实验和模型对比 |
| V3-C PhaseRouter | 黄色 | 四模式路由测试和三题 smoke | 真实 repair/structural 路由验收后转绿 |
| V3-C REPAIR | 黄色 | projection deterministic smoke PASS | 真实 LLM + Vitis 成功运行 |
| V3-C SYNTH_FIX | 黄色 | Router/Planner/资源超限 Graph 测试 PASS | 真实综合失败任务成功运行 |
| V3-C STRUCTURAL_FIX | 黄色 | residual deterministic smoke PASS | 真实 CoSim deadlock 修复成功 |
| 模型覆盖 | 红色 | DeepSeek 真实结果 | 补 Qwen3.5、Qwen3.6 或写明不可用原因 |
| 泛化与稳定性 | 红色 | 3 道公开题，覆盖仍小 | hidden-like 题集、重复次数、失败率统计 |
| Docker/复现包 | 红色 | 本地环境可运行 | 干净环境构建和唯一启动命令 |
| 论文/视频 | 红色 | 已有技术数据和报告素材 | 实验表、两页正文、附录、5 分钟演示 |

当前总体判断：

> 工程主体进入“黄色偏绿”：核心系统已经形成，单个优化任务成绩很好，但比赛提交所需的任务覆盖、模型覆盖和复现材料仍不足。

## 11. 接下来的阶段计划

按团队 2026-07-10 保存的官网规则快照，比赛技术材料截止时间约为北京时间 2026-08-08 19:59；若官方后续更新 FAQ 或提交入口，应以最新通知为准。现在不适合继续增加新 Agent、RL、RAG 或复杂 Controller，应以证据闭环和提交为主。

### 7 月 20 日至 7 月 24 日：补齐 V3-C 真实闭环

目标：证明新 PhaseRouter 不是只在 fake backend 上有效。

必须交付：

1. `projection_bugfix`：真实模型进入 REPAIR，使用至少 31 Credits 的本地覆盖完成最终 CSim/Synth/CoSim，并明确标记 `LOCAL_BUDGET_OVERRIDE`，不能据此宣称满足官方样例的 20 Credits；
2. `residual_stream_deadlock`：真实模型进入 STRUCTURAL_FIX，最终三项 PASS；
3. 团队 synthesis-error fixture：真实进入 SYNTH_FIX，最终三项 PASS；
4. 每次实验保存模型 ID、Prompt 版本、Token、Credits、工具次数、wall time 和失败原因；
5. 失败运行保留，不只记录最好结果；
6. 对齐“Agent 搜索预算”与“外部 grader/final closure 是否计费”的官方口径，解决 20 < 25 的结构性冲突；
7. 将 V3-B 成功与失败各发布一份脱敏核心报告，避免真实证据只存在于被 Git 忽略的本地 `runs/`。

验收线：三种非 optimize mode 至少各有一次真实成功证据，并能从干净 run 目录复现。

### 7 月 25 日至 7 月 31 日：实验矩阵与稳定性

目标：从“跑通过一次”变成“可以比较和写论文”。

优先级：

1. 固定同一 task、预算、Prompt、Vitis 版本和超时；
2. DeepSeek 在三道官方题上各重复至少 3 次；
3. 接入可用的 Qwen3.5 与 Qwen3.6，不能接入时记录明确环境/资源原因；
4. 统计成功率、平均/最好 latency、Token、Credits、工具次数和 wall time；
5. 增加少量 hidden-like 变体，检查是否存在针对公开样例的硬编码。

验收线：形成一张可直接放入论文的模型 × 任务实验表，并且每个数字都能追到 run ID。

### 8 月 1 日至提交截止：冻结与交付

目标：按时交出可复现材料，不再追求大规模架构变化。

必须完成：

1. 冻结比赛分支、依赖和 Prompt 版本；
2. 完成 Docker/复现方式、环境变量模板和唯一启动命令；
3. 从干净环境复跑最小演示；
4. 完成两页论文正文和 appendix；
5. 完成实验报告、失败分析、Token/Credit 表；
6. 录制不超过 5 分钟的真实运行视频；
7. 检查 `.zip` 中没有 API key、license 信息、私有路径和大型无关 run。

建议 8 月 3 日后停止加入非阻断性功能，只允许修复会影响复现、正确性或提交的缺陷。

### 提交后至 8 月 21 日：等待 shortlist 期间的 Final stage 预备

如果提交完成，可提前准备；只有正式入围后才算进入 Final stage：

- 10 分钟讲解和 live demo；
- 5 分钟问答题库；
- 断网、模型超时、Vitis 超时和 CoSim 失败时的演示回退；
- 用一张图解释 PhaseRouter、Budget、Candidate 和 CoSim gate；
- 准备“为什么不是多 Agent”“为什么不使用 RL”“怎样防 hidden hardcode”等问题。

## 12. 建议的组内分工

没有必要让每个人都同时改 `v3_prototype.py`。建议按交付物分工：

| 角色 | 本阶段主要责任 | 每周必须产出 |
|---|---|---|
| Agent/Prompt | Planner schema、Prompt、Patch 成功率 | Prompt 版本、失败分类、Token 表 |
| HLS/Vitis | Synth/CoSim、latency/II/资源、XSIM 问题 | 可复现 run、指标表、工具失败结论 |
| 实验与数据 | 批量运行、run ID、模型公平对比 | 实验矩阵、均值/成功率、异常记录 |
| 复现与提交 | 安装、Docker、入口、打包和秘密检查 | 干净环境结果、提交清单 |
| 论文与演示 | 两页论文、appendix、视频和答辩 | 每周可审阅版本和素材缺口 |

每项工作必须绑定证据文件或 run ID，不能只写“已完成”。

## 13. 月报以后怎样更新

建议每月复制下面这张表，并保留上月数据，不要覆盖历史：

```text
月份：
代码基线 commit：
总体状态：绿色 / 黄色 / 红色

本月目标：
本月实际完成：
真实 LLM 运行数：
真实 Vitis 完整闭环数：
公开任务通过数 / 总数：
模型覆盖数 / 目标数：
最佳 latency 与对应 run ID：
总 Tokens / Credits / 工具次数：
新增回归测试：
主要失败和根因：
未解决阻塞项：
下月唯一最高优先级：
负责人和证据链接：
```

月报中必须分别统计：

- 单元测试；
- deterministic smoke；
- 真实 LLM；
- 真实 Vitis；
- final 三项验证；
- hidden/官方最终评分。

这样可以避免“测试都过了”被误解成“比赛已经能得高分”。

## 14. 当前最值得做的一件事

现在最值得做的不是拆文件或增加新架构，而是：

> 用真实模型和真实 Vitis，完整跑通 `projection_bugfix` 的 REPAIR 路径，并生成第一份 V3-C 真实团队报告。

原因是它成本和复杂度都低于 structural deadlock，却能一次验证 PhaseRouter、失败 Evidence、task-aware Prompt、Patch、Candidate、Budget 和 Final closure 是否真的连通。完成后再处理 `residual_stream_deadlock`。

## 15. 文档与命名债务

当前部分名称仍停留在旧阶段：

- `v3_prototype.py` 模块注释仍称 V3-A1；
- CLI 描述中仍有 V3-B0；
- 结果中的 workflow 标识仍有 `V3A0_LANGGRAPH_PROTOTYPE`。

这些名称不会改变运行逻辑，但会让新人误以为 V3-B/V3-C 尚未实现。本报告暂时作为当前事实入口。提交截止前只建议修正文案和入口说明，不建议为改名破坏 checkpoint、run identity 或旧报告兼容性。

## 16. 相关入口

- [比赛总览](track-a-competition-overview.md)
- [零基础 HLS/Agent 入门](track-a-zero-foundation-guide.md)
- [官方规则快照](../materials/01_official/2026-07-10-track-a-official-summary.md)
- [V2 核心报告阅读指南](../materials/07_experiments/2026-07-18-v2-core-report-guide.md)
- [V2 冻结发布记录](../../llm4hls_harness/releases/v2-ppa-gated-final_CN.md)
- [V3-C task-aware smoke 测试](../../llm4hls_harness/tests/test_v3_task_aware_smoke.py)
