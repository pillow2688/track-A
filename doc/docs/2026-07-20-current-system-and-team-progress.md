# Track A 当前系统说明与团队月度进展

- 首次发布：2026-07-20
- 更新时间：2026-07-21
- 当前代码分支：`feat/v3d-overnight-execution`
- 当前代码基线：`a796da3`（V3-D corpus/oracle/batch/interface guard/Docker + XSIM A03 恢复报告）
- 最近完整回归（源码基线 `a796da3`）：宿主 `Ran 382 tests; OK`；容器 `Ran 382 tests; OK (skipped=3)`
- 报告版本：查看 `git log -1 -- doc/docs/2026-07-20-current-system-and-team-progress.md`
- 发布状态：当前功能分支已推送；尚未合并到生产分支
- 面向读者：第一次接触项目的组员、需要复盘实验的开发者、论文与演示负责人
- 文档定位：当前事实入口。历史设计文档用于解释“为什么这样设计”，本报告说明“代码现在实际能做什么”

## 0. 新成员先按这个顺序看

如果你只有 10 分钟，不需要先读历史聊天或几百行设计规范：

1. 看第 1 节，知道项目现在做到哪里；
2. 看第 2.1、2.2 节，把概念职责映射到真实代码并认清常用术语；
3. 看第 3.0～3.2 节，理解真实系统边界、纵向流程和横向组件；
4. 先看第 8.0 节掌握当前证据，再按需看第 8.1、8.3 节的历史成功与失败；
5. 看第 10、14 节，知道当前成绩和唯一最高优先级；
6. 只有准备改代码时，再查第 4、5 节的入口和文件地图。

加入开发前必须记住三条：

- 我们优化的是 **HLS kernel**，不是修改 testbench 来让测试通过；
- LLM 只负责提出假设和 Patch，预算、工具、晋升和 final 都由程序控制；
- `tests PASS` 只说明程序逻辑没回归，只有真实 LLM + 真实 Vitis + fresh final 才是比赛实验结果。

## 1. 先看结论

我们正在做一个自动修改 HLS C++ 的程序。它收到一道题后，先让真实工具产生 baseline 事实；PhaseRouter 只负责选择任务模式，Evidence Extractor 压缩失败或性能信息，再由大模型诊断具体问题并提出 Patch，最后调用 Vitis 验证修改是否正确、能否综合、是否更快。

截至 2026-07-21，最准确的项目状态是：

1. V0、V1、V2 已经完成各自的工程闭环：V0 保留真实 Vitis baseline 证据，V1/V2 保留真实 LLM + Vitis 验收证据。
2. V3-A 已把工具节点和 scripted Planner 流程迁移到可 checkpoint 的 LangGraph；V3-B 进一步加入真实、不可重放 LLM 请求的 STARTED/COMPLETED action 记录与恢复边界。
3. V3-B 已接入真实 OpenAI-compatible Planner，并在官方 `dotProduct_optimize` 上得到一次真实成功结果：Synth worst latency 从 `1027` 降到 `38 cycles`，最终 CSim、Synth、CoSim 全部通过。
4. V3-C 的任务分诊已经真实闭环：`projection_bugfix` 进入 REPAIR、`residual_stream_deadlock` 进入 STRUCTURAL_FIX、动态分配 fixture 进入 SYNTH_FIX，三者均由真实 `deepseek-v4-pro` 生成 Patch，并通过真实 Vitis fresh final CSim/Synth/CoSim。
5. V3-D 已新增 28 题 fast corpus、fail-closed Corpus Oracle、批量 Benchmark、TopInterfaceGuard、Docker/clean-room、提交报告和秘密扫描。Deterministic Oracle 为 28 accepted / 0 rejected；可移植真实 Vitis receipts 覆盖四种 mode 各 3 道。
6. XSIM A01/A02 的 4 个失败任务已在全新 A03 环境串行恢复为 4 accepted / 0 rejected；`015/016/017` 复现预期 deadlock 后 golden CoSim PASS，`028` 从 39 降到 6 cycles。
7. DeepSeek 已完成官方三题各 3 次和额外 SYNTH_FIX 3 次：12/12 DONE、12/12 fresh final 全 PASS，共 31258 Tokens、526 Credits；dotProduct 三次分别得到 38、518 和安全回退 1027 cycles。
8. 源码/镜像基线 `a796da3` 的 Docker 镜像已重建并通过 demo-smoke 与容器快速测试；当前唯一不能立即执行的模型矩阵是 Qwen，因为没有可达的 Qwen endpoint/key/model alias。

一句话概括：

> 当前系统已经从“单题原型”进入“可重复实验和提交冻结前验证”阶段；下一步重点是 Qwen 公平矩阵、消融、最终提交格式和论文/视频，而不是继续扩建 Agent 架构。

## 2. 用最通俗的话理解整个系统

可以把 Agent 看成一家小型医院：

| 项目组件 | 通俗比喻 | 实际职责 |
|---|---|---|
| PhaseRouter | 分诊台 | 判断病人应该去功能修复、综合修复、结构修复还是优化科 |
| LLM Planner | 提方案的工程师 | 根据代码和有限证据提出一个修改假设与 Patch |
| Patch Validator | 安检员 | 禁止修改 header/testbench；允许把行号不准但 old-hunk 唯一逐字匹配的 Patch 重定位到正确位置 |
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

### 2.1 设计图中的名字，代码里不一定是同名类

历史讨论中经常出现 `Supervisor`、`Phase Controller`、`Risk Analyzer`、`Tool Router`。这些是职责名称，不代表仓库中已经各自存在一个独立 Agent 或 class。当前真实映射如下：

| 概念职责 | 当前真实实现 | 成熟度 |
|---|---|---|
| Supervisor / 总控 | `build_v3_prototype_graph()`、条件边和若干纯路由函数 | 已运行，不是 LLM，也不是独立 class |
| Phase Controller | `phase_router` Graph 节点 + `v3_phase_router.py` | 已实现四模式确定性路由 |
| Optimization Planner | 一个 OpenAI-compatible Planner，根据 `mode` 改变目标 | 已实现；不是多个 Agent 对话 |
| Risk Analyzer | fast-experiment OPTIMIZE 用 `_fast_experiment_risk()` 读取 task/strategy/Patch/Planner risk，再结合 score/budget gate；strict 按 score/PPA gate | 已实现为规则/节点组合，不是独立学习模型 |
| Budget Controller | `BudgetLedger` + 各预算 gate 节点 | 已实现为硬规则，不由 LLM 决定 |
| Tool Router | Graph 条件边 + `ToolServer` | 已实现；ToolServer 负责执行/计费，条件边决定下一工具 |
| Memory | Candidate registry、失败摘要、最近策略、Artifact 引用 | 已实现 run 内记忆；fast history 显式截断，strict 主要由轮数限制；没有长期 RAG Memory |
| Reporter | `v3_prototype.py` 内的 `write_report` | 已实现，但尚未独立成模块 |

明确不存在的内容：

- 没有 RL、Bandit、beam search；
- 没有多个 LLM Agent 相互讨论；
- 没有让 LLM 直接执行 shell/Vitis；
- hidden testbench/reference kernel 不进入模型上下文；API key 只用于发往配置 provider 的 Authorization header，不进入 Prompt 或持久化 request audit；
- 没有一个万能“智能 Router”自动替代所有确定性规则。

### 2.2 七个必须先分清的术语

| 术语 | 在本项目中的含义 |
|---|---|
| baseline | 题目最初给出的 kernel 及其真实验证结果，是修复或优化的起点 |
| Candidate | 从某个父版本应用一份通过安全校验的 Patch 后，物化出的不可变代码版本 |
| incumbent / best | 当前探索中已被程序接受、下一轮优先从它继续修改的最好 Candidate |
| mode | PhaseRouter 选择的当前任务目标：`REPAIR`、`SYNTH_FIX`、`STRUCTURAL_FIX` 或 `OPTIMIZE` |
| validation profile | 验证策略档位；当前有保守的 `strict` 和节省探索 CoSim 的 `fast-experiment` |
| exploration | final 之前的候选搜索阶段，允许快速拒绝无效 Candidate，并按风险决定验证深度 |
| fresh final | 对最终选中代码重新发起、不复用探索结论的 CSim、Synth、CoSim 出厂验证 |

## 3. 一道题实际怎样流动

### 3.0 当前真实系统边界

下面不是未来设想，而是当前代码的真实依赖关系：

```text
公开 Task Package
  task.toml + description.md + kernel.cpp + headers + public TB
                         │
                         ▼
                 v3_prototype_cli.py
                         │
                         ▼
              LangGraph（v3_prototype.py）
                │                    │
                │                    ├── OpenAI-compatible Planner API
                │                    │      只收受限上下文，返回 JSON + Patch
                │                    │
                │                    └── ToolServer
                │                          ├── --backend vitis → VitisBackend → Vitis 2025.2
                │                          └── 默认/demo → DeterministicPrototypeBackend
                │                                  只证明编排，不能当真实工具结果
                ▼
运行目录 runs/<run-id>/
  Candidate 源码、Planner 输入输出、工具结果、Evidence、Budget、Trace、Checkpoint、Final Report
```

系统边界之外：

```text
hidden testbench / hidden grader / reference solution
```

它们不进入 Planner Prompt，也不参与 Agent 内部搜索。正式评分由外部 grader 完成。

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
TopInterfaceGuard 检查顶层硬件 ABI
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

在代码中，这条主流程不是一个巨大的 `while`，而是 26 个动作级节点。新人不需要逐个背，先按六组理解：

| 节点组 | 实际节点 | 作用 |
|---|---|---|
| 初始化 | `initialize` | 固定任务、baseline、预算、run identity 和 checkpoint 边界 |
| Baseline | `baseline_csim`、`baseline_synth`、`baseline_cosim` | 取得分诊与性能基线；失败时可提前进入 PhaseRouter |
| 分诊与预算 | `phase_router`、`evaluate_task_round_budget`、`evaluate_round_budget` | 选择 mode，并确认本轮与 final reserve 仍可支付 |
| Planner 与 Candidate | `plan_candidate`、`materialize_candidate`、`record_rejected_proposal` | 调模型、规范化 Patch、创建不可变 Candidate，或记录未落地提案 |
| Candidate 验证与决策 | `candidate_csim`、`candidate_synth`、`candidate_score_gate`、`candidate_cosim_budget_gate`、`candidate_cosim`、`promote_*`、`reject_candidate`、`advance_round` | 真实验证、收益/风险门控、晋升/拒绝和多轮继续 |
| Final 与报告 | `select_final_attempt`、`evaluate_final_budget`、`final_csim`、`final_synth`、`final_cosim`、`evaluate_final_fallback`、`write_report` | 对 best 做 fresh closure；次数、预算和 eligible Candidate 允许时才 fallback，最后封存报告 |

这些节点有两个重要性质：

1. 一个 LLM 或 Vitis 副作用只属于一个节点，便于 checkpoint 后恢复；
2. Graph State 主要保存小字段和 Artifact 引用，不把完整代码、日志和 Prompt 塞进 SQLite。

Final fallback 也有边界：OPTIMIZE 只有在 `max_final_attempts`、预算和 eligible Candidate 同时允许时才 fallback；候选必须满足当前 profile 的探索验证要求——fast 普通任务至少通过 CSim+Synth，strict 或 required-cosim 路径还要求 CoSim。fallback 选中历史 Candidate 后仍必须重新跑完整的 fresh CSim/Synth/CoSim。REPAIR、SYNTH_FIX、STRUCTURAL_FIX 不能回退到原本就有错误的 baseline，修复 Candidate final 失败会明确结束。

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
| `OPTIMIZE` | 在正确前提下降低 latency/PPA proxy | CSim → Synth → 风险/收益 CoSim gate |

无论探索阶段走哪个模式，最终提交对象都必须重新运行完整的 CSim、Synth、CoSim。

当前保留两种 validation profile：

| Profile | Baseline | Optimize Candidate | 适用场景 |
|---|---|---|---|
| `strict` | CSim PASS 后跑 Synth；Synth PASS 后跑 baseline CoSim；任一失败都会提前进入 PhaseRouter | 使用完整评分/CoSim gate，策略更保守 | 回归、保守验证和兼容旧实验 |
| `fast-experiment` | 普通任务默认 CSim + Synth；`requires_cosim=true` 才跑 baseline CoSim | 先 CSim + Synth；无严格 latency 提升直接拒绝；低风险提升可把 CoSim 推迟到 final，高风险提升才探索 CoSim | 有限 Credit 下的真实多轮搜索 |

`fast-experiment` 省略的只是部分**探索 CoSim**，不是最终正确性。Final 仍固定为 fresh CSim → Synth → CoSim。

另外，当前三种修复模式通过对应 correctness gate 后会直接进入 final；系统还没有实现“修复成功后自动切换到 OPTIMIZE 再继续提速”。

### 3.2 横向公共组件

“横向”指它们不只属于某一步，而是从开始到结束一直保护主流程。

| 横向组件 | 输入/触发 | 它怎样影响纵向流程 | 它无权做什么 |
|---|---|---|---|
| Task Loader | task 目录 | 只加载公开 kernel、headers、description 和 public TB，固定任务指纹 | 不能读取 hidden/reference 给 Planner |
| Budget gate + Ledger | 每轮规划及每次 LLM/工具动作前后 | Graph gate 先判断本轮和 final reserve 是否整体付得起；Ledger 再以 STARTED、COMPLETED/AMBIGUOUS 记录并硬限制每个真实动作的 Token/Credit/次数/runtime | 不能生成 Patch 或提高预算上限 |
| Candidate Manager | 通过校验的 Patch + parent | 原子保存源码、Patch、hash 和父子关系 | 不决定 promote、reject 或 final |
| Patch Validator | Planner unified diff + parent source | 校验路径/大小并做唯一精确上下文重定位 | 不接受歧义修改，也不判断性能 |
| TopInterfaceGuard | Patch 后源码 + baseline 顶层签名 | 在 Candidate 进入 Vitis 前阻止顶层函数名、参数、类型和固定接口被改写 | 不判断算法正确性或优化收益 |
| ToolServer | Graph 指定的 stage + Candidate | 统一调用、计费、缓存、保存 action 结果 | 不自己选择下一工具或 final |
| Evidence Extractor | ToolResult 和有限日志 | 压缩成 CSim/Synth/CoSim failure 或 Synth 性能事实 | 不虚构缺失的 II、latency 或根因 |
| Scoring/Comparator | Candidate Synth/验证结果 | 判断 optimize Candidate 是否严格优于 incumbent | 不覆盖 correctness gate |
| CoSim gate | 收益、risk、task、final reserve | 决定探索 CoSim、推迟到 final 或拒绝 | 不允许跳过最终 CoSim |
| Checkpoint | 每个 Graph step 的小 State | 中断后恢复下一节点，配合 action journal 避免重复副作用 | 不保存完整代码/日志作为真相 |
| Report/Manifest | 终态 registry、ledger、trace、artifacts | 串起模型、工具、预算、Candidate 和 final 证据 | 不能把失败美化成 PASS |

可以简单理解为：

```text
纵向流程 = 车从起点开到终点

横向组件 = 导航、油表、收费站、仓库、安检和行车记录仪
```

### 3.3 三条真实数据流

新成员调试时要区分代码、证据和控制状态，它们不是同一份数据。

#### 代码流

```text
公开 kernel bytes
  → candidate_000（不可变 baseline）
  → Planner Patch
  → Patch 唯一上下文匹配/安全校验
  → TopInterfaceGuard 固定顶层硬件 ABI
  → candidates/candidate_NNN/source/<kernel>.cpp
  → CSim/Synth/CoSim
```

Patch 行号只是定位元数据。当前策略允许自动纠正错误行号，但必须满足：目标仍是 kernel、old-hunk 在源码中只有一个逐字匹配、修改大小合法、最终 diff 可完整应用。零匹配或多匹配仍会拒绝。

#### 证据流

```text
Vitis 原始日志/报告
  → actions/<action-id>/result.json
  → Failure Evidence 或 Synth Evidence
  → 有界 Planner Input
  → Planner hypothesis + Patch
  → Candidate 结果与下一轮最近失败摘要
```

模型不接收完整原始日志或完整对话历史。fast optimize 显式限制为最多 8 个 attempts 和 3 个 failures；task-aware 修复主要发送当前 Failure Evidence，不追加增长式 Candidate 历史；strict optimize 的历史规模目前主要由配置轮数间接限制，尚没有与 fast 相同的显式切片。

#### 控制与审计流

```text
LangGraph State（当前 mode、best、active、round、Artifact refs）
  ↔ graph_checkpoints.sqlite

Budget reserve() 写入 STARTED → COMPLETED/AMBIGUOUS → reconcile
  → budget_ledger.jsonl

每个节点结果
  → trace.jsonl + candidate_registry.json + v3_prototype_result.json
```

所以某一步失败时，应先问：“代码没生成、工具失败，还是只是 Graph/报告状态错误？”不要只看终端最后一行。

### 3.4 提交前的目标整体架构

目标不是再堆新的 Agent，而是把当前同一套架构补齐真实覆盖和可复现性：

```text
Reference-compatible Task Input
             ↓
一张 task-aware、budget-aware、可恢复的 LangGraph
             ├── 硬控制：Python Phase / Budget / Stop / Final 路由 + 启发式/Planner risk gate
             ├── 一个 LLM Planner：按 mode 诊断、规划、生成 Patch
             ├── 受限执行面：Patch → Candidate → ToolServer → Vitis
             └── 证据面：Evidence / Ledger / Trace / Checkpoint / Report
             ↓
经过 fresh CSim + Synth + CoSim 的最终 kernel
             ↓
Docker/唯一命令/实验矩阵/论文与视频
```

| 层次 | 当前真实状态 | 提交前目标 | 主要缺口 |
|---|---|---|---|
| 任务输入 | 三道官方公开题 + 28 题 V3-D fast corpus | 增加经过真实 Oracle 的高区分度任务 | hidden-like 覆盖仍有限 |
| 控制面 | 四 mode、预算、checkpoint、final/fallback | 保持现架构，冻结非必要变化 | strict/fast 和两项消融 |
| 推理面 | DeepSeek mode-aware Planner 已完成 12-run 重复矩阵 | 在完全相同配置下补 Qwen3.5/Qwen3.6 | 缺 Qwen serving 配置 |
| 执行面 | Candidate、Patch、TopInterfaceGuard、ToolServer、Vitis 2025.2 已工作 | 冻结 Docker/外部 Vitis 部署口径 | 最终官方容器要求待确认 |
| 评价面 | latency、II、clock、resource 和 public score proxy | 正确性优先，报告可追溯；不冒充 hidden scorer | Power 没有可靠实测，官方最终评分仍可能调整 |
| 交付面 | Docker、依赖锁、preflight、release、submission 草稿和 QA 已有 | 最终 staging、论文、视频和官方格式复核 | 尚未形成最终提交包 |

因此目标架构的“升级”主要发生在证据覆盖、任务覆盖和交付层，不是继续新增 Controller、Multi-Agent、RL 或 RAG。

## 4. 当前代码从哪里开始看

项目现在有两套入口并存，这是新人最容易混乱的地方。

| 要运行的阶段 | 命令入口 | 主要代码 |
|---|---|---|
| V0、V1、V2 | `python -m llm4hls_agent ...` | `llm4hls_agent/cli.py` |
| V3-B、V3-C、V3-D 单题 | `llm4hls-v3-prototype` | `v3_prototype_cli.py` + `v3_prototype.py` |
| V3-D corpus/oracle/batch | `llm4hls-v3d-oracle`、`llm4hls-benchmark` | `v3d_oracle_validator.py` + `v3_batch_benchmark.py` |

V3-C 不是第三套新程序。它是在 V3-B 的同一张 LangGraph 中加入 PhaseRouter 和修复分支。V3-D 也没有再造一张 Agent Graph，而是在外层增加 corpus、Oracle、batch、接口保护和交付工具。

当前若只想理解最新系统，建议阅读顺序是：

1. 本报告；
2. 一个官方任务的 `task.toml` 和 kernel；
3. `v3_prototype_cli.py`；
4. `v3_prototype.py` 中的 `build_v3_prototype_graph()`；
5. `v3_phase_router.py`；
6. `test_v3_task_aware_smoke.py`；
7. `budget.py`、`candidate.py`、`tools.py`；
8. `v3_openai_planner.py` 和 `openai_provider.py`；
9. 需要跑数据集时再看 `v3d_oracle_validator.py`、`v3_batch_benchmark.py` 和 `top_interface_guard.py`。

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
| [`v3_batch_benchmark.py`](../../llm4hls_harness/llm4hls_agent/v3_batch_benchmark.py) | 串行执行模型 × 任务 × repeat，失败后继续并区分真实/fixture 证据 | 做重复实验和汇总时 |
| [`v3d_oracle_validator.py`](../../llm4hls_harness/llm4hls_agent/v3d_oracle_validator.py) | 按 mode 验证 baseline/golden 应命中的门，只让合格题进入 accepted corpus | 扩充或复验数据集时 |

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
| [`repair.py`](../../llm4hls_harness/llm4hls_agent/repair.py) | 检查、规范化和应用 unified diff；唯一逐字 old-hunk 可修正错误行号 | Patch 越权、零/多处匹配、改动过大 |
| [`candidate.py`](../../llm4hls_harness/llm4hls_agent/candidate.py) | 原子创建不可变 Candidate 并保存 lineage；Graph 把验证记录和 best/active/final 决策写入同一 registry | Candidate ID、best、回退不一致 |
| [`budget.py`](../../llm4hls_harness/llm4hls_agent/budget.py) | 追加式记录 Credit、Token、次数和时间 | 预算不足、重复计费、账本不一致 |
| [`tools.py`](../../llm4hls_harness/llm4hls_agent/tools.py) | CSim/Synth/CoSim 唯一受预算控制的调用入口 | 缓存、action 绑定、工具结果审计失败 |
| [`vitis.py`](../../llm4hls_harness/llm4hls_agent/vitis.py) | 真正启动 Vitis 2025.2 并解析报告 | Vitis/XSIM、license、超时、报告解析问题 |
| [`validation.py`](../../llm4hls_harness/llm4hls_agent/validation.py) | V1/V2 共用的整段验证助手 | 旧流程 Candidate 验证问题 |
| [`top_interface_guard.py`](../../llm4hls_harness/llm4hls_agent/top_interface_guard.py) | 独立检查 top 函数名、参数、返回类型、public symbol 和 interface pragma 风险 | 防止模型改变硬件 ABI 时 |

### 5.5 优化、评分和报告

| 文件 | 一句话职责 | 当前地位 |
|---|---|---|
| [`optimization.py`](../../llm4hls_harness/llm4hls_agent/optimization.py) | V2 优化主循环和旧 Selector；V3 只复用 `evaluate_exploration_cosim_gate` 的探索 CoSim 门控 | V3 Planner 和循环由当前 Graph 实现，不复用 V2 Selector |
| [`scoring.py`](../../llm4hls_harness/llm4hls_agent/scoring.py) | 检查验证等级、时钟、资源和 PPA proxy，比较 Candidate | public proxy，不等于 hidden 最终分 |
| [`artifacts.py`](../../llm4hls_harness/llm4hls_agent/artifacts.py) | 生成和验证 V0-V2 证据 Manifest | 防止报告引用丢失或被修改的文件 |
| [`v2_team_report.py`](../../llm4hls_harness/llm4hls_agent/v2_team_report.py) | 根据 V2 证据生成逐轮团队报告 | V2 复盘使用 |

V3 的报告生成目前仍在 `v3_prototype.py` 内部，没有单独拆文件。这是可读性债务，但在提交截止前不应为了“代码漂亮”重构主流程。

### 5.6 题目、测试、运行和发布目录

| 路径 | 内容 | 能否当真实成绩 |
|---|---|---|
| [`task_corpus/official/`](../../llm4hls_harness/task_corpus/official/) | 三道官方公开题目的本地快照 | 是真实公开题目，但运行结果仍取决于 backend |
| [`task_corpus/v3d-fast/`](../../llm4hls_harness/task_corpus/v3d-fast/) | 28 道不透明 mutation 任务和 acceptance/golden/hidden-like 隔离 | 只有真实 Vitis/LLM run 才能当真实结果 |
| [`examples/`](../../llm4hls_harness/examples/) | 团队自建任务和测试 Patch | 只能证明局部能力 |
| [`tests/`](../../llm4hls_harness/tests/) | 自动单元测试和 deterministic smoke | 不能冒充真实 LLM/Vitis 成绩 |
| `llm4hls_harness/runs/` | 本机原始实验、工具日志、Planner 输入输出 | 可能是真实证据，但默认不进 Git |
| [`releases/`](../../llm4hls_harness/releases/) | 已冻结、适合团队共享的阶段报告 | 以每份 release 声明的证据等级为准 |
| [`doc/materials/`](../materials/) | 官方规则、HLS、环境和实验笔记 | 资料，不是运行证据 |
| [`docs/superpowers/`](../../docs/superpowers/) | 历史设计规范和实施计划 | 解释设计，不代表当前代码已经实现 |
| [`docs/submission/`](../../docs/submission/) | 实验表、失败分析、复现、Demo 和 checklist | 提交素材草稿，不等于官方最终格式 |

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

### 5.8 核心数据对象

| 对象/文件 | 谁创建 | 保存什么 | 下游怎样使用 |
|---|---|---|---|
| `PublicTask` | `task.py` | 公开 kernel、headers、description、public TB、约束 | 初始化 baseline 和所有 Planner/工具边界 |
| `V3PrototypeState` | LangGraph | 当前 mode、round、best/active/final Candidate ID、最近结果和 Artifact refs | 条件边决定下一节点；SQLite checkpoint 保存 |
| `PatchProposal` | scripted/live Planner | hypothesis、change class/bundle、risk、`required_validation` 归一化提示、unified diff、Token | Patch policy 校验并物化 Candidate；最终工具路径仍由 Graph、task mode、risk gate 和 Budget 决定 |
| `candidate_registry.json` | Candidate Manager + Graph 决策节点 | Candidate lineage、code/patch hash、验证、best/active、状态 | 恢复、晋升、拒绝、final/fallback |
| `ToolResult` / `actions/*/result.json` | ToolServer | CSim/Synth/CoSim 终态、报告、配置/code hash、耗时 | 更新验证状态并生成 Evidence/score |
| Failure/Synth Evidence | Evidence Extractor | 有界失败摘要或 latency/II/TripCount/resource 事实 | 进入下一轮 Planner Input |
| `budget_ledger.jsonl` | Budget Ledger | STARTED、COMPLETED/AMBIGUOUS、Token/Credit/runtime | 所有收费动作前后核账和恢复 |
| `v3_prototype_result.json` | `write_report` | 程序认定的终态、mode、best/final、预算、最终验证 | 自动汇总和验收；比 Markdown 报告更权威 |

权限边界：Planner 生成 `PatchProposal`，其中 `required_validation` 只是 Provider/V3 adapter 归一化后的验证提示，不是模型对工具的批准。Candidate Manager 保存代码版本，Graph 节点根据 task mode、risk gate 和预算决定验证路径与 promote/reject，并规划本轮与 final reserve；ToolServer 执行工具，Budget Ledger 对每个真实动作执行最终硬约束和计费。它们不能互相越权。

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

真实实验的目录规则：

- 每次新的模型实验必须使用全新 run 目录；失败后把 attempt 从 `A01` 改为 `A02/A03/...`，不要覆盖或续写旧目录；
- 想恢复**同一次因进程中断的事务**才使用原 run/thread/checkpoint；一次已经正常结束为 FAILED 的实验不是“中断恢复”；
- 目录名应包含 task、mode、attempt，使用本地超额预算时显式加入 `LOCAL_BUDGET_OVERRIDE`；
- 环境变量只在执行命令的终端设置，报告保存模型名和配置摘要，不保存 API key。

证据等级必须严格区分：

```text
单元测试通过
  ≠ deterministic/demo 路径通过
  ≠ 真实模型参与
  ≠ 真实 Vitis 通过
  ≠ hidden grader 得分
```

## 7. V0 到 V3-D 到底完成了什么

| 阶段 | 通俗目标 | 已完成内容 | 当前状态 |
|---|---|---|---|
| V0 | 先把三种 HLS 工具可靠跑起来 | 公开任务加载、baseline 快照、预算、CSim/Synth/CoSim、缓存和审计 | 已完成，作为底座保留 |
| V1 | 让模型修一个错误 | 结构化诊断、受限 Patch、Candidate 隔离、修复终验和安全拒绝 | REAL/PASS 验收已完成 |
| V2 | 让模型反复优化并保留最好版本 | Candidate 树、PPA 比较、CoSim gate、多轮优化、final closure、团队报告 | 已冻结发布，不再扩展 |
| V3-A | 把流程变成可恢复状态机 | LangGraph 节点、checkpoint、scripted Planner/action 哈希、循环级 Synth Evidence | 已完成 |
| V3-B | 让真实 LLM 自主分析优化 | OpenAI-compatible Planner、live action journal、fast-experiment、多轮拒绝后继续、风险 CoSim gate | optimize 真实闭环已成功 |
| V3-C | 先判断题型，再进入相应流程 | PhaseRouter、三类失败 Evidence、四种 mode、共享 Candidate/Budget/Final | 三种修复 mode 均有真实 LLM + Vitis fresh final 成功 |
| V3-D | 从单题 Agent 变成可评测、可复现的候选系统 | 28 题 corpus、Oracle、batch、TopInterfaceGuard、Docker、submission QA 工具与草稿、真实重复矩阵 | 主体完成；Qwen、消融和最终提交冻结待完成 |
| V4/提交冻结 | 形成官方可复现交付 | 最终容器/外部 Vitis 口径、论文、视频、hidden-like 扩展 | 尚未完成，不再通过新增 Agent 架构推进 |

## 8. 目前最重要的真实结果

### 8.0 2026-07-21 当前证据总览

完整时间线、逐 run 数据和 SHA-256 绑定见：

- [`v3d-overnight-daytime-summary-2026-07-21.md`](../../llm4hls_harness/releases/v3d-overnight-daytime-summary-2026-07-21.md)
- [`v3d-overnight-daytime-summary-2026-07-21.json`](../../llm4hls_harness/releases/v3d-overnight-daytime-summary-2026-07-21.json)

四种 mode 现在均有真实模型 + 真实 Vitis 原子闭环：

| 任务 | Mode | Tokens | Credits | LLM/CSim/Synth/CoSim | Fresh final |
|---|---|---:|---:|---:|---|
| projection A04 | REPAIR | 2043 | 31 | 1/3/2/1 | 全 PASS |
| residual A01 | STRUCTURAL_FIX | 2216 | 71 | 1/3/2/3 | 全 PASS |
| synth-fix A01 | SYNTH_FIX | 1675 | 35 | 1/3/3/1 | 全 PASS |
| dotProduct 独立成功 | OPTIMIZE | 8128 | 35 | 3/3/3/1 | 1027 → 38，Final 全 PASS |

DeepSeek 重复矩阵不是只保留最好值：官方三题 9/9 DONE、9/9 fresh final 全 PASS；加上额外 synth-fix 后共 12/12 DONE、31258 Tokens、526 Credits、15/38/32/18 次 LLM/CSim/Synth/CoSim、1645.641 秒。dotProduct 三次分别为 `1027 → 38`、`1027 → 518`、`1027 → 1027 baseline fallback`。

XSIM A03 对 A01/A02 的四个失败任务使用全新目录重跑后为 4 accepted / 0 rejected；源码/镜像基线 `a796da3` 的 Docker 镜像也已重建并通过 demo-smoke 和 382 项容器测试。下面 8.1～8.3 保留较早的独立成功、smoke 和失败历史，用于解释系统怎样走到当前状态，不应再当作最新结论。

### 8.1 历史独立 dotProduct：真实成功

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
| Wall time | 130.781 seconds |

后两轮 Planner 提出的 Patch 因超过改动行数限制而在 Candidate 创建前被拒绝，没有再次调用 Vitis。系统保留第一轮的 `candidate_001`，并完成全新的 Final 三阶段验证。

注意：这是公开任务上的真实本地结果，不等于比赛 hidden grader 最终成绩；reference harness 的评分公式也不能当作已公布的最终官方评分公式。

### 8.2 历史 V3-C 三道官方题目：编排 smoke 通过

当前 smoke 使用官方题目源码、scripted Patch 和 deterministic backend，用于证明 Graph 路由与工具顺序，不代表真实模型或真实 Vitis 能力。

| 任务 | Baseline 现象 | 路由 | 最终状态 | CSim/Synth/CoSim | Credits |
|---|---|---|---|---:|---:|
| `projection_bugfix` | CSim FAIL | REPAIR | DONE | 3 / 2 / 1 | 31 |
| `dotProduct_optimize` | CSim、Synth PASS | OPTIMIZE | DONE | 3 / 3 / 1 | 35 |
| `residual_stream_deadlock` | CoSim deadlock | STRUCTURAL_FIX | DONE | 3 / 2 / 3 | 71 |

此外还有一个合成的资源超限 Graph 测试，证明“工具返回 Synth PASS，但资源超出上限”时仍会进入 `SYNTH_FIX`，不会误进入优化。

### 8.3 历史 projection A01–A03：旧 Patch policy 失败

> 这三次失败已由 A04 后续成功闭环。保留本节是为了说明“模型诊断正确但 Patch 落地失败”怎样推动 `d1cf869`，不是当前 REPAIR 状态。

下面三次不是 fake smoke：都调用了真实 Vitis baseline CSim 和真实 OpenAI-compatible `deepseek-v4-pro`，并使用独立 run 目录。但它们都发生在 Patch 重定位修复之前，因此不能写成 REPAIR 成功。

| Attempt | Baseline / Router | 模型判断 | 停止位置 | Token | Credit | Candidate / Final |
|---|---|---|---|---:|---:|---|
| A01 | CSim FAIL → REPAIR | 正确发现 `angle==0` 漏掉 `z2 / 3` | Patch hunk 声明第 12 行，实际唯一上下文在第 14 行，旧策略拒绝 | 2034 | 1/40 | 未创建 / 未运行 |
| A02 | CSim FAIL → REPAIR | 同样正确 | 同上 | 2027 | 1/40 | 未创建 / 未运行 |
| A03 | CSim FAIL → REPAIR | 同样正确 | 同上 | 2033 | 1/40 | 未创建 / 未运行 |

对应本机 run 目录：

```text
llm4hls_harness/runs/v3c-real-projection-repair-a01-LOCAL_BUDGET_OVERRIDE/
llm4hls_harness/runs/v3c-real-projection-repair-a02-LOCAL_BUDGET_OVERRIDE/
llm4hls_harness/runs/v3c-real-projection-repair-a03-LOCAL_BUDGET_OVERRIDE/
```

三个不同模型 request ID 证明它们是三次真实请求；A02/A03 的 `cached_input_tokens` 是服务端 Prompt 前缀缓存，不是复用旧 run 结果。三次合计 6094 Tokens、3 Credits、约 28.9 秒，只运行了 3 次 baseline CSim，没有 Synth/CoSim。

这组失败证明了三件事：

1. 真实 PhaseRouter 和 Failure Evidence 已经能把任务送到正确模式；
2. 真实模型能理解并修正功能 Bug；
3. 当时的直接阻塞在“Planner diff → Candidate materialization”，不是 Vitis 环境或模型诊断。

`d1cf869` 已修复该阻断：行号错误时，只有 old-hunk 在 kernel 中存在唯一逐字匹配才自动重定位；零匹配、多匹配和非 kernel 目标仍拒绝。修复后只要 Candidate 成功物化，系统会在 Candidate/Trace 中同时保存 Planner 原 Patch hash、实际应用 Patch hash 和 normalization 标记；A01～A03 发生在修复前且没有创建 Candidate，因此只有 Planner 原 Patch 证据。回归覆盖了同一官方 projection 的 `-12 → -14` 错位模式，以及大偏移、歧义、缺失和越权路径。

后续 A04 已在新目录执行：2043 Tokens、31 Credits、83.919 秒，Candidate 和 fresh final CSim/Synth/CoSim 全部 PASS。REPAIR 因而已从黄色转为绿色；A01–A03 仍作为真实失败历史保留。

这三次都以 40 Credits 本地覆盖运行，必须标记 `LOCAL_BUDGET_OVERRIDE`；不能宣称满足官方样例的 20-Credit 预算。

### 8.4 当前自动测试

截至代码基线 `a796da3`：

- 宿主完整回归：382 tests PASS；
- 源码/镜像基线 `a796da3` Docker quick-tests：`Ran 382 tests; OK (skipped=3)`；
- Docker `demo-smoke`：PASS；
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
- 限制模型只能修改 kernel，并自动修正 old-hunk 唯一逐字匹配的错误 diff 行号；
- 在预算内多轮创建、验证、晋升或拒绝 Candidate；
- 从 Synth 提取 top latency、transaction interval、loop II、TripCount 和资源；
- 对低价值 Candidate 跳过探索 CoSim；
- 中断后通过 checkpoint 和 action journal 恢复；
- 对最终 Candidate 重新执行完整验证；
- 生成逐节点、逐 Candidate、逐预算的团队报告；
- 构建并 Oracle 验证 28 道四模式 fast corpus；
- 串行执行模型 × 任务 × repeat，单题失败后继续并保持独立 run；
- 用 TopInterfaceGuard 独立保护顶层硬件 ABI；
- 在源码/镜像基线 `a796da3` 的 Docker clean-room 中运行 demo-smoke 和快速回归；
- 生成脱敏 submission 报告、receipt、staging 和秘密扫描。

### 9.2 还不能宣布完成

- Qwen3.5/Qwen3.6 尚无真实运行；当前缺少可达 endpoint、key 和服务端实际 model alias，本机没有本地 Qwen serving 或权重；
- 28 题 corpus 已扩充任务形状，但只有 12 道提交级真实 Vitis anchors，尚不能等价为 hidden grader 泛化证明；
- DeepSeek 已完成重复矩阵，但 strict/fast 对照和 Evidence/CoSim gate 消融尚未运行；
- 当前所谓 PPA 评价主要是 latency/II、clock 和资源占用的 proxy；没有可靠的板上或 post-route Power 测量，不能宣称已经优化真实功耗；
- Agent-only Docker 和复现入口已完成，但最终官方 Docker/Vitis 部署口径、最终 `.zip`、两页论文和 5 分钟视频尚未冻结；
- 真实运行必须在**执行命令的那个终端**设置 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`LLM4HLS_MODEL`；不同 Codex/shell 进程不会自动继承，任何密钥都不得进入 Git、Prompt 审计文件或报告；
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

因此 deterministic 完整 smoke 使用本地测试配置；A01–A04 和 projection 重复矩阵使用 40-Credit 上限并显式写入 `LOCAL_BUDGET_OVERRIDE`。真实比赛前必须确认：

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
| V3-B optimize | 绿色 | dotProduct 独立 1027 → 38；重复矩阵两次改善、一次安全 baseline fallback，全部 Final PASS | 补 Qwen 和 strict/fast 对照 |
| V3-C PhaseRouter | 绿色 | 四 mode 均有真实 baseline 分诊 + LLM Patch + Vitis fresh final | 只维护阻断性问题 |
| V3-C REPAIR | 绿色 | projection A04 + 重复 3/3，fresh final 全 PASS | 继续明确 20/31 Credit 口径 |
| V3-C SYNTH_FIX | 绿色 | dynamic fixture 独立验收 + 重复 3/3，fresh final 全 PASS | 增加更多真实综合错误形状 |
| V3-C STRUCTURAL_FIX | 绿色 | residual 独立验收 + 重复 3/3；A03 三道 structural anchor accepted | 扩充不同 stream/FIFO 根因 |
| V3-D corpus/oracle/batch | 绿色偏黄 | 28 题、28/0 deterministic Oracle、12 个真实 Vitis receipts、batch runner | 增加真实模型 corpus 子集 |
| 模型覆盖 | 黄色 | DeepSeek 12-run 矩阵完整 | Qwen3.5/Qwen3.6 外部服务配置 |
| 泛化与稳定性 | 黄色 | 28 题 corpus、12 anchors、DeepSeek 重复成功率 | hidden-like 真实模型运行和消融 |
| Docker/复现包 | 绿色偏黄 | 源码/镜像基线 `a796da3`、demo-smoke、382 容器测试 PASS | 官方部署格式和外部 Vitis 口径最终确认 |
| 论文/视频 | 黄色 | 实验表、失败分析、复现、Demo 草稿已有 | 两页正文、最终图表、真实视频和提交 QA |

当前总体判断：

> 工程主体已经进入“绿色偏黄”：四模式和 DeepSeek 重复实验已闭环，主要风险从 Agent 实现转移到 Qwen 外部服务、官方预算/容器口径、消融与最终交付。

## 11. 接下来的阶段计划

按团队 2026-07-10 保存的官网规则快照，比赛技术材料截止时间约为北京时间 2026-08-08 19:59；若官方后续更新 FAQ 或提交入口，应以最新通知为准。现在不适合继续增加新 Agent、RL、RAG 或复杂 Controller，应以证据闭环和提交为主。

### 7 月 20 日晚至 7 月 21 日白天：已完成里程碑

本阶段已经交付：

1. projection A04、residual A01、synth-fix A01 三种非 optimize mode 的真实模型 + Vitis fresh final；
2. 28 题 fast corpus、deterministic Oracle 28/0 和四 mode 各 3 个真实 Vitis receipts；
3. Batch Benchmark、TopInterfaceGuard、Docker/clean-room 和 submission QA 工具与草稿；
4. XSIM A03 对四个失败 anchor 的 4/4 恢复；
5. DeepSeek 官方三题各 3 次及额外 synth-fix 3 次，全部 run 和失败/回退均保留；
6. 脱敏 JSON/Markdown 总结，避免关键事实只存在于被 Git 忽略的 `runs/`。

仍需对齐“Agent 搜索预算”与“外部 grader/final closure 是否计费”的官方口径，解决 projection 公开 20 Credits 小于本地完整 closure 最低 31 Credits 的结构性冲突。

### 7 月 21 日至 7 月 31 日：补齐对比与稳定性

目标：从“跑通过一次”变成“可以比较和写论文”。

优先级：

1. 获得 Qwen3.5 与 Qwen3.6 的真实 endpoint/key/model alias，并复用相同 task、预算、Prompt、Vitis 版本和超时；
2. Qwen 每个推荐模型至少跑官方三题一轮，保留全部失败；
3. 运行 strict/fast 对照，以及有/无结构化 Evidence、有/无 CoSim risk gate 的最小消融；
4. 从 28 题 corpus 选取四 mode 小子集做真实模型运行，检查公开样例硬编码；
5. 将成功率、平均/最好 latency、Token、Credits、工具次数和 wall time 固化进论文表。

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

现在最值得做的不是继续修改 optimize loop 或增加新架构，而是：

> 获得 Qwen3.5/Qwen3.6 可达的 OpenAI-compatible endpoint、key 和服务端真实 model alias，然后用与 DeepSeek 完全相同的三道官方任务、预算、Prompt、Vitis 和超时跑第一轮公平矩阵。

原因是 A04、residual、synth-fix、DeepSeek 12-run、XSIM A03 和源码/镜像基线 `a796da3` 的 Docker 都已经完成。继续堆 Controller 的边际收益很低；目前论文和比赛最缺的是跨模型公平证据。Qwen 配置不可用时，不应把“未运行”写成 0% 成功率，也不应把 Hugging Face repo ID 猜成 API alias。

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
- [V3-D 昨夜至今日执行总结](../../llm4hls_harness/releases/v3d-overnight-daytime-summary-2026-07-21.md)
- [V3-D Corpus](../../llm4hls_harness/task_corpus/v3d-fast/README.md)
- [提交实验表](../../docs/submission/experiment_tables.md)
