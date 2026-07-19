# V3-A Optimize 动作级 LangGraph 设计规范

- Status: written design for team review
- Date: 2026-07-19
- Scope: FPT26 Track A LLM4HLS Agent，V3-A optimize-only 纵向闭环
- Branch: `design/v3a-optimize-graph`

## 1. 文档定位

本文定义 V3-A 的首个可运行纵向切片。它是在 V2 已验证执行底座之上增加的增量设计，不重写 V2，也不把现有雏形误称为已经完成的 Controller、Router、Memory 或 Supervisor。

V3-A 的目标是：

> 将一个功能正确但性能待优化的公开 HLS kernel，通过动作级 LangGraph 编排完成 baseline、受限优化提议、逐级验证、Candidate 比较、停止、最终化和报告，并在进程中断后不重复调用或扣费；随后在不破坏编排等价性的前提下升级循环级证据和 Planner。

本文是实现规范，不是正式比赛规则。公开 Reference Harness 的 credits、预算、评分权重和样例任务都只作为可配置开发基线；最终比赛规则变化时必须通过配置适配。

## 2. 当前事实与问题

### 2.1 V2 已有的是执行机制

V2 已提供以下可复用能力：

- `PublicTask`：公开任务加载与输入哈希绑定；
- `BudgetLedger`：append-only 计费、reserve/complete、幂等与歧义动作处理；
- `CandidateManager`：不可变 baseline、Candidate 父子关系、源码/Patch 引用和哈希；
- `ToolServer`：受计费保护的 CSim、Synth、CoSim 执行与结果缓存；
- Patch 校验与 Candidate 物化；
- CSim、Synth、CoSim 验证服务；
- Reference/public-validation score proxy、Candidate 比较与 CoSim eligibility gate；
- Artifact、trace、acceptance 和团队复盘报告。

这些能力的准确称谓是 Ledger、Registry、Executor、Store、Validator、Comparator 和 Gate。它们是 V3 的底层积木，但尚不构成完整的 Budget Controller、Tool Router、Optimization Memory、Risk Analyzer、Supervisor 或 durable Graph checkpoint。

### 2.2 V2 当前的决策缺口

当前 optimize 流程存在以下关键缺口：

1. `run_v2()` 将 LLM、工具、预算、Candidate、比较、循环和最终化集中在一个大型命令式函数中，无法在单次副作用后形成 Graph checkpoint。
2. `validate_candidate()` 可在一次调用内连续执行 CSim、Synth 和 CoSim，不满足“一次 LLM/Vitis 副作用一个节点”的恢复边界。
3. 固定 Selector 会丢弃源码，只根据少量顶层指标指定唯一优化类别；LLM 不是优化策略的真正决策者。
4. 当前 Synth parser 主要读取顶层 latency、transaction interval、clock 和资源，没有形成循环级 II、TripCount、自动流水化状态和 memory-port 证据。
5. dotProduct 已证明顶层 transaction interval 可能被误当作 loop II，导致重复添加无效 PIPELINE。
6. `PublicTask` 已加载公开 header 和 description，但当前优化 Prompt 没有完整使用这些公开约束。
7. Tool action cache 可以恢复部分单次工具结果，但当前 Planner pending action 仍缺少完整 reconcile 语义，也没有保存 Graph 节点、路由原因、阶段、轮次和下一动作。

### 2.3 公开输入边界

V3-A 运行时允许使用：

- `task.toml` 中经过 typed schema 和字段白名单解析的公开配置；
- `description.md`；
- 当前 kernel；
- fixed/read-only headers；
- public testbench 和公开 build scripts，由 Harness/ToolServer 固定使用；
- Vitis 从公开输入产生的本次运行报告；
- 本次运行产生的 Candidate、Patch、失败摘要和预算信息。

V3-A 禁止将以下内容加载进 Agent State、Planner Prompt 或 Runtime Context：

- hidden testbench；
- reference/golden kernel；
- hidden grader 结果；
- API Key、认证头或无关环境秘密。

Public testbench 和 build scripts 可以由 Harness/ToolServer 使用，但 V3-A 默认不把它们或 raw TOML 发送给 LLM。若未来 Repair 场景需要局部测试契约，必须经过单独设计和 Prompt 审计。V3-A 的 `PublicTask` 和 Runtime Context 只构造公开视图，禁止探测 hidden/reference 路径。

## 3. 方案选择

### 3.1 方案 A：把 `run_v2()` 包成一个 Graph 节点

流程：

```text
START -> hydrate -> run_v2 -> report -> END
```

优点是改动最小，缺点是 Graph 看不到任何单轮 LLM、CSim、Synth、CoSim、晋升和停止边界。崩溃后只能重新进入整个 `run_v2` 节点，无法证明动作级 checkpoint。该方案只适合依赖安装 smoke，不算 V3-A 完成。

### 3.2 方案 B：确定性动作级 Graph + 受约束 Planner

每个 LLM 或 Vitis 调用是独立节点；普通 Python Policy 管预算、风险、路由、停止和比较；V2 服务负责执行和持久化。

优点：

- 能证明动作级恢复；
- 最大程度复用 V2；
- Graph path 可直接生成团队复盘报告；
- 编排正确性与模型优化能力可以分别验收；
- 以后可替换 Planner、RiskPolicy 或增加 Repair 子图。

缺点是需要将 `run_v2()` 和 `validate_candidate()` 中的阶段动作抽成窄服务。

### 3.3 方案 C：自由 LLM Supervisor/工具搜索

LLM 自主选择 Candidate 分支、请求工具和停止，Harness 只做审批。该方案能力上限高，但同时改变编排、工具策略、模型调用次数和搜索空间，难以定位失败来源，也容易增加 Token 和 credits。

### 3.4 决策

V3-A 选择方案 B。

V3-A 不允许 LLM 任意调用 shell、Vitis 或互联网，也不允许 LLM批准预算、晋升 Candidate 或选择最终答案。Planner 只提出有界优化计划和 Patch。

## 4. 范围与非目标

V3-A 分成两个连续、各自可验收的 gate，避免同时改变“编排、证据、Planner 和验证政策”后无法定位回归。

### 4.1 V3-A0：Orchestration MVP

V3-A0 只证明编排机制：

- optimize 类型任务的一条端到端动作级 Graph；
- `STRICT_AUDIT_EQUIVALENCE` 验证策略；
- V2 compatibility adapter：单元素 strategy bundle、现有 deterministic/scripted provider、现有 score/stop/CoSim gate；
- baseline、Candidate、final 的完整动作链；
- durable checkpoint、Planner/Tool/Registry/package 恢复事务；
- Graph trace 和团队报告；
- V2 与 V3-A0 的 best/final/score/stop/calls/credits 等价；
- 至少一个 deterministic Patch 驱动的真实 Vitis 完整纵向闭环。

V3-A0 不启用新的风险路由，不让新 Evidence 改变决策。

### 4.2 V3-A1：Evidence/Planner Upgrade

只有 V3-A0 通过后才启用 V3-A1：

- 循环级结构化 Synth 证据；
- 公开 description/header 进入受审计 Planner Context；
- 受 Schema 约束的 Planner 接口；
- 1–3 个配套 strategy bundle；
- pre-LLM 与 post-proposal 两级重复阻断；
- Patch 安全校验与 Candidate 物化；
- 最小 RiskPolicy 与 risk-gated 实验模式；
- A1 feature/config gate 关闭时，A0 等价测试仍全部通过。

真实开放权重模型能否加速不作为 V3-A1 完成条件；该质量门属于 V3-B。

### 4.3 V3-A 明确不做

- baseline CSim/Synth/CoSim 失败后的自动 Repair；
- residual stream deadlock 专用修复；
- 多 Candidate beam search；
- 多 LLM Agent 对话；
- 自由互联网搜索；
- 长期通用 RAG；
- RL、offline RL 或 contextual bandit；
- Docker、hidden-like 和多模型最终比赛强化；
- 修改 external/reference harness；
- 默认替换现有 V2 CLI 或生产路径。

如果 baseline 不满足 V3-A 的 optimize 入口条件，流程必须显式终止为 `REPAIR_REQUIRED` 或 `BASELINE_UNSYNTHESIZABLE`，不得在 V3-A 内隐式进入未设计的 Repair 循环。

## 5. 权威数据边界

| 数据 | 唯一权威来源 | Graph State 中保存 |
|---|---|---|
| kernel、Patch、Prompt、日志、报告 | Artifact Store | ref、hash、action ID |
| Candidate 关系、状态、best/final/fallback | Candidate Registry | Candidate ID |
| actual credits、Token、工具次数、pending action reservation | Budget Ledger | 最新完整 snapshot 和 ledger sequence |
| final validation reserve、预计 closure cost | BudgetPolicy + run config | 当前 RouteDecision、policy/config hash |
| 当前阶段、轮次和路由控制 | LangGraph checkpoint | 小型结构化字段 |
| Vitis、LLM、Manager、Policy 实例 | Runtime Context | 不进入 checkpoint |

禁止出现两份真相：

- State 不保存 `current_code` 或 `best_code`；
- State 不保存不断增长的完整 history；
- State 不单独维护可由 Ledger 推导的 `remaining_credit`；final reserve 也不能伪装成 Ledger 已实际消费或 pending 的 credits；
- Checkpointer 不替代 Ledger、Registry 或 Artifact Store；
- 恢复时以 Ledger/Registry/Artifact 的持久化事实重新 hydrate 并核对 checkpoint。

## 6. 总体组件

### 6.1 V2 复用服务

V3-A 复用并通过窄接口调用：

- `PublicTask`；
- `BudgetLedger`；
- `CandidateManager`；
- `ToolServer`；
- Patch parser/validator/applier；
- score/comparator；
- artifact/trace/review 基础设施。

### 6.2 V3-A 新增 Policy 与编排

V3-A 新增：

- `RunCoordinator`：创建 BootstrapState、绑定外部 checkpointer/control 路径，并处理
  唯一的 Graph 外不可恢复错误；
- `GraphSupervisor`：StateGraph、节点和纯条件路由；
- `EvidenceExtractor`：形成与 Candidate/报告绑定的结构化证据；
- `OptimizationPlanner`：一次调用返回计划与 Patch；
- `BudgetPolicy`：计算完整闭环可承担性和最终 reserve；
- `RiskPolicy`：根据任务、Patch diff、策略和历史生成风险决策；
- `ValidationProfilePolicy`：决定 baseline、exploration 和 final 所需验证深度；
- `TerminationPolicy`：硬停止、软停止和重复动作阻断；
- `CandidateDecisionTransaction`：为 promote/reject/select-final 提供 operation ID、Registry revision/CAS 和提交结果；
- `RouteDecision`：由无工具副作用的 decision node 计算并写入小型 State；conditional edge 只读取 `route_key`；
- `PlannerActionJournal`：补齐 LLM STARTED/result/COMPLETED reconcile，不能假定 V2 pending Planner 已可恢复；
- `LedgerStartTimeAdapter`：让 V3 Ledger 从 Bootstrap run start epoch 计时，而不是
  从初始化节点重新起算；
- durable LangGraph checkpointer adapter。

### 6.3 机制与 Policy 的职责分离

```text
BudgetPolicy 决定动作是否可承担
BudgetLedger 负责权威记账

Tool Router/Graph route 决定下一动作
ToolServer 负责执行指定工具

OptimizationPlanner 提出假设和 Patch
Patch Validator 决定提议是否合法

RiskPolicy 决定所需验证深度
CSim/Synth/CoSim 提供客观事实

Comparator 决定 Candidate 是否更优
CandidateDecisionTransaction 负责原子晋升或拒绝
CandidateManager 继续负责 immutable materialization 和 Registry 基础能力
```

`CandidateDecisionTransaction`、`PlannerActionJournal` 和 package commit protocol 是 V3-A 新增事务，不能描述成“原样复用 V2”。

## 7. V3-A 动作级 Graph

### 7.1 主路径

```text
START
  -> load_public_task
  -> initialize_ledger
  -> preflight_toolchain
  -> create_baseline
  -> evaluate_baseline_budget
       -> record_failure       [baseline + final closure不可承担]
       -> baseline_csim        [可承担]
  -> baseline_synth
  -> evaluate_baseline_cosim
       -> baseline_cosim       [V3-A全部profile要求]
  -> extract_best_evidence
  -> evaluate_continue
       -> plan_patch           [可继续]
       -> select_final_attempt [应停止]
  -> plan_patch
       -> validate_proposal         [返回 proposal]
       -> record_rejected_proposal  [Provider 正常拒绝/无提议]
  -> validate_proposal
       -> repair_proposal_format [仅一次格式修复LLM action]
       -> record_rejected_proposal [越权/非法/重复]
       -> classify_proposal_risk  [合法]
  -> repair_proposal_format
  -> validate_proposal
  -> classify_proposal_risk
  -> evaluate_candidate_closure_budget
       -> record_rejected_proposal [必要验证不可承担]
       -> materialize_candidate    [可承担]
  -> candidate_csim
  -> candidate_synth
  -> extract_candidate_evidence
  -> preliminary_score
  -> evaluate_candidate_cosim
       -> candidate_cosim     [需要且值得]
       -> record_provisional_candidate [A1允许跳过，仅进入shadow记录]
       -> reject_candidate    [无改善，不值得]
  -> decide_candidate
       -> promote_candidate
       -> reject_candidate
  -> restore_or_advance_best_working_set
       -> 复用 incumbent evidence [拒绝]
       -> 使用 Candidate evidence [晋升]
  -> evaluate_continue
       -> plan_patch          [继续]
       -> select_final_attempt[停止]
  -> select_final_attempt
  -> final_csim
  -> final_synth
  -> final_cosim
  -> commit_final_candidate
  -> stage_package
  -> commit_package
  -> DONE

任一 final stage 代码失败
  -> evaluate_final_fallback
       -> select_fallback_attempt -> final_csim [可承担fallback]
       -> record_failure -> FAILED               [无fallback]

任一可重试 LLM/Vitis action TOOL_ERROR
  -> evaluate_infra_retry
       -> 原 action node [首次、预算允许；使用新 attempt action ID]
       -> record_failure  [再次失败/预算不足/AMBIGUOUS]

record_rejected_proposal/reject_candidate
  -> restore_or_advance_best_working_set
  -> evaluate_continue

record_provisional_candidate
  -> restore_or_advance_best_working_set [恢复权威 CoSim-verified incumbent]
  -> evaluate_continue

record_failure
  -> FAILED
```

所有可结构化的 Graph 路径都必须终止于 `DONE` 或 `FAILED`；只有
`record_failure` 自身无法持久化时才允许 Graph 外不可恢复异常。所有优化循环经过
`evaluate_continue`；所有 final fallback 循环经过 `evaluate_final_fallback`。
`final_candidate_id` 只能由 `commit_final_candidate` 在完整 final
CSim/Synth/CoSim PASS 后写入。

### 7.2 Fresh start 与恢复入口

Fresh run 由 RunCoordinator 只提供最小 `BootstrapState`：run/config/version
身份、一个只读 `task_source_ref`、`run_start_epoch_seconds` 和独立
`preflight_timeout_seconds`。此时没有 `PublicTask`、Ledger snapshot、
Registry revision 或 Candidate working set。Runtime Context 初始只持有
`PublicTaskLoader/Repository` 和其他非 task-bound 服务；`load_public_task` 负责白名单
解析、输入边界检查、哈希并生成 immutable `public_task_ref`。`initialize_ledger`
完成后才能出现权威 budget snapshot，`create_baseline` 完成后才能建立 operational
State 和 `working`。

因此 Graph 使用“必填 Bootstrap 字段 + 分阶段可选 operational 字段”的 State
schema，并在每个生命周期边界执行 invariant assertion；不得为了满足类型声明而
伪造空 Budget、Registry 或 Candidate。

`load_public_task` 的 artifact key 由 run/config identity 与 task-source digest
确定；若 artifact 已存在则校验后复用。task boundary 在 Ledger 初始化前失败时，
`record_failure` 使用 bootstrap failure schema，只写稳定 run-level failure artifact，
不假定 Ledger/Registry/Candidate 已存在。Ledger 初始化后的 preflight 和后续失败
使用完整 operational failure transaction。

`initialize_ledger` 必须继承 immutable `run_start_epoch_seconds`，不能以 Ledger
创建时刻重新起算 runtime。Graph 顺序为 load → Ledger init → preflight；preflight
不扣 tool credits，但其实际 timeout 必须为：

```text
effective_preflight_timeout =
    min(preflight_timeout_seconds, ledger.runtime_remaining_seconds)
```

Ledger 初始化后若 `runtime_remaining_seconds <= 0`，不启动 preflight，直接进入
`FAILED(RUNTIME_LIMIT_REACHED_BEFORE_PREFLIGHT)`。effective timeout 非正数同样拒绝；
preflight 结束或被强制终止后刷新 Ledger runtime snapshot。这样 task load 与
preflight 的时间都计入全局 runtime hard limit，独立 timeout 不能越过它。

跨进程恢复不重新创建 run，而由 RunCoordinator 在恢复 Graph pending task前执行：

```text
inspect_persistent_run          [只读]
  -> reconcile_pending_action  [仅在需要时，一次 Ledger/operation 事务]
  -> hydrate_committed_state   [只读]
  -> resume LangGraph pending node
```

`reconcile_pending_action` 只能完成一种持久化事务。若同时存在多个 pending operation，按 Ledger/operation sequence 逐个重新进入该节点。它不能和只读 hydrate 合并。

### 7.3 完整节点清单与副作用边界

| 节点 | 主要职责 | 允许的单一副作用 |
|---|---|---|
| `load_public_task` | 从 `task_source_ref` 构造公开 typed task snapshot | 一个 immutable public-task artifact |
| `initialize_ledger` | 继承 run start epoch，创建并绑定 budget config | 一次 Ledger 初始化事务 |
| `preflight_toolchain` | 在独立 timeout 内检查 Vitis/version/part/clock，并刷新 runtime snapshot | 一次 preflight process |
| `create_baseline` | 创建 immutable baseline | 一次 Candidate 事务 |
| `baseline_csim` | 验证 baseline C 功能 | 一次 CSim action |
| `baseline_synth` | 综合 baseline 并产生报告 | 一次 Synth action |
| `baseline_cosim` | 完成所需 baseline RTL 验证 | 一次 CoSim action |
| `extract_best_evidence` | 从 Synth Manifest 的稳定 refs 解析 baseline/best Evidence | 一个 evidence artifact |
| `plan_patch` | 生成版本化 PlannerOutcome（proposal 或正常 rejection） | 一次 LLM action |
| `repair_proposal_format` | 仅修复返回 Schema | 一次 LLM action |
| `validate_proposal` | Schema、路径、接口、diff、dry-run | 无 Candidate 分配 |
| `classify_proposal_risk` | 绑定 proposal digest/patched code hash 计算风险 | 一个 risk artifact |
| `materialize_candidate` | 从 immutable parent 创建 Candidate | 一次 Candidate 事务 |
| `candidate_csim` | 验证 Candidate C 功能 | 一次 CSim action |
| `candidate_synth` | 验证综合、clock、资源和指标 | 一次 Synth action |
| `extract_candidate_evidence` | 从 Candidate Synth Manifest/canonical ref 解析 Evidence | 一个 evidence artifact |
| `candidate_cosim` | 验证高风险/required Candidate RTL | 一次 CoSim action |
| `preliminary_score` | 计算 CoSim 前的 Reference/public-validation 代理分 | 一个 score artifact |
| `record_rejected_proposal` | 记录未物化提议和 reason | 一次 proposal result 事务 |
| `record_provisional_candidate` | 记录 synth-verified shadow Candidate，不改权威 best/fallback/final | 一次 provisional decision operation |
| `promote_candidate` | CAS 更新 local best | 一次 decision operation |
| `reject_candidate` | 记录拒绝和失败摘要 | 一次 decision operation |
| `restore_or_advance_best_working_set` | 原子切换 Candidate-bound working set | State update，无外部副作用 |
| `select_final_attempt` | 选择待最终验证 Candidate，并新建全 NOT_RUN 的 final scope validation | 一次 selection operation |
| `select_fallback_attempt` | 切换 fallback，并重建独立 final scope validation | 一次 selection operation |
| `final_csim` | 只更新独立 final validation 的 CSim | 一次 CSim action |
| `final_synth` | 只更新独立 final validation 的 Synth | 一次 Synth action |
| `final_cosim` | 只更新独立 final validation 的 CoSim | 一次 CoSim action |
| `commit_final_candidate` | 同一 final scope 三阶段全 PASS 后写 final ID | 一次 CAS operation |
| `stage_package` | 以 final-commit checkpoint 为 cutoff 生成 trace snapshot、Manifest/报告 | 一次 staging operation |
| `commit_package` | 校验并原子发布 package，再写 hash-linked marker | 一次 package commit operation |
| `record_failure` | 保存明确失败终态和最小报告 | 一次 failure operation |

`inspect_persistent_run`、`reconcile_pending_action` 和 `hydrate_committed_state` 是
RunCoordinator 的 Graph 外恢复操作，不是 StateGraph 节点，因此不出现在 transition
table，也不会制造动态 Graph edge。Coordinator 只在调用 `graph.invoke/resume` 前按
7.2 的固定顺序执行它们；完成 hydrate 后，由 checkpointer 的既有 pending task
恢复到已注册节点。它们的只读/单事务副作用边界仍受 17.2 恢复契约约束。

以下 decision nodes 调用纯 Policy，只返回小型 `RouteDecision`，不读写外部文件：

- `evaluate_baseline_budget`；
- `evaluate_baseline_cosim`；
- `evaluate_candidate_closure_budget`；
- `evaluate_candidate_cosim`；
- `decide_candidate`；
- `evaluate_continue`；
- `evaluate_final_fallback`；
- `evaluate_infra_retry`。

LangGraph checkpointer 保存这些节点返回的 `RouteDecision`。conditional edge 只读取 `route_key` 映射下一节点；报告从 checkpoint history 生成 edge trace。因此 route 本身保持纯函数，不需要 `RouteDecisionWriter` 在 edge 中写文件。

纯 route、Policy 和 Comparator 不得调用 LLM、Vitis、修改文件或直接扣费。

### 7.4 Phase 映射

| Phase | 节点范围 |
|---|---|
| `BOOTSTRAP` | load、preflight、Ledger、baseline 创建和启动预算检查 |
| `VALIDATE` | baseline validation，以及 final/fallback validation |
| `OPTIMIZE` | Evidence、Planner、proposal、Candidate validation、比较和循环 |
| `FINALIZE` | final attempt selection、final commit 和 package |
| `DONE` | package commit 完成 |
| `FAILED` | failure operation 完成 |

Baseline 是 immutable Candidate 身份，不单独作为 Phase。每个节点进入时显式写入或断言所属 Phase，禁止把阶段隐藏在 mutable service 内。

## 8. Edge 与路由条件

### 8.1 完整 transition table

| 当前节点/事件 | route key | 下一节点 | reason code |
|---|---|---|---|
| `load_public_task` success | `TASK_READY` | `initialize_ledger` | `PUBLIC_TASK_BOUND` |
| task input violation | `FAIL` | `record_failure` | `PUBLIC_INPUT_BOUNDARY_FAIL` |
| `initialize_ledger` committed 且 runtime remaining > 0 | `LEDGER_READY` | `preflight_toolchain` | `LEDGER_INITIALIZED` |
| Ledger init 时 runtime 已耗尽 | `FAIL` | `record_failure` | `RUNTIME_LIMIT_REACHED_BEFORE_PREFLIGHT` |
| `preflight_toolchain` PASS | `PREFLIGHT_PASS` | `create_baseline` | `TOOLCHAIN_READY` |
| preflight fail/独立 timeout | `FAIL` | `record_failure` | `INFRA_PREFLIGHT_FAIL` / `INFRA_PREFLIGHT_TIMEOUT` |
| preflight 触达全局 runtime effective timeout | `FAIL` | `record_failure` | `RUNTIME_LIMIT_REACHED` |
| `create_baseline` committed | `BASELINE_READY` | `evaluate_baseline_budget` | `BASELINE_IMMUTABLE` |
| baseline + final closure affordable | `RUN_BASELINE` | `baseline_csim` | `BASELINE_CLOSURE_AFFORDABLE` |
| baseline + final closure unaffordable | `FAIL` | `record_failure` | `INSUFFICIENT_BASELINE_FINAL_BUDGET` |
| baseline CSim PASS | `RUN_SYNTH` | `baseline_synth` | `BASELINE_CSIM_PASS` |
| baseline CSim code FAIL | `FAIL` | `record_failure` | `REPAIR_REQUIRED` |
| baseline CSim TIMEOUT | `FAIL` | `record_failure` | `BASELINE_CSIM_TIMEOUT` |
| baseline Synth PASS 且 clock/resource 合法 | `CHECK_COSIM` | `evaluate_baseline_cosim` | `BASELINE_SYNTH_PASS` |
| baseline Synth PASS 但 clock/resource 非法 | `FAIL` | `record_failure` | `BASELINE_HARD_CONSTRAINT_FAIL` |
| baseline Synth code FAIL | `FAIL` | `record_failure` | `BASELINE_UNSYNTHESIZABLE` |
| baseline Synth TIMEOUT | `FAIL` | `record_failure` | `BASELINE_SYNTH_TIMEOUT` |
| 任一 V3-A release profile baseline | `RUN_COSIM` | `baseline_cosim` | `STRICT_BASELINE_COSIM_REQUIRED` |
| baseline CoSim PASS | `EXTRACT_EVIDENCE` | `extract_best_evidence` | `BASELINE_COSIM_PASS` |
| baseline CoSim code FAIL/deadlock | `FAIL` | `record_failure` | `STRUCTURAL_REPAIR_REQUIRED` |
| baseline CoSim TIMEOUT | `FAIL` | `record_failure` | `BASELINE_COSIM_TIMEOUT` |
| best evidence ready | `CHECK_CONTINUE` | `evaluate_continue` | `BEST_EVIDENCE_READY` |
| round affordable and new hypothesis exists | `PLAN` | `plan_patch` | `ROUND_AFFORDABLE` |
| final reserve protected / stop condition | `FINALIZE` | `select_final_attempt` | `FINAL_RESERVE_PROTECTED` 或规范化 StopPolicy code |
| `plan_patch` schema candidate returned | `VALIDATE_PROPOSAL` | `validate_proposal` | `PLAN_RESULT_READY` |
| `plan_patch` completed provider rejection/abstention | `REJECT_PROPOSAL` | `record_rejected_proposal` | `PROVIDER_REJECTED` |
| format-only error, retry unused/affordable | `REPAIR_FORMAT` | `repair_proposal_format` | `FORMAT_RETRY_ONCE` |
| repaired format returned | `VALIDATE_PROPOSAL` | `validate_proposal` | `FORMAT_RETRY_RESULT_READY` |
| format retry unavailable/failed | `REJECT_PROPOSAL` | `record_rejected_proposal` | `PROPOSAL_SCHEMA_INVALID` |
| proposal 越权/非法路径/接口变化 | `REJECT_PROPOSAL` | `record_rejected_proposal` | `PATCH_FORBIDDEN_PATH` / `PATCH_INTERFACE_CHANGE` / `PATCH_POLICY_REJECTED` |
| exact post-proposal duplicate | `REJECT_PROPOSAL` | `record_rejected_proposal` | `DUPLICATE_PROPOSAL` |
| proposal valid + dry-run PASS | `CLASSIFY_RISK` | `classify_proposal_risk` | `PROPOSAL_VALID` |
| risk artifact ready | `CHECK_CLOSURE` | `evaluate_candidate_closure_budget` | `PROPOSAL_RISK_BOUND` |
| required candidate closure affordable | `MATERIALIZE` | `materialize_candidate` | `CANDIDATE_CLOSURE_AFFORDABLE` |
| required closure unaffordable | `REJECT_PROPOSAL` | `record_rejected_proposal` | `CANDIDATE_CLOSURE_UNAFFORDABLE` |
| proposal rejection committed | `RESTORE_BEST` | `restore_or_advance_best_working_set` | `PROPOSAL_REJECTED` |
| Candidate materialized | `RUN_CSIM` | `candidate_csim` | `CANDIDATE_MATERIALIZED` |
| Candidate CSim PASS | `RUN_SYNTH` | `candidate_synth` | `CANDIDATE_CSIM_PASS` |
| Candidate CSim code FAIL | `REJECT_CANDIDATE` | `reject_candidate` | `CANDIDATE_CSIM_FAIL` |
| Candidate CSim TIMEOUT | `REJECT_CANDIDATE` | `reject_candidate` | `CANDIDATE_CSIM_TIMEOUT` |
| Candidate Synth PASS + hard constraints PASS | `EXTRACT_EVIDENCE` | `extract_candidate_evidence` | `CANDIDATE_SYNTH_PASS` |
| Candidate Synth code/hard constraint FAIL | `REJECT_CANDIDATE` | `reject_candidate` | `CANDIDATE_SYNTH_OR_CONSTRAINT_FAIL` |
| Candidate Synth TIMEOUT | `REJECT_CANDIDATE` | `reject_candidate` | `CANDIDATE_SYNTH_TIMEOUT` |
| Candidate evidence ready | `SCORE_PRELIMINARY` | `preliminary_score` | `CANDIDATE_EVIDENCE_READY` |
| preliminary score artifact ready | `EVALUATE_COSIM` | `evaluate_candidate_cosim` | `PRELIMINARY_SCORE_READY` |
| `evaluate_candidate_cosim`: not strictly better | `REJECT_CANDIDATE` | `reject_candidate` | `NO_STRICT_IMPROVEMENT` |
| `evaluate_candidate_cosim`: A0 V2-equivalence gain gate PASS | `RUN_COSIM` | `candidate_cosim` | `V2_EQUIVALENCE_COSIM_REQUIRED` |
| `evaluate_candidate_cosim`: A1 risk gate says CoSim required | `RUN_COSIM` | `candidate_cosim` | `RISK_COSIM_REQUIRED` |
| `evaluate_candidate_cosim`: A1 risk gate allows skip | `RECORD_PROVISIONAL` | `record_provisional_candidate` | `RISK_COSIM_DEFERRED` |
| Candidate CoSim PASS | `COMPARE` | `decide_candidate` | `CANDIDATE_COSIM_PASS` |
| Candidate CoSim FAIL/deadlock/timeout | `REJECT_CANDIDATE` | `reject_candidate` | `CANDIDATE_COSIM_FAIL` / `CANDIDATE_COSIM_DEADLOCK` / `CANDIDATE_COSIM_TIMEOUT` |
| Comparator strictly better | `PROMOTE` | `promote_candidate` | `STRICTLY_BETTER` |
| Comparator not better | `REJECT_CANDIDATE` | `reject_candidate` | `COMPARATOR_REJECTED` |
| promote committed | `ADVANCE_BEST` | `restore_or_advance_best_working_set` | `CANDIDATE_PROMOTED` |
| reject committed | `RESTORE_BEST` | `restore_or_advance_best_working_set` | `CANDIDATE_REJECTED` |
| provisional shadow record committed | `RESTORE_BEST` | `restore_or_advance_best_working_set` | `PROVISIONAL_CANDIDATE_RECORDED` |
| working set switched | `CHECK_CONTINUE` | `evaluate_continue` | `WORKING_SET_BOUND` |
| final attempt selected | `FINAL_CSIM` | `final_csim` | `FINAL_ATTEMPT_SELECTED` |
| final CSim PASS | `FINAL_SYNTH` | `final_synth` | `FINAL_CSIM_PASS` |
| final Synth PASS + hard constraints PASS | `FINAL_COSIM` | `final_cosim` | `FINAL_SYNTH_PASS` |
| final CoSim PASS | `COMMIT_FINAL` | `commit_final_candidate` | `FINAL_COSIM_PASS` |
| 任一 final code FAIL | `CHECK_FALLBACK` | `evaluate_final_fallback` | `FINAL_CSIM_FAIL` / `FINAL_SYNTH_FAIL` / `FINAL_COSIM_FAIL` / `FINAL_HARD_CONSTRAINT_FAIL` |
| 任一 final stage TIMEOUT | `CHECK_FALLBACK` | `evaluate_final_fallback` | `FINAL_CSIM_TIMEOUT` / `FINAL_SYNTH_TIMEOUT` / `FINAL_COSIM_TIMEOUT` |
| affordable fallback exists | `SELECT_FALLBACK` | `select_fallback_attempt` | `FALLBACK_AFFORDABLE` |
| fallback selected | `FINAL_CSIM` | `final_csim` | `FALLBACK_ATTEMPT_SELECTED` |
| no fallback/attempt/budget | `FAIL` | `record_failure` | `NO_VALID_CANDIDATE` |
| 任一可重试 LLM/Vitis action 返回 `TOOL_ERROR` | `CHECK_INFRA_RETRY` | `evaluate_infra_retry` | `RETRYABLE_INFRA_ERROR` |
| 任一 non-retryable LLM/Vitis `TOOL_ERROR` | `FAIL` | `record_failure` | `NON_RETRYABLE_INFRA_ERROR` |
| retry eligible，origin=`plan_patch` | `RETRY_PLAN_PATCH` | `plan_patch` | `INFRA_RETRY_ALLOWED` |
| retry eligible，origin=`repair_proposal_format` | `RETRY_FORMAT_REPAIR` | `repair_proposal_format` | `INFRA_RETRY_ALLOWED` |
| retry eligible，origin=`baseline_csim` | `RETRY_BASELINE_CSIM` | `baseline_csim` | `INFRA_RETRY_ALLOWED` |
| retry eligible，origin=`baseline_synth` | `RETRY_BASELINE_SYNTH` | `baseline_synth` | `INFRA_RETRY_ALLOWED` |
| retry eligible，origin=`baseline_cosim` | `RETRY_BASELINE_COSIM` | `baseline_cosim` | `INFRA_RETRY_ALLOWED` |
| retry eligible，origin=`candidate_csim` | `RETRY_CANDIDATE_CSIM` | `candidate_csim` | `INFRA_RETRY_ALLOWED` |
| retry eligible，origin=`candidate_synth` | `RETRY_CANDIDATE_SYNTH` | `candidate_synth` | `INFRA_RETRY_ALLOWED` |
| retry eligible，origin=`candidate_cosim` | `RETRY_CANDIDATE_COSIM` | `candidate_cosim` | `INFRA_RETRY_ALLOWED` |
| retry eligible，origin=`final_csim` | `RETRY_FINAL_CSIM` | `final_csim` | `INFRA_RETRY_ALLOWED` |
| retry eligible，origin=`final_synth` | `RETRY_FINAL_SYNTH` | `final_synth` | `INFRA_RETRY_ALLOWED` |
| retry eligible，origin=`final_cosim` | `RETRY_FINAL_COSIM` | `final_cosim` | `INFRA_RETRY_ALLOWED` |
| retry denied/第二次失败 | `FAIL` | `record_failure` | `INFRA_RETRY_BUDGET_DENIED` / `INFRA_RETRY_EXHAUSTED` |
| action `AMBIGUOUS` | `FAIL` | `record_failure` | `AMBIGUOUS_ACTION` |
| action node 返回缺失/`NOT_RUN` terminal stage | `FAIL` | `record_failure` | `STATE_INTEGRITY_ERROR` |
| 已批准 closure 内的后继 action 仍被 Budget 拒绝 | `FAIL` | `record_failure` | `BUDGET_CLOSURE_INVARIANT_BROKEN` |
| `initialize_ledger` / `create_baseline` typed operation failure | `FAIL` | `record_failure` | `BOOTSTRAP_OPERATION_ERROR` |
| Evidence/Risk/Score/Policy schema、parse、hash binding failure | `FAIL` | `record_failure` | `STATE_INTEGRITY_ERROR` |
| materialize/promote/reject/provisional/select/commit-final CAS 或 operation failure | `FAIL` | `record_failure` | `REGISTRY_OPERATION_ERROR` / `REGISTRY_CAS_CONFLICT` |
| 任一非工具节点未捕获但可结构化的 operation error | `FAIL` | `record_failure` | `ORCHESTRATION_OPERATION_ERROR` |
| final committed | `STAGE_PACKAGE` | `stage_package` | `FINAL_CANDIDATE_COMMITTED` |
| package staged | `COMMIT_PACKAGE` | `commit_package` | `PACKAGE_STAGED` |
| package committed | `DONE` | `DONE` | `PACKAGE_COMMITTED` |
| package permanent failure | `FAIL` | `record_failure` | `PACKAGE_ERROR` |
| failure operation committed | `FAILED` | `FAILED` | 已持久化的规范化 failure code |

`record_failure` 是 Graph 内最后一道可持久化边界。只有它自身因不可恢复的磁盘、
权限或 checkpointer 故障而无法写入 failure operation 时，RunCoordinator 才允许在
Graph 外抛出 `UnrecoverableOrchestrationError`；该异常必须携带 run ID、最后可信
checkpoint/ledger sequence 和原始错误，不能伪装成 `FAILED`。除这个“连失败事实都
无法持久化”的外部故障外，所有 typed 路径必须进入 `DONE` 或 `FAILED`。

### 8.2 工具与 Planner 重试

Checkpoint replay 与主动重试必须区分：

- checkpoint replay：复用同一个 action ID；若可信结果已落盘，返回缓存且不再次执行、不再次收费；
- `STARTED + result exists`：同一 action ID 只做 reconcile，不执行工具；
- `STARTED + no result`：标记 `AMBIGUOUS` 并保守核销，不按普通 TOOL_ERROR 自动重跑；
- 已完成的可重试 `TOOL_ERROR`：若 Policy 允许，用 canonical payload 的 SHA-256（包含 `logical_operation_id + attempt_index=1`）生成新 action ID，记录 `retry_of`，并按新调用计费；
- 第二次工具错误：进入 `record_failure(INFRA_ERROR)`；
- Planner provider 错误使用同样的 attempt/reconcile 语义；format repair 属于另一种受预算控制的 LLM action，不是免费重试。

Provider 正常完成但明确拒绝、abstain 或返回“无安全提议”不是 `TOOL_ERROR`。
V2 compatibility adapter 将其原样记录为 `PROVIDER_REJECTED`，增加一次
no-improvement，随后通过 `record_rejected_proposal -> evaluate_continue` 继续或按
V2 stop policy 停止；该次 LLM call/Token 正常计费，不触发基础设施 retry。

主动 retry 不创建一个动态或自由工具节点。失败 action 写入小型 `RetryState`，
`evaluate_infra_retry` 只可返回 transition table 枚举的有限 `RETRY_*` route key，
随后重新进入对应原 action node。该节点读取 `attempt_index=1` 生成新 action ID；
成功后原子清空 `retry`，再次 `TOOL_ERROR` 或任何 `AMBIGUOUS` 都进入明确失败。
静态 Graph 构建测试必须验证每个 `RETRY_*` key 都映射到唯一已注册节点。

Proposal 在完整校验、dry-run、风险分类和 closure budget gate 前不得分配 Candidate ID。

### 8.3 Candidate 决策与停止

Candidate 比较顺序保持确定性：

```text
当前 validation profile 的必要验证合格性
  > 正确性与硬约束
  > Reference/public-validation proxy
  > PPA tie-break
  > 验证成本
  > 稳定 Candidate ID
```

严格优于 local best 才能晋升。拒绝 Candidate 不会停止整个任务；只要存在不同合法假设且预算/轮数/时间允许，下一轮继续。
`record_provisional_candidate` 保存 shadow PPA 结果，但由于权威 best 未变化，按一次
authoritative no-improvement 计数；报告必须单列“发现了 provisional gain”和“没有
发生 correctness-tier 合法晋升”。

硬停止包括：

- 无法承担下一轮 Planner + 必要验证闭环；
- 会侵占最终 reserve；
- Token、credits、tool call 或 wall time 达到上限；
- 最大优化 Candidate 数达到上限；
- 没有不同且合法的新动作；
- 已达到配置的性能上界。

软停止包括：

- 连续两个不同假设没有改善；
- Planner 重复相同 strategy bundle、metrics digest 或 Patch；
- 瓶颈证据没有变化且没有新假设；
- 预计收益不足以覆盖验证风险和成本。

规范化 StopPolicy code 只能从以下集合选择：

```text
FINAL_RESERVE_PROTECTED
MAX_OPTIMIZATION_ROUNDS
MAX_NO_IMPROVEMENT
TOKEN_LIMIT_REACHED
CREDIT_LIMIT_REACHED
TOOL_LIMIT_REACHED
WALL_TIME_LIMIT_REACHED
NO_NEW_HYPOTHESIS
PERFORMANCE_CAP_REACHED
```

上述集合是 Graph/报告使用的 normalized reason，不覆盖 V2 compatibility 原值。
V3-A0 同时保存 `exploration_stop_reason_raw`、`terminal_stop_reason_raw` 和
`normalized_stop_reason`。等价测试比较前两个 raw 字段；纯 route 使用 normalized
字段。最小兼容映射为：

| V2 raw reason | Graph normalized reason |
|---|---|
| `MAX_OPTIMIZATION_ROUNDS` | `MAX_OPTIMIZATION_ROUNDS` |
| `NO_IMPROVEMENT_LIMIT` | `MAX_NO_IMPROVEMENT` |
| `FINAL_RESERVE_REACHED` | `FINAL_RESERVE_PROTECTED` |
| `TOKEN_RESERVE_REACHED` | `TOKEN_LIMIT_REACHED` |
| `NO_DISTINCT_OPTIMIZATION` | `NO_NEW_HYPOTHESIS` |

Selector 返回的其他 V2 raw stop reason 必须原样保留，并通过版本化 adapter 映射到
上述 normalized 集合；未知值 fail closed，不能悄悄改名。final 成功、fallback 和
失败仍按 V2 产生 `terminal_stop_reason_raw`（例如 `FALLBACK_VERIFIED` 或
`FINAL_VALIDATION_FAILED`）。

## 9. 最小 State Schema

以下为字段契约，不要求实现逐字采用此 Python 形式：

```python
Phase = Literal[
    "BOOTSTRAP",
    "VALIDATE",
    "OPTIMIZE",
    "FINALIZE",
    "DONE",
    "FAILED",
]

ValidationProfile = Literal[
    "STRICT_AUDIT_EQUIVALENCE",
    "STRICT_AUDIT_RISK_GATED",
]

ValidationScope = Literal["baseline", "exploration", "final"]

class StageResult(TypedDict, total=False):
    status: Literal[
        "NOT_RUN", "PASS", "FAIL", "TIMEOUT", "TOOL_ERROR", "AMBIGUOUS"
    ]
    validation_scope: ValidationScope
    action_id: str
    result_ref: str
    code_hash: str
    tool_config_hash: str

class CandidateValidationState(TypedDict, total=False):
    candidate_id: str
    code_hash: str
    validation_scope: ValidationScope
    csim: StageResult
    synth: StageResult
    cosim: StageResult

class CandidateWorkingSet(TypedDict):
    candidate_id: str
    code_hash: str
    validation: CandidateValidationState
    evidence_ref: str | None
    plan_ref: str | None
    risk_ref: str | None
    score_ref: str | None

class PlannerOutcomeState(TypedDict, total=False):
    schema_version: str
    planner_action_id: str
    outcome_ref: str
    outcome_hash: str
    status: Literal["PROPOSAL", "REJECTED"]
    plan_ref: str
    rejection_reason_code: str

class ProposalState(TypedDict):
    planner_action_id: str
    plan_ref: str
    proposal_digest: str
    patched_code_hash: str
    risk_ref: str | None

class BudgetDimensionCheck(TypedDict, total=False):
    dimension: Literal["credits", "tokens", "tool_calls", "runtime"]
    allowed: bool
    required: float
    available: float | None
    required_by_kind: dict[str, float]
    available_by_kind: dict[str, float | None]
    reason_code: str

class RouteDecisionCore(TypedDict):
    route_key: str
    reason_code: str
    evidence_refs: list[str]
    policy_bundle_hash: str

class RouteDecision(RouteDecisionCore, total=False):
    estimated_action_cost: int
    estimated_closure_cost: int
    final_reserve: int
    budget_snapshot_ref: str
    budget_checks: list[BudgetDimensionCheck]

class BudgetSnapshot(TypedDict):
    snapshot_ref: str
    ledger_sequence: int
    ledger_event_hash: str
    credit_limit: int | None
    credits_used: int
    pending_credits_reserved: int
    credits_remaining: int | None
    tool_costs: dict[str, int]
    tool_limits: dict[str, int | None]
    tool_used: dict[str, int]
    tool_pending: dict[str, int]
    token_limit: int
    tokens_used: int
    pending_tokens_reserved: int
    tokens_remaining: int
    input_tokens_used: int
    output_tokens_used: int
    cached_input_tokens_used: int
    token_usage_complete: bool
    runtime_limit_seconds: float
    runtime_used_seconds: float
    runtime_remaining_seconds: float
    config_hash: str

class RetryState(TypedDict):
    failed_node: str
    logical_operation_id: str
    failed_action_id: str
    action_kind: str
    attempt_index: int
    error_ref: str

class BootstrapState(TypedDict):
    state_schema_version: str
    graph_version: str
    checkpoint_namespace: str
    run_id: str
    task_source_ref: str
    run_start_epoch_seconds: float
    preflight_timeout_seconds: float
    run_config_hash: str
    optimization_config_hash: str
    tool_config_hash: str
    policy_bundle_hash: str
    phase: Phase
    validation_profile: ValidationProfile

# LangGraph 的实际 superset schema。除 BootstrapState 继承字段外，以下字段
# 在对应生命周期节点完成前不存在；节点入口 invariant 决定哪些字段必填。
class AgentState(BootstrapState, total=False):
    task_id: str
    task_fingerprint: str
    public_task_ref: str

    baseline_candidate_id: str
    best_candidate_id: str | None
    best_cosim_candidate_id: str | None
    provisional_best_candidate_id: str | None
    final_attempt_candidate_id: str | None
    final_candidate_id: str | None
    fallback_candidate_ids: list[str]

    working: CandidateWorkingSet
    final_validation: CandidateValidationState | None
    planner_outcome: PlannerOutcomeState | None
    proposal: ProposalState | None

    round_index: int
    proposal_retry_count: int
    no_improvement_count: int
    final_attempt_count: int

    budget: BudgetSnapshot
    retry: RetryState | None
    pending_action_id: str | None
    pending_operation_id: str | None
    registry_revision: int
    route_decision: RouteDecision | None
    exploration_stop_reason_raw: str | None
    terminal_stop_reason_raw: str | None
    normalized_stop_reason: str | None
    graph_trace_snapshot_ref: str | None
    graph_trace_snapshot_hash: str | None
    package_operation_id: str | None
    package_manifest_ref: str | None
    package_manifest_hash: str | None
    package_marker_ref: str | None
    package_marker_hash: str | None
```

`credit_limit` 或单项 `tool_limits` 中的 `None` 表示该维度未配置硬上限，但其他
维度仍必须检查。字段命名与现有 V2 Ledger snapshot 保持兼容；V3 只增加
`snapshot_ref/ledger_sequence/ledger_event_hash` 身份绑定。BudgetSnapshot 不是第二份
可独立修改的预算；Graph 节点只能用新 Ledger snapshot 整体替换它。

### 9.1 Working-set reducer

Reducer 必须原子绑定 Candidate 工作集：

- `create_baseline` 建立 scope=`baseline` 的 working validation；
  `materialize_candidate` 建立新的 scope=`exploration` validation；
- `candidate_id` 或 `code_hash` 变化时，整体替换 `working`；
- 同一 Candidate 更新单个 stage 时，只在验证绑定一致时合并该 stage；
- Candidate 被拒绝回到 incumbent 时，`validation/evidence_ref/plan_ref/risk_ref/score_ref` 一起替换；
- 不允许 Candidate A 的 Evidence/Risk/Score 与 Candidate B 的 Validation 组合；
- 每个已执行 StageResult 必须完整绑定 `validation_scope + action_id + code_hash + tool_config_hash`。

Final validation 使用独立 reducer：`select_final_attempt` 或
`select_fallback_attempt` 必须为所选 Candidate 原子创建 scope=`final`、三个 stage
全为 `NOT_RUN` 的新 `final_validation`。`final_csim/final_synth/final_cosim` 只能
更新该对象，不能读取或合并 `working.validation` 的 exploration/baseline PASS。
`commit_final_candidate` 必须断言同一 Candidate、同一 code hash、同一 tool config
下三个 scope=`final` 的 stage 全部 PASS。

`proposal` 在 Candidate 分配前独立存在；materialization 成功后，它被链接进新的 `working`，随后清空。`final_attempt_candidate_id` 表示正在接受独立最终验证的候选；fallback 只替换该字段并重建 `final_validation`，不污染 exploration working set。`final_candidate_id` 在完整 final CSim/Synth/CoSim PASS 前必须保持为空。

`route_decision` 只保存当前 decision node 的小型输出；完整 edge history 由 checkpointer 的状态版本记录，不在 State 中累积列表。

`stage_package` 成功后写入 graph-trace snapshot 与 Manifest ref/hash；
`commit_package` 成功后再写 `package_operation_id + package_marker_ref/hash`。进入
`DONE` 的 reducer 必须断言 marker 内容与 State 中的 Manifest/trace/final Candidate
绑定完全一致。State 只反向引用 marker，不尝试引用或哈希包含自身的 DONE
checkpoint。

### 9.2 不进入 State 的内容

- 完整 kernel；
- 完整 Patch；
- 完整 Prompt/response；
- public testbench；
- stdout/stderr 和 Vitis 报告全文；
- 不断增长的聊天历史或工具历史；
- Provider client、ToolServer 或 Manager 实例；
- hidden/reference 内容。

首个 durable checkpoint 在 `load_public_task` 成功、`task_fingerprint` 已产生后写入；
在此之前只有可重新构造的 Bootstrap invocation。后续 checkpoint identity 必须同时
匹配 `run_id + checkpoint_namespace + task_fingerprint + run/config/policy hashes +
state_schema_version`。任一不匹配都不得恢复旧 checkpoint。

## 10. Runtime Context

Runtime Context 保存不可序列化服务：

```text
PublicTaskLoader/Repository
BudgetLedger/BudgetPolicy
CandidateManager
ToolServer
EvidenceExtractor
OptimizationPlanner
PatchValidator/PatchApplier
RiskPolicy
ValidationProfilePolicy
CandidateComparator
TerminationPolicy
ArtifactStore/TraceWriter
```

初始 Context 不预装 `PublicTask`。`load_public_task` 通过 `task_source_ref` 调用
Loader，产生 immutable `public_task_ref`；后续节点通过 Repository 和该 ref 解析
只读 typed PublicTask。节点读取 State 中的 ID/ref，再通过 Context 服务访问权威
数据。Context 对象不写入 checkpoint。

## 11. Evidence Contract

### 11.1 Synth 结构化证据

`EvidenceExtractor` 至少产生：

```json
{
  "schema_version": "v3a.synth-evidence.v1",
  "candidate_id": "candidate_000",
  "code_hash": "...",
  "tool_config_hash": "...",
  "synth_action_id": "...",
  "raw_report_set_digest": "...",
  "parser_version": "v3a.vitis-synth-parser.v1",
  "source_reports": [
    {
      "kind": "vitis_csynth_xml_top",
      "ref": "tool-results/.../csynth.xml",
      "hash": "..."
    },
    {
      "kind": "vitis_loop_schedule_raw",
      "ref": "tool-results/.../loop-report.rpt",
      "hash": "..."
    }
  ],
  "top": {
    "latency_min": 1027,
    "latency_max": 1027,
    "transaction_interval_min": 1025,
    "transaction_interval_max": 1025,
    "estimated_clock_period_ns": 3.17
  },
  "loops": [
    {
      "name": "...",
      "trip_count": 1024,
      "pipeline_status": "PIPELINED",
      "achieved_ii": 1,
      "latency": 1025,
      "bottleneck_refs": []
    }
  ],
  "memory": [],
  "resources": {
    "lut": 0,
    "ff": 0,
    "dsp": 5,
    "bram": 0,
    "uram": 0
  }
}
```

示例中的资源值不是验收常量；关键要求是顶层 transaction interval 与循环 achieved II 分开建模。

Synth action 必须在 Vitis workspace 仍存在时，由 ToolServer/backend adapter 收集并
封存原始报告。为避免循环哈希，封存顺序和绑定方向固定为：

```text
raw reports + identity
  -> raw_report_set_digest
  -> canonical synth-evidence.v1（绑定 raw digest，不绑定 Manifest hash）
  -> final Synth Manifest（绑定 raw entries 和 canonical Evidence ref/hash）
```

Manifest 最后生成并至少绑定：

```text
synth action ID
candidate ID / code hash / tool config hash
Vitis version / part / clock
artifact kind / immutable ref / content hash
top csynth XML
loop/schedule raw report（A1 可用时）
canonical synth-evidence.v1 ref/hash
parser version
```

A0 compatibility 可以只消费 Manifest 中已有的 top-level artifact。A1 backend adapter
必须从明确列入 Manifest 的原始报告生成 canonical `synth-evidence.v1`；若该 Vitis
版本无法提供 loop report，必须把 loop 字段标成 `UNAVAILABLE`，不能伪造。
Synth StageResult 的 tool result 必须暴露最终 Manifest ref/hash。`EvidenceExtractor`
先验证 Manifest，再按 Manifest 读取 canonical Evidence，并重新计算 raw report set
digest；禁止在临时 `work/`、solution 目录或日志中猜路径。Manifest 中的 Evidence
hash、Evidence 中的 raw digest、raw entries 与 Synth action 任一 ID/hash 不一致都
属于 `STATE_INTEGRITY_ERROR`。Evidence 不得反向包含 Manifest hash。

### 11.2 V3-A Evidence Gate

dotProduct 的真实或精简报告测试必须证明：

```text
top transaction interval = 1025
loop achieved II = 1
trip count = 1024
```

Planner Context 必须能读取公开 header 中的 `NUM_FEATURES` 和 `PAR_FACTOR`。相同证据下，系统不得通过固定规则再次强制选择 `PIPELINE-only`。

若报告缺失循环级数据，Evidence 必须显式标记 `UNKNOWN/UNAVAILABLE`，不得用顶层 interval 伪造 loop II。

## 12. Planner Contract

### 12.1 输入

Planner 每轮接收有界上下文：

- objective；
- 当前 best kernel；
- 公开 description 和 fixed headers；
- interface、numeric、clock 和 resource 约束；
- 结构化 Evidence；
- 最近失败动作的压缩摘要；
- 已禁止的重复 strategy/Patch digest；
- 剩余可探索 Token、credits、轮数；
- Patch 路径、接口和修改行数 Policy。

任务配置只通过 typed/whitelisted 字段进入 Planner；不发送 raw TOML、完整聊天历史、完整工具日志、public TB、build scripts、hidden/reference 或无关工程文件。

### 12.2 输出

每个正常完成的 Planner action 都必须原子持久化版本化
`v3a.planner-outcome.v1`，并把 `status + planner_action_id + outcome_ref/hash` 写入
`PlannerOutcomeState`。纯 conditional edge 只读取该 status。

有提议时 outcome 为：

```json
{
  "schema_version": "v3a.planner-outcome.v1",
  "planner_action_id": "...",
  "status": "PROPOSAL",
  "provider_binding": {"provider": "...", "model": "...", "revision": "..."},
  "proposal": {
    "schema_version": "v3a.plan.v1",
    "hypothesis": "serial reduction limits total latency",
    "primary_bottleneck": "REDUCTION",
    "evidence_refs": ["evidence://..."],
    "strategy_bundle": [
      "MEMORY_PARTITION",
      "LOOP_UNROLL",
      "PARALLEL_REDUCTION"
    ],
    "expected_effect": "reduce 1024 serial accumulations",
    "risk_claim": {
      "level": "MEDIUM",
      "dimensions": ["NUMERICAL_ORDER", "MEMORY_BANKING"]
    },
    "patch": "--- a/dotProduct.cpp\n+++ b/dotProduct.cpp\n..."
  }
}
```

Provider 正常拒绝或 abstain 时 outcome 为：

```json
{
  "schema_version": "v3a.planner-outcome.v1",
  "planner_action_id": "...",
  "status": "REJECTED",
  "provider_binding": {"provider": "...", "model": "...", "revision": "..."},
  "rejection_reason_code": "NO_SAFE_PROPOSAL",
  "rejection_summary": "bounded, redacted explanation"
}
```

`REJECTED` 不构造假的 `ProposalState`、Patch digest 或 Candidate。transport/runtime
错误也不伪装成 `REJECTED`，而是走 `TOOL_ERROR/RetryState`。`record_rejected_proposal`
通过 `outcome_ref/hash` 持久化 V2-compatible `PROVIDER_REJECTED` round 结果。

约束：

- `strategy_bundle` 包含 1–3 个配套策略；
- Patch 只修改允许的 kernel 文件；
- Planner 的 `risk_claim` 只是建议，不是 Policy 事实；
- Planner 不返回工具批准、Candidate 晋升或 final 选择；
- Schema 错误最多允许一次小型格式修复；
- V3-A 支持 deterministic/scripted Planner 做 Graph 等价验收；真实模型质量属于 V3-B 验收。

V3-A 持久化格式必须带版本：

```text
v3a.planner-outcome.v1
v3a.plan.v1
v3a.proposal.v1
v3a.candidate-decision.v1
v3a.round.v1
v3a.route-decision.v1
```

V2 compatibility adapter 将当前单一 `change_class` 映射为单元素 `strategy_bundle`，并保持旧 Selector/Provider、score、stop 和 CoSim gate 行为。A0 报告明确标记 `planner_mode=V2_COMPATIBILITY`；A1 才允许原生多策略 Schema。未知 Schema version 必须 fail closed，不能猜测迁移。

## 13. 最小 Optimization Memory

V3-A 不建设通用长期记忆。最小记忆由本次 run 的 Registry、round artifacts 和 trace 派生，并提供有界查询：

- 当前 best 与 metrics digest；
- 最近最多三个不同失败假设；
- 已尝试的 strategy bundle；
- Patch digest；
- 失败阶段和 reason code；
- latency/resource/clock delta；
- 是否发生 CoSim mismatch/deadlock。

重复阻断分成两个阶段。

Planner 调用前使用 request key：

```text
(best_code_hash, metrics_digest, failed_action_digest, planner_config_hash, round_objective)
```

相同 request key 必须复用已持久化的 Planner 结果，不产生第二次模型调用。

Planner 返回后使用 proposal key：

```text
(best_code_hash, metrics_digest, strategy_bundle_digest, patch_digest)
```

相同 proposal key 在 materialization/Vitis 前拒绝。若 request key 已因新增失败摘要而变化，模型仍可能产生重复 proposal；这种情况下最多消耗本次 LLM 调用，不得继续消耗 Candidate 工具 credits。只有 best 或 metrics digest 变化，或失败摘要引入新的明确约束后，才允许重新评估同类策略。

原始历史保存在 Artifact/trace，不塞入 State 或 Prompt。

## 14. Risk Policy 与 CoSim

### 14.1 RiskDecision

确定性 RiskPolicy 输出：

```text
risk_level
risk_dimensions
requires_exploration_cosim
reason_codes
evidence_refs
policy_version
proposal_digest
patched_code_hash
```

风险判断发生在 Candidate materialization 之前，因此 `proposal_digest` 与
`patched_code_hash` 是 immutable RiskDecision artifact 的身份。Candidate 成功
创建后，Registry 事务把新 `candidate_id` 关联到既有 `risk_ref`，不回写或修改
RiskDecision artifact；不得为了取得 `candidate_id` 而提前创建一个尚未通过
closure-budget gate 的 Candidate。

输入包括：

- `task.requires_cosim`；
- validation profile；
- Patch diff/AST/pragma 特征；
- strategy bundle；
- interface、stream、DATAFLOW、FIFO、bitwidth、数值归约和 memory banking 变化；
- 本次运行相似失败记录。

### 14.2 V3-A v1 风险原则

- V3-A0 使用 V2-equivalence Policy：baseline 完整 CoSim；只有 preliminary gain gate PASS 的 Candidate 才运行探索 CoSim，并且 CoSim PASS 后才能晋升；final 完整 CSim/Synth/CoSim；
- V3-A1 才允许启用 `STRICT_AUDIT_RISK_GATED`，且必须保留 A0 profile 作为回归对照；
- `requires_cosim=true`：必要 Candidate 必须通过 CoSim 才可晋升；
- DATAFLOW、stream、FIFO、interface：高 RTL 风险；
- bitwidth、fixed-point、归约顺序：高数值语义风险，CSim 与 CoSim 都不能替代测试覆盖；
- memory partition/layout、loop restructuring、unroll：默认低至中风险，但出现 banking、依赖或数值顺序变化时升级；
- pragma-only 不能永久硬编码为低风险，必须结合 diff 和任务结构；
- 所有 Candidate 先通过 CSim 和 Synth；只有 Synth 严格改善且 Policy 要求时，才消费探索 CoSim；
- Synth 不改善的 Candidate 直接拒绝，不为其运行 CoSim。

## 15. Validation Profile

V3-A 只允许最终 CSim、Synth、CoSim 全部 PASS 的 Candidate 获得团队 release acceptance。公开 Reference scorer 的 `requires_cosim` 行为不等于正式比赛最终规则。

### 15.1 Profile 矩阵

| Profile | Baseline | Exploration gain gate | Promotion | Final | 用途 |
|---|---|---|---|---|---|
| `STRICT_AUDIT_EQUIVALENCE` | CSim→Synth→CoSim | CSim→Synth；严格改善才 CoSim | CoSim PASS 后按 V2 Comparator | CSim→Synth→CoSim | V3-A0、V2等价、团队release |
| `STRICT_AUDIT_RISK_GATED` | CSim→Synth→CoSim | CSim→Synth；严格改善后按 RiskPolicy 决定 CoSim | CoSim PASS 才可权威晋升；跳过 CoSim 只记入 shadow provisional | CSim→Synth→CoSim | V3-A1 显式实验 |

`STRICT_AUDIT_RISK_GATED` 下，低风险 synth-verified Candidate 只能更新独立的
`provisional_best_candidate_id` shadow 记录。它不能覆盖 `best_candidate_id` 或
`best_cosim_candidate_id`，不能成为下一轮权威 working parent，不能进入 fallback
排名，也不能被 `select_final_attempt` 选择。权威 Comparator 继续要求 Candidate
达到 CoSim verification tier 后才允许 PPA 比较，遵守“PPA 不能压过更高正确性
tier”的团队硬不变量。将 shadow Candidate 延迟 CoSim 后再挑战权威 best，属于
V3-B 的显式扩展节点，不在 V3-A1 偷偷实现。

### 15.2 Reference scorer compatibility 不属于 V3-A release profile

未来可增加名为 `REFERENCE_SCORER_COMPAT` 的显式实验模式，用于复现公开 Reference scorer 在 `requires_cosim=false` 时的预算行为。但它：

- 不是“官方规则”或“官方分数”；
- 不属于 V3-A core acceptance；
- 不能生成团队 release PASS；
- 必须与 `STRICT_AUDIT_*` 报告分开；
- 在引入前需单独设计非 release 终态和验收契约。

V3-A 默认且唯一 release profile 是 `STRICT_AUDIT_EQUIVALENCE`。V3-A1 的 risk-gated profile 必须显式选择。

## 16. Budget Policy

### 16.1 权威计费

每个 charged action 遵循：

```text
estimate
-> reserve
-> append STARTED
-> execute
-> persist result
-> reconcile actual usage
-> append COMPLETED
-> return complete snapshot
```

### 16.2 一轮闭环可承担性

V3-A 选择“两级 gate”，不在 Patch 出现前假装知道 patch-level 风险。

Planner 调用前检查 task/profile 已知的最小闭环：

```text
planner token reserve
+ candidate CSim
+ candidate Synth
+ task/profile 已知必须的 Candidate CoSim
+ final reserve
```

`validate_proposal` 完成 dry-run 后先产生 `proposal_digest + patched_code_hash`，随后 `classify_proposal_risk` 在 Candidate 分配前计算权威风险。`evaluate_candidate_closure_budget` 再检查：

```text
candidate CSim
+ candidate Synth
+ proposal RiskPolicy 所需 Candidate CoSim
+ final reserve
```

无法承担时记录 rejected proposal，不分配 Candidate ID、不运行 Candidate 工具。探索动作不得侵占 final reserve。

Ledger 中的 pending reservation 只代表已经开始的单个 charged action；final reserve 是 BudgetPolicy 根据 profile/config 计算的不可花探索额度。两者在报告中必须分列。

### 16.3 基础设施 retry 准入

`evaluate_infra_retry` 不是“报错就再跑一次”。只有同时满足以下条件才返回
`RETRY_*`：

- 原结果是已完成且被分类为 retryable 的 Provider/Vitis `TOOL_ERROR`；
- 该 `logical_operation_id` 尚未使用主动 retry（下一次固定为 `attempt_index=1`）；
- 没有 unresolved pending/`AMBIGUOUS` action；
- `retry estimated credits + continuation_closure_after_retry` 不超过可用 credits；
- 对 LLM retry，预计总 Token 不超过 `tokens_remaining`，并继续分别记录
  input/output/cached usage；
- retry action 以及后续必要 stages 的全部 CSim/Synth/CoSim/LLM call capacity
  都仍可用；
- 预计 retry 加 continuation closure 的 runtime 和安全余量不超过剩余 runtime。

`continuation_closure_after_retry` 不能只写成 final reserve，必须按
`failed_node + validation profile + risk decision` 计算：

| retry origin | retry 成功后必须继续保护的 closure |
|---|---|
| `plan_patch` / `repair_proposal_format` | Candidate CSim + Synth + profile/risk 可能要求的 CoSim + 完整 final closure |
| `baseline_csim` | baseline Synth + CoSim + 完整 final closure |
| `baseline_synth` | baseline CoSim + 完整 final closure |
| `baseline_cosim` | 完整 final closure |
| `candidate_csim` | Candidate Synth + profile/risk 可能要求的 CoSim + 完整 final closure |
| `candidate_synth` | profile/risk 可能要求的 Candidate CoSim + 完整 final closure |
| `candidate_cosim` | 完整 final closure |
| `final_csim` | final Synth + CoSim + 显式 fallback reserve（若有） |
| `final_synth` | final CoSim + 显式 fallback reserve（若有） |
| `final_cosim` | 显式 fallback reserve（若有） |

表中的 closure 在 credits、后续 tool-call capacity 和 runtime 三个适用维度都要
检查；Planner/format retry 还要检查本次 retry Token。第一次失败已经实际核销的
成本不会“退回”计算。这样 baseline/candidate retry 不能把本轮剩余必要 stages 或
最终 closure 的预算花掉，final retry 也不能侵占尚未执行的 final stages。

主动 retry 产生新 action、重新 reserve、重新计费。任一维度不足时输出
`INFRA_RETRY_BUDGET_DENIED`；已 retry 一次输出 `INFRA_RETRY_EXHAUSTED`；
`AMBIGUOUS` 不进入本 gate。BudgetPolicy 必须把 credits、Token、各工具 calls 和
runtime 四类检查结果完整返回给 decision node，并嵌入 checkpointed
`RouteDecision.budget_checks`。BudgetPolicy 和 decision node 都不另写 artifact。

### 16.4 Stop 输出

BudgetPolicy 返回结构化纯值，decision node 将其规范化进 `RouteDecision`，而不是
只抛异常或另写文件：

```text
allowed
requested_action
estimated_cost
spendable_budget
final_reserve
reason_code
snapshot_ref
policy_version
closure_estimate
```

Ledger 负责记账，Policy 负责决策；二者不能合并成第二份预算状态。

## 17. Checkpoint 与恢复

### 17.1 Checkpoint 要求

- 使用 durable checkpointer；开发与正式恢复测试不得使用仅内存 saver；
- `thread_id = run_id`，并使用固定 `checkpoint_namespace`；
- 每个 LLM/Vitis/Registry/package 副作用前后形成可恢复边界；
- action ID 使用稳定 SHA-256，不使用 Python `hash()`；
- checkpoint 绑定 task fingerprint、run/optimization/tool config hashes、policy bundle hash、State schema version、Ledger sequence 和 Registry revision；
- exact LangGraph/checkpointer 依赖版本在实施计划中固定并记录，且必须兼容 Python 3.11；
- SQLite-backed saver 可作为 V3-A 本地默认实现，外部存储通过 adapter 替换。

### 17.2 恢复规则

恢复时先 reconcile：

```text
checkpoint
<-> Budget Ledger
<-> Candidate Registry
<-> action artifacts
```

- `COMPLETED` 且结果哈希有效：直接复用；
- `STARTED` 且结果存在：验证后完成 Ledger 对账；
- `STARTED` 且结果不存在：标记 `AMBIGUOUS` 并按保守策略处理；
- checkpoint 预算快照落后于 Ledger：重新 hydrate，不回滚 Ledger；
- checkpoint 指向不存在或哈希错误的 Candidate/artifact：fail closed；
- checkpoint replay 使用原 action ID；明确基础设施重试使用带 `attempt_index` 的新 action ID，并记录 `retry_of`；
- Planner action 必须像工具 action 一样先写 STARTED、原子保存 result、再写 COMPLETED；现有 pending Planner 直接失败的行为不能作为 V3-A 完成实现；
- materialize、promote、reject、select-final、commit-final 和 package 都使用稳定 `operation_id`；
- Candidate decision transaction 使用 expected Registry revision/CAS，结果写 operation artifact；
- `select_final_attempt` 只写 attempt 字段，不能提前写 `final_candidate_id`；
- package 使用 staging 目录和 `PACKAGE_COMMITTED` marker，恢复按 operation ID 复用或完成提交。

### 17.3 Sealed package 与最终 checkpoint 信任链

Durable checkpointer 和 control journal 必须位于 sealed package 树之外，例如：

```text
run_root/control/checkpoints.sqlite
run_root/control/operations/
run_root/package/                 # sealed payload
```

`control/` 不进入 package Manifest。封存顺序固定为：

1. `commit_final_candidate` 完成，checkpointer 保存
   `FINAL_CANDIDATE_COMMITTED` 状态；
2. `stage_package` 从 checkpoint history 截取到该状态，生成 immutable
   `graph_trace.snapshot.json` 和团队报告；
3. staging 目录写完 payload，Manifest 覆盖 payload 与 trace snapshot，但按协议
   排除 Manifest 自身和尚未创建的 `PACKAGE_COMMITTED` marker；
4. `commit_package` 校验所有 hash，原子发布 staging，并写
   `PACKAGE_COMMITTED` marker。marker 包含 package operation ID、Manifest hash、
   graph-trace snapshot hash 和 final Candidate/hash；
5. Graph 在 package 树外保存 `PACKAGE_COMMITTED`/`DONE` checkpoint，DONE state
   反向引用 marker hash。此后不得修改 `package/` 中任何已封存 payload。

因此 sealed 报告不会声称包含它之后发生的 `commit_package` 与 `DONE` 节点；完整
信任链由“截至 final commit 的 Graph trace snapshot + PACKAGE_COMMITTED marker +
外部 DONE checkpoint”共同构成。若进程在 marker 写入后、DONE checkpoint 前崩溃，
恢复只验证 marker/Manifest 后补写外部 checkpoint，不重新封包。需要展示完整运行
路径的团队 live report 可以在 `control/` 下由三部分派生，但它不属于 sealed payload。

## 18. 错误处理

### 18.1 错误类别

- `CODE_ERROR`：CSim/Synth/CoSim 的候选代码失败；
- `PROPOSAL_ERROR`：Schema、Patch、路径、接口或重复动作错误；
- `TOOL_ERROR`：Vitis、license、runner、文件系统、缺失报告；
- `POLICY_DENIED`：预算、安全、验证深度或重复策略拒绝；
- `STATE_INTEGRITY_ERROR`：checkpoint、Ledger、Registry、artifact 不一致。

Runner 必须在写 StageResult 前区分 `TIMEOUT` 与 `TOOL_ERROR`：可归因于候选执行
超时、综合超时或 CoSim deadlock 的结果写 `TIMEOUT`，按 transition table 进入
Candidate/final 失败路径；license、runner、文件系统等基础设施原因写
`TOOL_ERROR`，再细分 retryable/non-retryable。不得在 route 中凭日志文本临时猜测。

### 18.2 处理原则

- Candidate code fail：拒绝 Candidate，若预算允许则换不同假设；
- Proposal format fail：最多一次格式修复；
- Patch 越权：立即拒绝，不分配 Candidate；
- Checkpoint replay：相同 action ID 只做缓存复用/reconcile，不再次执行；
- 已完成且可重试的 Tool/Provider error：用新的 deterministic retry action ID 主动重试一次，之后 `FAILED(INFRA_ERROR)`；
- `AMBIGUOUS` action：保守核销并 fail closed，不伪装成普通免费重试；
- Evidence/Risk/Score/Policy hash 或 schema 不一致：
  `FAILED(STATE_INTEGRITY_ERROR)`，不降级成 UNKNOWN 后继续；
- Registry/decision CAS 冲突：`FAILED(REGISTRY_CAS_CONFLICT)`，先保留冲突双方
  operation refs，不做 last-writer-wins；
- final fail：尝试已排序且可承担的 fallback；
- 无 valid fallback：`FAILED(NO_VALID_CANDIDATE)`；
- V3-A baseline repair required：明确结束，不调用 optimize Planner 假装修复。
- 只有 `record_failure` 本身无法持久化时，才由 RunCoordinator 抛出 Graph 外
  `UnrecoverableOrchestrationError`。

## 19. 报告与数据流

V3-A 团队报告必须直接读取 Graph/Registry/Ledger/Artifact 证据，不从自然语言反推流程。
sealed package 内的报告以 `FINAL_CANDIDATE_COMMITTED` 为 trace cutoff；package
commit 和 DONE 由 marker/外部 checkpoint 证明。`control/` 下的 live report 可以
在运行后拼接完整路径，但必须标注自己不属于 sealed Manifest。

每个节点记录：

- node name、phase、round；
- started/completed time 和 wall time；
- 输入 Candidate/evidence/plan/risk/result refs；
- action ID 和 side effect 类型；
- edge target、reason code 和 Policy version；
- budget before/reserved/actual/after；
- LLM input/output/cached Token；
- 工具 status、关键指标和 artifact refs；
- Candidate promote/reject/fallback 结果。

每轮必须回答：

1. LLM 看到了哪些公开证据？
2. 它提出什么假设和 strategy bundle？
3. Harness 为什么允许或拒绝？
4. 为什么依次调用这些工具？
5. CoSim 为什么运行或跳过？
6. 实际指标如何变化？
7. Candidate 为什么晋升或拒绝？
8. 花费多少 Token、calls、credits 和时间？
9. 下一轮为什么继续或停止？

报告必须区分：

- Graph 编排通过；
- deterministic/scripted provider smoke；
- 真实 LLM 优化能力；
- Reference/public-validation proxy；
- 外部 grader score（仅在比赛方返回并显式导入后展示，V3-A 自身不能生成或声称）；
- `STRICT_AUDIT_EQUIVALENCE`、`STRICT_AUDIT_RISK_GATED` 与未来非 release 的 Reference compatibility 实验。

## 20. 测试设计

### 20.1 Evidence 单元测试

- top transaction interval 与 loop II 分离；
- loop II=1 时不得被解释为 1025；
- TripCount 和 pipeline status 正确；
- Evidence 绑定 Synth action、Candidate/code/tool hashes、raw report set digest、
  原始 report kind/ref/hash 和 parser version；最终 Manifest 单向绑定 Evidence hash；
- EvidenceExtractor 不扫描临时 `work/` 路径，Manifest 缺失/错配时 fail closed；
- 单元测试证明不存在 Manifest hash ↔ Evidence hash 循环依赖；
- 缺失字段返回 `UNKNOWN`，不伪造；
- public header 的 `PAR_FACTOR` 进入 Planner Context；
- hidden/reference 不被 V3-A `PublicTask` 或 Runtime Context 读取；
- public TB/build scripts 只由 Harness/ToolServer 使用，不进入默认 Prompt；

### 20.2 State 与 route 单元测试

- BootstrapState 不要求伪造 PublicTask/Budget/Registry/working，且 lifecycle invariant
  在 load、Ledger init、baseline materialization 后逐步收紧；
- Ledger 继承 Bootstrap `run_start_epoch_seconds`；preflight 独立 timeout 且耗时进入
  全局 runtime snapshot；
- preflight 使用 `min(independent timeout, runtime remaining)`；Ledger init 时 runtime
  已耗尽则不启动进程并走明确失败边；
- working-set reducer 不跨 Candidate 混合 Validation/Evidence/Risk/Score；
- synth-only shadow Candidate 只能更新 `provisional_best_candidate_id`，不能覆盖
  authoritative best/working、进入 fallback/final 排名或绕过 CoSim tier；
- `final_attempt_candidate_id` 不得提前污染 `final_candidate_id`；
- exploration/baseline PASS 不能满足 final gate；final/fallback selection 必须重建
  全 NOT_RUN、scope=`final` 的独立 validation；
- 每个 route 输出唯一合法下一节点；
- route 无 I/O 和 side effect；
- 每个 `RETRY_*` route key 唯一映射回已注册的原 action node；
- 通过静态 Graph 遍历证明无悬空节点，所有非终态节点至少有一个且仅有契约允许的出口；
- final reserve 优先于继续探索；
- rejected Candidate 后在预算允许时继续；
- 重复 strategy/Patch 不产生新 LLM/Vitis action；
- 所有循环存在轮数、预算和时间上限；
- 所有路径终止于 DONE/FAILED。

### 20.3 V3-A0 确定性等价集成测试

使用相同 task、V2 compatibility Planner、`STRICT_AUDIT_EQUIVALENCE` 和 fake/recorded backend 比较 V2 与 V3-A0：

- best Candidate 等价；
- final Candidate 等价；
- score、raw exploration stop reason 和 raw terminal stop reason 等价；
- normalized stop reason 通过版本化 mapping 单独断言；
- LLM/Vitis charged action 数不增加；
- Token、credits 和工具次数一致；
- completed `PROVIDER_REJECTED` 与 V2 一样计入 no-improvement 并继续/停止，
  不被误判为 infra retry；
- PROPOSAL/REJECTED 都由 `v3a.planner-outcome.v1` 绑定 action ID 和 outcome ref/hash；
  REJECTED 不构造 ProposalState/Candidate；
- V3-A 报告额外包含 Graph path，但不改变事实结果。

V3-A1 的 `STRICT_AUDIT_RISK_GATED` 会有意改变探索 CoSim calls/credits，因此不要求与 V2 调用数完全相同；它必须保证 final 三阶段 PASS、Reference/public-validation proxy 不回归、无禁止数据、无超预算，并与 A0 并列报告节省和风险结果。

### 20.4 Crash/recovery 测试

在以下“结果已落盘、checkpoint 尚未完成”位置注入崩溃：

- Planner；
- Candidate materialization；
- CSim；
- Synth；
- CoSim；
- promote/reject；
- select-final/fallback；
- final package。

恢复后断言：

- 重复 LLM 调用为 0；
- 重复工具调用为 0；
- 重复收费为 0；
- Candidate tree、best、final、score、raw/normalized stop reasons 与无崩溃运行一致；
- State、Ledger、Registry 和 artifacts 可对账。

另外必须分别覆盖：

- checkpoint replay 使用原 action/operation ID，不重复执行；
- completed TOOL_ERROR 的主动 retry 使用新 attempt ID 并链接 `retry_of`；
- retry 准入逐项覆盖 credits、Token、tool-call 和 runtime，且完整保护按
  failed node 计算的 `continuation_closure_after_retry`；
- `STARTED + result` reconcile；
- `STARTED + no result` 进入 `AMBIGUOUS`；
- Registry revision/CAS 冲突 fail closed；
- Evidence/Risk/Score parse/hash mismatch 和 typed non-tool operation error 都进入
  `record_failure -> FAILED`；
- `record_failure` 自身持久化失败是唯一预期的 Graph 外
  `UnrecoverableOrchestrationError`；
- package staging 后崩溃可完成或复用 `PACKAGE_COMMITTED`。
- checkpointer/control journal 位于 sealed package 外；Manifest、trace snapshot、
  marker 和外部 DONE checkpoint 的 hash 信任链闭合且无自引用；
- marker 后、DONE checkpoint 前崩溃只补写外部 checkpoint，不修改 sealed payload。
- DONE State 的 package operation/Manifest/trace/marker ref+hash 与 marker 内容一致，
  且 State 不创建 checkpoint 自引用哈希。

### 20.5 真实 Vitis 纵向闭环

V3-A0 必须使用至少一个不含 reference/golden 代码的公开或团队 fixture，由 deterministic/scripted Planner 返回已知安全 Patch，完成真实 Vitis `STRICT_AUDIT_EQUIVALENCE` 全链：

```text
baseline CSim/Synth/CoSim
-> Planner/Patch/Candidate
-> Candidate CSim/Synth/[gain-gated CoSim]
-> promote 或 reject
-> final CSim/Synth/CoSim
-> package
```

该运行必须产生 Graph path、Ledger、Candidate tree、Manifest 和恢复可核对报告。Patch 是否加速不是 Graph 验收重点，但必须真实走完 promote 或 reject 决策。

`STRICT_AUDIT_EQUIVALENCE` 会为 baseline 和 final 各消费一套完整三阶段验证，再加 Candidate 探索；若公开样例 task budget 不足，必须使用显式记录的本地 audit budget override。报告必须注明它不是公开 Reference 原始预算内的兼容成绩。

V3-A1 另使用公开 dotProduct 进行真实 baseline Evidence Gate：

- CSim PASS；
- Synth PASS；
- loop II、TripCount、top interval 分离；
- Graph 报告能解释节点、边、预算和数据流。

V3-A 不把真实 LLM 必须达到加速作为完成条件。真实模型在最多四轮内产生实际加速、以及强目标 latency `<=128`，属于 V3-B intelligence gate。

## 21. V3-A 完成标准

只有同时满足以下条件，V3-A0 才算完成：

1. optimize-only 动作级 Graph 从公开任务输入运行到 DONE/明确 FAILED；
2. 每个 LLM/Vitis 副作用属于单独 checkpointable node；
3. State 只含小型结构化字段和 refs；
4. `STRICT_AUDIT_EQUIVALENCE` 的 baseline/exploration/final 行为与 V2 等价；
5. BudgetPolicy 保护完整 final reserve；
6. reject 后在允许时继续，不 stop-on-first-rejection；
7. deterministic V2/V3-A0 best/final/score/raw-stop/calls/credits 等价测试通过；
8. deterministic Patch 驱动的真实 Vitis 完整纵向闭环通过；
9. 所有指定 crash 点恢复不重复调用或扣费；
10. final/fallback 使用独立 final-scope validation，探索结果不能冒充最终 PASS；
11. 所有静态 retry route 和多维预算准入测试通过；
12. Registry CAS、Planner journal 和 package commit protocol 通过恢复测试；
13. 报告能逐节点解释数据流、路由、Candidate 和成本；
14. 默认 V2 CLI/生产流程未被替换；
15. 所有既有测试继续通过。

只有 A0 完成后，才检查 V3-A1：

1. Evidence 正确区分 top interval 与 loop II；
2. Planner Context 包含 typed 公开 header/description 且不含 forbidden 输入；
3. Planner Schema 支持 1–3 个配套策略，但不能批准工具或 final；
4. pre-LLM request key 和 post-proposal key 都能阻断对应重复工作；
5. RiskPolicy 产生可审计决策；synth-only 结果只进入 shadow provisional，权威
   best/fallback/final 不降 correctness tier，final 仍完成三阶段；
6. 关闭 A1 feature gates 后，所有 A0 等价测试继续通过；
7. dotProduct Evidence Gate 通过；
8. 真实模型优化质量明确留给 V3-B，不用 scripted 结果冒充。

## 22. 实施顺序

本文批准后，实施计划按以下顺序拆分：

### V3-A0 顺序

1. 先写 State/working-set reducer、完整 transition graph 和 operation identity 的 failing tests；
2. 定义 V2 compatibility Planner 与 `STRICT_AUDIT_EQUIVALENCE`；
3. 将单阶段 CSim/Synth/CoSim 暴露为可复用动作服务；
4. 增加 PlannerActionJournal、CandidateDecisionTransaction 和 package commit protocol；
5. 实现 State、动作节点、decision nodes、pure routes 和 durable checkpointer adapter；
6. 接入 stop/final/fallback 和 Graph trace；
7. 完成 deterministic V2/V3-A0 等价测试；
8. 完成所有 crash/recovery 测试；
9. 完成 deterministic Patch 驱动的真实 Vitis 严格纵向闭环；
10. 保持 V2 为默认入口，通过独立 feature flag/CLI 启用 V3-A0。

### V3-A1 顺序

1. 先写 loop Evidence、typed Planner Context 和两级 dedupe failing tests；
2. 扩展 Synth Evidence parser，不改变 A0 路由；
3. 增加 strategy bundle Planner schema 和兼容 adapter；
4. 增加 RiskPolicy、shadow provisional transaction 与
   `STRICT_AUDIT_RISK_GATED` 实验 feature；
5. 证明关闭 A1 features 后 A0 完全不回归；
6. 运行 dotProduct Evidence/Graph smoke；
7. 完成 V3-A1 后再进入 V3-B 真实 LLM 优化质量实验。

## 23. 后续路线与扩展插槽

| 阶段 | 责任边界 |
|---|---|
| V3-A0 | 动作级 Graph、严格 V2 等价、事务恢复、完整真实 Vitis scripted 闭环 |
| V3-A1 | loop Evidence、公开 header context、strategy bundle、最小 risk-gated 实验 |
| V3-B | 真实 LLM 多轮策略质量、dotProduct 实际加速、Token/credit 消融 |
| V3-C | Repair、structural、CoSim mismatch/deadlock 子图 |
| V3-D | 多任务泛化、多 Candidate frontier、模型/策略对比 |
| V4 | Docker、hidden-like、最终比赛强化、技术报告和 demo |

V3-A 预留但不实现：

- `RepairSubgraph`；
- `StructuralCosimSubgraph`；
- 多 Candidate frontier/beam search；
- 官方文档离线知识检索；
- model router；
- learned Risk/Value Estimator；
- bandit/RL Tool Selection；
- Docker/hidden-like/V4 强化。

这些扩展必须通过现有 Policy/Planner/Registry/Graph 接口接入，不得绕过 Ledger、ToolServer、Patch Policy 或 Candidate Comparator。

## 24. 最终原则

```text
V2 mechanisms 执行并保存事实。
V3-A policies 决定允许什么。
LangGraph 编排阶段、路由和恢复。
LLM 提出假设与 Patch，不拥有预算、工具和 final 权力。
Vitis 结果决定正确性和性能事实。
先修正确证据，再构建智能搜索。
先跑通一条窄但完整的 optimize 纵向闭环，再增加横向能力和新分支。
```
