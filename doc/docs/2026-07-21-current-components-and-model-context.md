# V3 当前组件、模型控制链与参考信息报告

- 更新时间：2026-07-21
- 对应分支：`feat/v3e-experience-guided-planner`
- 对应代码基线：`8af3763`
- 面向读者：新加入项目的组员、调试 Planner 的开发者、实验与报告负责人
- 本文依据：当前仓库代码，不以历史聊天或旧设计图作为实现事实

## 1. 先说结论

当前系统不是“把所有信息交给大模型，让大模型自己决定一切”。它由三部分组成：

```text
控制面：Python/LangGraph 决定当前阶段、预算、工具路径和停止条件
推理面：一个 OpenAI-compatible Planner 根据受限上下文提出假设和 Patch
验证面：Patch、Candidate、CSim、Synth、CoSim 和 Final closure 判定结果是否可用
```

模型只有一项核心权力：

> 根据当前任务模式和已提供证据，提出一份 kernel 修改建议。

模型没有以下权力：

- 不能选择或提高预算；
- 不能直接调用 CSim、Synth、CoSim 或 shell；
- 不能修改 header、testbench、task metadata、hidden 或 reference；
- 不能让自己的 Candidate 晋升；
- 不能指定哪个 Candidate 成为 final；
- 不能声称“已经通过工具验证”来代替真实结果。

当前真正会影响模型思考内容的主要组件是：

1. `PhaseRouter`：告诉模型现在是修功能、修综合、修 RTL 结构，还是做性能优化；
2. `V3OpenAIPlannerAdapter`：从运行状态中挑选哪些内容可以进入模型上下文；
3. `Failure/Synth Evidence`：把 Vitis 结果压缩成模型可读的事实；
4. Prompt builder 和 System Prompt：规定目标、输出格式、可用策略和权限边界；
5. 当前 best Candidate 与有界历史：决定模型下一轮从什么代码继续、哪些方法不要重复；
6. Budget 摘要：告诉模型还剩多少 Token、Credit 和轮次，但不允许模型修改预算；
7. V3-E 经验层：只有 `guided` 模式会把历史建议加入 Prompt；`shadow` 只记录，不影响模型请求。

## 2. 一道题在系统里的完整流程

当前真实纵向流程如下：

```text
读取公开 Task Package
  ↓
创建不可变 baseline Candidate
  ↓
Baseline CSim / Synth / 必要时 CoSim
  ↓
PhaseRouter 选择 mode
  ├─ REPAIR
  ├─ SYNTH_FIX
  ├─ STRUCTURAL_FIX
  └─ OPTIMIZE
  ↓
预算门判断本轮是否仍付得起，并保留 Final 预算
  ↓
整理当前代码、Evidence、历史、预算和约束
  ↓
调用一个 OpenAI-compatible Planner
  ↓
严格 JSON 解析，取得 hypothesis、risk、strategy/change class 和 unified diff
  ↓
Patch Validator + TopInterfaceGuard
  ↓
创建隔离、不可变 Candidate
  ↓
按 mode/profile 运行 CSim / Synth / CoSim
  ↓
程序决定 promote、reject、继续下一轮或停止
  ↓
对最终 Candidate 重新运行 fresh CSim → Synth → CoSim
  ↓
写入报告、账本、trace、Candidate registry 和 checkpoint
```

这条链上，大模型只位于“提出 Patch”这一段。它前面有分诊和上下文筛选，后面有安全检查和真实工具验证。

## 3. 当前全部主要组件

下面按“核心运行组件、模型组件、验证安全组件、经验组件、批量评测组件”分类。这里的“组件”是职责单元，不一定对应一个同名 class。

### 3.1 任务输入与主流程组件

| 组件 | 代码位置 | 输入 | 输出/职责 | 是否直接影响模型 |
|---|---|---|---|---|
| V3 CLI | [`v3_prototype_cli.py`](../../llm4hls_harness/llm4hls_agent/v3_prototype_cli.py) | 命令行、模型环境变量、任务目录、预算、profile | 创建 Planner、Backend、ExperienceCoordinator 和 RunConfig | 是，模型名、温度、Token 上限、profile、experience mode 都从这里进入 |
| Task Loader | [`task.py`](../../llm4hls_harness/llm4hls_agent/task.py) | `task.toml`、公开 kernel/header/description/public TB | 形成 `PublicTask`，拒绝 hidden/reference 等路径 | 是，提供任务公开事实；public TB 内容不会发送给 Planner |
| LangGraph 总装 | [`v3_prototype.py`](../../llm4hls_harness/llm4hls_agent/v3_prototype.py) | `PublicTask`、Planner、Backend、RunConfig | 串起 baseline、路由、规划、Candidate、验证、final 和报告 | 是，决定是否调用模型、何时调用以及调用几轮 |
| Graph State | `V3PrototypeState`，位于 `v3_prototype.py` | 节点产生的小型状态和 Artifact 引用 | 保存 mode、round、best/active/final Candidate、最近结果 | 间接影响；它是下一轮 Planner Context 的来源 |
| Checkpoint | LangGraph `SqliteSaver` | 每个节点完成后的 Graph State | 中断恢复，避免从头重复执行 | 不改变模型内容；可能复用已完成请求，避免重复调用 |

### 3.2 分诊、路由与停止组件

| 组件 | 真实实现 | 它决定什么 | 它不决定什么 |
|---|---|---|---|
| PhaseRouter | [`v3_phase_router.py`](../../llm4hls_harness/llm4hls_agent/v3_phase_router.py) + Graph `phase_router` 节点 | 根据 baseline 结果选择 `REPAIR`、`SYNTH_FIX`、`STRUCTURAL_FIX`、`OPTIMIZE` | 不诊断具体代码，不生成 Patch，不调用工具 |
| Planner route | `v3_openai_planner.py::prepare()` | 根据 mode 选择 task-aware Prompt 或 optimize Prompt | 不批准 Candidate，也不决定工具结果 |
| Validation profile route | CLI + `v3_prototype.py` 条件边 | `strict` 或 `fast-experiment` 下 baseline/Candidate 走哪些验证节点 | 不修改模型返回的 Patch |
| Candidate tool route | `candidate_csim/synth/cosim` 后的条件边 | 根据 PASS/FAIL、mode 和 profile 决定下一工具或拒绝 | 不是一个 LLM Router |
| CoSim risk gate | `_fast_experiment_risk()`、`candidate_score_gate`、`candidate_cosim_budget_gate` | 高风险优化是否必须探索 CoSim，低风险收益是否可推迟到 final | 不允许跳过 fresh final CoSim |
| Stop/budget route | `evaluate_*_budget`、`advance_round`、final 路由 | 预算不足、达到最大轮次或连续无提升时停止探索 | 不让模型自行延长轮次或超预算 |
| Final fallback route | `select_final_attempt`、`evaluate_final_fallback` | final 失败后是否有可支付且合格的历史 Candidate 可重试 | 不回退到一个未满足当前正确性门的 Candidate |

这里要特别区分四种“Router”：

```text
PhaseRouter       = 决定当前任务目标是什么
Planner route     = 决定使用哪一种 Prompt/Context
Tool route        = 决定下一步跑 CSim、Synth、CoSim 还是拒绝
Stop/final route  = 决定继续探索、停止或尝试 final fallback
```

当前只有 `PhaseRouter` 是独立的纯 Python 分诊器。所谓 Tool Router、Risk Router 和 Stop Router，目前由 LangGraph 条件边与若干纯函数共同实现，不是额外的 LLM Agent。

### 3.3 模型与 Prompt 组件

| 组件 | 代码位置 | 作用 | 对模型的影响 |
|---|---|---|---|
| V3 Planner Adapter | [`v3_openai_planner.py`](../../llm4hls_harness/llm4hls_agent/v3_openai_planner.py) | 从 Artifact、State、Evidence、历史和预算中构造受限 Context | 决定模型实际能看到什么 |
| OpenAI-compatible Provider | [`openai_provider.py`](../../llm4hls_harness/llm4hls_agent/openai_provider.py) | 生成 System/User Prompt，发 HTTP 请求，解析严格 JSON 和 Token usage | 决定模型角色、格式、采样配置和输出 schema |
| Planner contract | [`v3_planner.py`](../../llm4hls_harness/llm4hls_agent/v3_planner.py) | 定义 Planner 输入、输出、canonical hash 和 scripted Planner | 保证上下文和结果可校验、可去重 |
| Planner action journal | [`v3_planner_action.py`](../../llm4hls_harness/llm4hls_agent/v3_planner_action.py) | 对不可重放 LLM 调用记录 STARTED/COMPLETED、输入输出 hash、Token | 不改变回答内容；防止崩溃后静默重复收费调用 |
| System Prompt | `FAST_EXPERIMENT_SYSTEM_PROMPT` / `TASK_AWARE_SYSTEM_PROMPT` | 定义 AMD Vitis HLS 角色、权限边界、只输出 JSON、只改 kernel | 直接约束模型行为 |
| User Prompt builder | `build_fast_experiment_prompt()` / `build_task_aware_prompt()` / 旧 `build_optimization_prompt()` | 把 Context 渲染成模型请求 | 直接影响模型依据和优化方向 |
| JSON response validator | `openai_provider.py` 中的 strict response 解析 | 检查必需字段、strategy 数量、risk 和 Patch | 模型输出不合规时，Candidate 不会被创建 |

当前只有一个 Planner，不是四个不同模型 Agent。`mode` 改变它的目标和 Prompt：

| Mode | 模型目标 | 主要参考证据 | 预期输出类别 |
|---|---|---|---|
| `REPAIR` | 修复 CSim 编译、运行或功能错误 | `CSimFailureEvidence` | `FUNCTIONAL_REPAIR` |
| `SYNTH_FIX` | 修复不可综合、时钟或资源失败 | `SynthFailureEvidence` | `SYNTHESIS_REPAIR` |
| `STRUCTURAL_FIX` | 修复 RTL mismatch、deadlock、stream/FIFO/interface 问题 | `CoSimFailureEvidence` | `STRUCTURAL_REPAIR` |
| `OPTIMIZE` | 在保持正确性的条件下降低 worst latency | `SynthEvidence`、最近失败和已尝试策略 | 一到三个 strategy bundle + Patch |

### 3.4 Evidence 组件

| 组件 | 代码位置 | 从哪里来 | 提供给模型什么 |
|---|---|---|---|
| Synth Evidence | [`v3_evidence.py`](../../llm4hls_harness/llm4hls_agent/v3_evidence.py) | Synth report/XML 和有限 scheduling 文本 | top latency、transaction interval、loop II、TripCount、pipeline 状态、memory/scheduling 证据、资源 |
| CSim Failure Evidence | [`v3_failure_evidence.py`](../../llm4hls_harness/llm4hls_agent/v3_failure_evidence.py) | 结构化 ToolResult 和有界日志片段 | compile/runtime/functional 摘要、源码位置、expected/actual（若能确认） |
| Synth Failure Evidence | 同上 | Synth ToolResult 和有限失败信息 | synthesis error、unsupported construct、clock/resource violation |
| CoSim Failure Evidence | 同上 | CoSim ToolResult 和有限失败信息 | deadlock、timeout、RTL mismatch、stream/FIFO/interface 线索 |
| 有界历史摘要 | `v3_openai_planner.py` | 最近 Candidate 结果和 Patch digest | 最近失败/无提升原因、尝试过的 strategy bundle，避免重复 |

Evidence Extractor 的作用不是“替模型思考”，而是把原始工具输出变成少量、可追踪的事实。当前约束包括：

- 不把几千行 Vitis log 整段塞给模型；
- 不把 top transaction interval 当成 loop achieved II；
- 缺少某项证据时保留未知，不伪造 latency、II 或根因；
- Failure Evidence 会限制日志行数、单行长度、源码位置数量并清理绝对路径/秘密信息。

### 3.5 Patch、Candidate 与接口安全组件

| 组件 | 代码位置 | 作用 | 对下一轮模型的影响 |
|---|---|---|---|
| Patch normalizer/relocator | [`repair.py`](../../llm4hls_harness/llm4hls_agent/repair.py) | 规范化 diff header；当 old-hunk 在当前源码中唯一逐字匹配时允许重定位错误行号 | 拒绝原因进入历史，下一轮模型可换方法 |
| Patch Validator | `repair.py` | 检查只改 kernel、修改规模、语法、路径穿越和非法文件 | 决定模型建议能否落地，不直接改变本轮回答 |
| TopInterfaceGuard | [`top_interface_guard.py`](../../llm4hls_harness/llm4hls_agent/top_interface_guard.py) | 保护 top 名称、签名、参数、返回类型、public symbol 和 interface pragma | 越权 Patch 被拒绝，并可成为下一轮失败摘要 |
| Candidate Manager | [`candidate.py`](../../llm4hls_harness/llm4hls_agent/candidate.py) | 原子保存 baseline/Candidate、父子关系、源码和 hash | promote 后的新 best 代码会成为下一轮模型的 `current_kernel` |
| Duplicate detector | `v3_prototype.py::_materialize_candidate()` | 拒绝重复 Patch；fast optimize 还拒绝重复 strategy bundle | 防止同一建议重复消耗 Vitis |
| Scoring/Comparator | [`scoring.py`](../../llm4hls_harness/llm4hls_agent/scoring.py) | 正确性、时钟、资源和 PPA/公开 score proxy 比较 | promote/reject 结果会写入下一轮历史 |

### 3.6 工具、预算与真实验证组件

| 组件 | 代码位置 | 作用 | 是否由模型控制 |
|---|---|---|---|
| Budget Ledger | [`budget.py`](../../llm4hls_harness/llm4hls_agent/budget.py) | 对 Token、Credit、工具次数、runtime 做追加式计费和硬限制 | 否；模型只能看摘要，不能改账本 |
| ToolServer | [`tools.py`](../../llm4hls_harness/llm4hls_agent/tools.py) | CSim/Synth/CoSim 的唯一受预算、审计和缓存保护的入口 | 否；由 Graph 指定 stage |
| Vitis Backend | [`vitis.py`](../../llm4hls_harness/llm4hls_agent/vitis.py) | 启动真实 Vitis 2025.2、生成 Tcl、解析结果和保存日志 | 否 |
| Candidate validation | Graph 工具节点；V1/V2 共用部分在 [`validation.py`](../../llm4hls_harness/llm4hls_agent/validation.py) | 依序验证 Candidate | 否 |
| CoSim gate | `v3_prototype.py` | 只有严格改善且风险/预算满足时才花探索 CoSim；低风险可推迟 | 否；Planner risk 只是输入之一，程序还会扫描 Patch 和 task |
| Fresh final closure | `final_csim`、`final_synth`、`final_cosim` | 最终代码重新执行三项验证 | 否，三项 PASS 才能 DONE |

`fast-experiment` 中的风险判断会检查：

- 题目是否 `requires_cosim=true`；
- strategy bundle 是否含 `DATAFLOW`、`STREAMING`、`BITWIDTH_OPTIMIZATION`；
- Patch 是否出现 `hls::stream`、FIFO、interface、dataflow、`ap_int/ap_fixed` 等变化；
- Planner 是否声明 HIGH risk；
- 额外 Candidate CoSim 后是否仍付得起完整 final closure。

因此模型写的 `risk=LOW` 不是最终裁决。如果 Patch 实际改了 stream/interface，程序仍会判为需要 CoSim。

### 3.7 V3-E 经验组件

V3-E 是“给 Planner 的历史建议层”，不是新 Agent，也不是新的 Graph。

| 组件 | 代码位置 | 作用 | 权限 |
|---|---|---|---|
| Experience schema | [`v3_experience.py`](../../llm4hls_harness/llm4hls_agent/v3_experience.py) | 定义安全、版本化的经验记录/查询/建议契约 | 只描述历史事实 |
| Experience Store | [`v3_experience_store.py`](../../llm4hls_harness/llm4hls_agent/v3_experience_store.py) | append-only JSONL、锁、幂等写入、冻结快照 | 不能修改 Candidate 或工具状态 |
| Feature/Retrieval/Advisory | [`v3_experience_guidance.py`](../../llm4hls_harness/llm4hls_agent/v3_experience_guidance.py) | 固定特征、相似案例检索、策略排序、risk/continue 建议 | 只输出有界建议，不输出 Patch |
| Historical importer | [`v3_experience_importer.py`](../../llm4hls_harness/llm4hls_agent/v3_experience_importer.py) | 从历史真实 run 提取脱敏 Candidate 经验，排除 fixture/oracle | 不把源码、Prompt、日志带入经验库 |
| Experience Coordinator | `v3_experience_guidance.py` | 生成并持久化每轮建议 | 只有 guided 时建议进入 Prompt |
| Pilot comparator | [`v3_experience_pilot.py`](../../llm4hls_harness/llm4hls_agent/v3_experience_pilot.py) | 离线比较 shadow/guided 批次 | 不执行模型或 Vitis |

三种模式对模型的真实影响：

| Experience mode | 是否检索/记录建议 | 是否改变模型 Prompt | 失败时行为 |
|---|---:|---:|---|
| `off` | 否 | 否 | 完全保持 V3-D 路径 |
| `shadow` | 是 | 否 | fail-open，经验层失败不阻塞原 Planner |
| `guided` | 是 | 只有建议可行动时才加入 | 经验契约错误会显式失败，避免悄悄污染实验 |

当前经验层只发送推荐/避免的策略、相似样本统计、历史收益/代价和 advisory。它不发送历史源码、完整 Patch、日志、Prompt、绝对路径或 task ID 明文。

### 3.8 V3-D 数据集、Oracle 和批量实验组件

这些组件位于 Agent 外层，用于准备题目和评测，不参与单次 Planner 思考。

| 组件 | 代码位置 | 作用 | 是否进入模型 Prompt |
|---|---|---|---|
| Fast corpus | [`v3d_corpus.py`](../../llm4hls_harness/llm4hls_agent/v3d_corpus.py) | 生成/验证四类开发任务和隔离的 golden/hidden-like 材料 | 运行时只允许公开 task package 部分进入 Agent |
| Corpus Oracle | [`v3d_oracle_validator.py`](../../llm4hls_harness/llm4hls_agent/v3d_oracle_validator.py) | 离线检查题目 baseline/golden 是否命中预期 gate | 否，Oracle/golden 不得进入 Planner |
| Anchor receipts | [`v3d_anchor_receipts.py`](../../llm4hls_harness/llm4hls_agent/v3d_anchor_receipts.py) | 绑定可移植真实 Vitis 锚点证据 | 否 |
| Batch benchmark | [`v3_batch_benchmark.py`](../../llm4hls_harness/llm4hls_agent/v3_batch_benchmark.py) | 运行模型 × 任务 × repeat，保留失败并汇总 | 只通过正常 CLI 启动单题 Agent，不额外泄露答案 |

### 3.9 报告、审计与旧流程兼容组件

| 组件 | 代码位置 | 当前作用 | 是否影响模型 |
|---|---|---|---|
| V3 Team Report | `v3_prototype.py` 的 `write_report` 路径 | 汇总每轮 Planner、Candidate、工具、预算、路由和 final | 否，只读取已发生事实 |
| Trace / Manifest | `v3_prototype.py`、[`artifacts.py`](../../llm4hls_harness/llm4hls_agent/artifacts.py) | 保存节点事件、hash 绑定和终态产物完整性 | 否 |
| V2 Team Report | [`v2_team_report.py`](../../llm4hls_harness/llm4hls_agent/v2_team_report.py) | 生成 V2 逐轮复盘报告 | 不参与 V3 Planner |
| V0/V1/V2 CLI | [`cli.py`](../../llm4hls_harness/llm4hls_agent/cli.py) | 保留旧阶段运行与验收入口 | 与 V3 入口并存，不会自动混入 V3 Graph |
| V0 workflow | [`workflow.py`](../../llm4hls_harness/llm4hls_agent/workflow.py) | 保留 baseline、工具调用和 Artifact 底层契约 | V3 复用部分公共规则，不直接构造当前 Prompt |
| V1 repair / V2 optimization | [`repair.py`](../../llm4hls_harness/llm4hls_agent/repair.py)、[`optimization.py`](../../llm4hls_harness/llm4hls_agent/optimization.py) | V3 复用 Patch 处理、HLS 规则、strict Selector 和部分 CoSim gate | 只有被 V3 Adapter/Graph 显式调用的部分才影响当前流程 |

因此，新组员看到 V0/V1/V2 文件时不要误认为系统同时跑了多套 Agent。当前 V3 单题实验入口是 `v3_prototype_cli.py`，旧流程是兼容和回归基线。

## 4. PhaseRouter 到底怎样影响模型

`PhaseRouter` 是模型之前的确定性分诊。规则是：

| Baseline 事实 | Router 输出 | 模型收到的目标 |
|---|---|---|
| CSim FAIL | `REPAIR` | 修复功能、编译或运行错误 |
| CSim PASS，Synth FAIL | `SYNTH_FIX` | 修复综合、时钟或资源问题 |
| CSim/Synth PASS，需要的 CoSim FAIL | `STRUCTURAL_FIX` | 修复 RTL 结构行为 |
| CSim/Synth PASS，且所需 CoSim 也满足 | `OPTIMIZE` | 降低 latency/PPA proxy |

Router 对模型有三层影响：

1. 改变 `MODE` 和 `OBJECTIVE`；
2. 改变主要 Evidence 类型；
3. 改变模型回答必须使用的 `change_class` 和后续 Candidate 验证路径。

Router 不会告诉模型具体应加哪条 pragma，也不会选择 Patch。比如它只会判断 `STRUCTURAL_FIX`，具体是调整 FIFO 深度、读写顺序还是取消错误 DATAFLOW，仍由 Planner 根据 CoSim Evidence 提议，最后由真实工具判定。

## 5. 模型实际看到什么

### 5.1 每次调用都包含的基础内容

| 参考内容 | 来源 | 用途 |
|---|---|---|
| System Prompt | `openai_provider.py` 常量 | 定义角色、权限、严格 JSON 和只改 kernel |
| mode/objective | PhaseRouter + Planner Adapter | 告诉模型本轮究竟在修什么或优化什么 |
| 当前 best kernel | Candidate artifact | 模型 Patch 的唯一代码基准 |
| public task description | `description.md` | 解释功能目标和公开约束 |
| task metadata | `task.toml` 经 Task Loader 白名单化 | top、kernel 文件、part、clock、requires_cosim；fast optimize 还包含 difficulty |
| read-only header 内容 | 公开 header | 让模型理解类型、常量和函数签名，但禁止修改 |
| allowed/read-only constraints | Planner Adapter | 明确只可修改 kernel，必须保持 top/interface/数值语义 |
| budget summary | BudgetLedger/Graph policy 的派生值 | 剩余 Token、Credit、轮次和 final reserve |

注意：当前 Prompt 会列出 public testbench 的文件名作为只读约束，但不会把 public testbench 源码内容发送给 Planner。

### 5.2 修复模式额外看到的内容

`REPAIR`、`SYNTH_FIX`、`STRUCTURAL_FIX` 使用 task-aware Prompt，并额外获得一份与 mode 对应的 `failure_evidence`：

```text
REPAIR         → CSim failure summary / source location / expected-actual
SYNTH_FIX      → synthesis error / unsupported construct / clock-resource violation
STRUCTURAL_FIX → deadlock / timeout / RTL mismatch / stream-FIFO-interface evidence
```

它不会收到无关的完整历史日志。

### 5.3 Fast OPTIMIZE 额外看到的内容

`fast-experiment` 下，模型会获得：

- top worst latency；
- top transaction interval；
- estimated clock period；
- 每个关键 loop 的 achieved II、TripCount 和 pipeline 状态；
- memory-port 或 scheduling 瓶颈摘要；
- LUT、FF、DSP、BRAM、URAM 和可用资源；
- 最近有限条失败/无提升原因；
- 已尝试的 strategy bundle 和 Patch digest；
- 允许的一到三个优化策略列表；
- 特别的 HLS 规则：transaction interval 不等于 loop II；loop 已经 II=1 时不能只返回 PIPELINE。

Fast Planner 可以从以下策略中组合最多三项：

```text
ARRAY_PARTITION, MEMORY_PARTITION, LOOP_UNROLL, PAR_FACTOR_TUNING,
MULTI_PARTIAL_SUM, PARALLEL_REDUCTION, MEMORY_BANKING,
LOCAL_LOOP_RESTRUCTURE, LOOP_PIPELINE, DATAFLOW, STREAMING,
BITWIDTH_OPTIMIZATION
```

### 5.4 Strict OPTIMIZE 的区别

`strict` 兼容路径仍会先由确定性 `select_optimization()` 选出一个优化类别，再让模型围绕该类别生成 Patch。因此这条路径中，Selector 会直接收窄模型的动作空间。

`fast-experiment` 则采用 autonomous strategy bundle：程序提供证据和允许策略，模型可以组合一到三项。这是当前真实多轮 optimize 实验主要使用的路径。

### 5.5 Guided 模式额外看到的经验

只有 `--experience-mode guided` 且检索结果可行动时，Prompt 才增加：

```text
EXPERIENCE GUIDANCE (ADVISORY ONLY)
```

其中可以包含：

- 相似历史状态下推荐或应避免的 strategy bundle；
- 平滑后的历史成功倾向；
- 典型 latency gain、Token、Credit 和 wall time 代价；
- 历史风险与是否值得继续的建议；
- 支撑建议的匿名、聚合证据。

当前 Synth/Failure Evidence、Budget、Patch Validator 和真实 Vitis 结果始终比经验建议更权威。

## 6. 明确不会给模型什么

以下信息不会进入 Planner Prompt：

- hidden testbench 内容；
- hidden grader 或正式评分私有信息；
- reference/golden kernel；
- mutation answer、Oracle 结论或 golden Patch；
- API key、Authorization header、cookie 等秘密；
- 本机绝对路径；
- 完整 Vitis 原始日志；
- 完整、不断增长的全部 Candidate 历史；
- 历史源码和历史完整 Patch；
- Graph checkpoint 数据库内部内容；
- “哪个 Candidate 应该 final”的答案。

这条边界同时由 Task Loader、Planner Adapter、Evidence schema、Experience schema、Prompt audit 和 Artifact hash 约束，不只靠一句 Prompt 提醒模型。

## 7. 哪些组件会间接影响下一次模型调用

有些组件不改变本轮 Prompt，但它们的结果会改变下一轮模型看到的状态：

| 组件结果 | 下一轮怎样变化 |
|---|---|
| Patch Validator 拒绝 | 记录 `PATCH_POLICY_REJECTED`，下一轮应提出不同或更小的 Patch |
| TopInterfaceGuard 拒绝 | 记录接口越权原因，下一轮必须保留 ABI |
| Candidate CSim FAIL | 失败摘要进入历史；incumbent 不变 |
| Candidate Synth 无提升 | 记录 latency 未严格改善；不跑无意义 CoSim；incumbent 不变 |
| Candidate 被 promote | 新 Candidate 成为下一轮 `current_kernel`，其 Synth Evidence 成为新性能事实 |
| Candidate CoSim FAIL | 结构风险结果进入后续判断，Candidate 不晋升 |
| Budget/stop gate 拒绝 | 不再调用模型，转入 final 或 FAILED |
| Checkpoint 命中已完成 Planner action | 复用已持久化输出，不重复请求模型 |

因此“模型表现”不能只看一次 response。一个完整 round 是：

```text
模型建议 → Patch 是否能应用 → Candidate 是否正确 → 是否综合 → 是否更快
→ 是否需要 CoSim → 是否晋升 → 反馈是否进入下一轮
```

## 8. 模型输出什么，程序怎样接管

### 8.1 Task-aware 修复输出

```json
{
  "hypothesis": "...",
  "primary_failure": "...",
  "evidence_used": ["..."],
  "change_class": "FUNCTIONAL_REPAIR|SYNTHESIS_REPAIR|STRUCTURAL_REPAIR",
  "expected_effect": "...",
  "risk": {"level": "LOW|MEDIUM|HIGH", "dimensions": []},
  "patch": "unified diff"
}
```

### 8.2 Fast optimize 输出

```json
{
  "hypothesis": "...",
  "primary_bottleneck": "...",
  "evidence_used": ["..."],
  "strategy_bundle": ["ARRAY_PARTITION", "LOOP_UNROLL"],
  "expected_effect": "...",
  "risk": {"level": "LOW|MEDIUM|HIGH", "dimensions": []},
  "patch": "unified diff"
}
```

返回后程序依次接管：

1. Provider 检查 JSON schema；
2. Planner action journal 保存请求/响应 hash 和 Token；
3. Patch Validator 检查并应用 diff；
4. TopInterfaceGuard 检查接口；
5. Candidate Manager 创建新版本；
6. Graph 运行真实验证；
7. Comparator/Router 决定晋升、拒绝、继续或 final。

## 9. 运行产物：怎样确认模型到底参考了什么

一次 V3 运行后按以下顺序查看：

| 文件/目录 | 能回答的问题 |
|---|---|
| `v3_team_report.md` | 整体运行了哪些轮、为什么晋升/拒绝、Token/Credit 和 final 结果 |
| `v3_prototype_result.json` | 程序最终认定的 mode、best、final、status 和 stop reason |
| `planner/inputs/` | 本轮 Planner 输入和 Artifact 绑定；用于追踪模型上下文 |
| `planner/outputs/` | 模型的 hypothesis、risk、策略和 Patch |
| `actions/*/result.json` | 每次 CSim/Synth/CoSim 的结构化真实结果 |
| `evidence/` | 从工具结果中提取并交给 Planner 的 Evidence |
| `candidate_registry.json` | baseline、Candidate 父子关系、best/active/final 和验证状态 |
| `budget_ledger.jsonl` | 每次 LLM/工具的 Token、Credit、次数和状态 |
| `trace.jsonl` | 节点时间线、route 原因和 promote/reject 原因 |
| `experience/experience_recommendations.jsonl` | shadow/guided 每轮生成了什么历史建议 |
| `graph_checkpoints.sqlite` | 恢复状态；通常不作为人工首查文件 |

如果怀疑“模型为什么会做这个修改”，最有效的排查顺序是：

```text
PhaseRouter mode
  → planner input
  → experience guidance（若 guided）
  → provider output
  → Patch policy / interface guard
  → Candidate tool result
  → promote/reject trace
```

## 10. 当前实现成熟度与限制

| 能力 | 当前状态 |
|---|---|
| 四 mode task-aware 路由 | 已实现，并有真实 LLM + Vitis 验收证据 |
| 真实 OpenAI-compatible Planner | 已实现 |
| Fast multi-round optimize | 已实现；dotProduct 有 1027 → 38 cycles 的独立真实成功证据 |
| Budget、Candidate、ToolServer、checkpoint、fresh final | 已实现 |
| V3-D corpus/oracle/batch | 已实现 |
| V3-E experience off/shadow/guided 工程路径 | 已实现 |
| 历史经验导入 | 当前已导入 26 条审计记录，其中 18 条真实 Candidate 可用于默认检索/排序 |
| V3-E 外部模型 shadow/guided 12 题效果结论 | 尚未获得；当前报告状态为 `BLOCKED / NOT_RUN`，不能宣称经验已提高成绩 |

当前最重要的事实是：

> V3-E 已经能够在 guided 模式改变模型参考内容，但“它是否比无经验更高分、更省 Token/Credit”仍需要同配置、同题、同模型的真实 shadow/guided 对照实验才能回答。

## 11. 新组员建议阅读顺序

1. 本报告，先理解组件和模型边界；
2. [`2026-07-20-current-system-and-team-progress.md`](2026-07-20-current-system-and-team-progress.md)，了解阶段进度和真实实验；
3. [`v3_prototype.py`](../../llm4hls_harness/llm4hls_agent/v3_prototype.py) 中的 `build_v3_prototype_graph()`，只看节点和边；
4. [`v3_phase_router.py`](../../llm4hls_harness/llm4hls_agent/v3_phase_router.py)，理解四种 mode；
5. [`v3_openai_planner.py`](../../llm4hls_harness/llm4hls_agent/v3_openai_planner.py)，理解哪些信息进入模型；
6. [`openai_provider.py`](../../llm4hls_harness/llm4hls_agent/openai_provider.py)，理解 System/User Prompt 和输出格式；
7. [`v3_evidence.py`](../../llm4hls_harness/llm4hls_agent/v3_evidence.py) 与 [`v3_failure_evidence.py`](../../llm4hls_harness/llm4hls_agent/v3_failure_evidence.py)，理解模型参考事实；
8. [`v3e-experience-layer-design.md`](../../docs/experiments/v3e-experience-layer-design.md)，理解经验层怎样只做建议；
9. 最后再看 Budget、Candidate、ToolServer 和测试。

## 12. 一句话记忆版

```text
PhaseRouter 决定“现在解决哪类问题”；
Planner Adapter 决定“模型能看到哪些事实”；
Prompt 决定“模型必须怎样回答”；
LLM 只提出 Patch；
Patch/Candidate/Tool/Score/Final 决定“这个建议能不能成为结果”；
Experience guided 只多给历史建议，永远不能替代当前 Vitis 证据。
```
