# V2 Candidate Tree 与 PPA 优化闭环设计

Status: approved<br>
Owner: team<br>
Date: 2026-07-16<br>
Scope: `llm4hls_harness` V2 Candidate and PPA Loop

## 1. 目标与边界

V2 在已完成的 V1 最小修复闭环上增加多 Candidate 与 PPA 优化能力。首个真实纵向
闭环使用自包含的 U55C `vector_add` optimize fixture，通过真实
`deepseek-v4-pro` 生成优化 Patch，并以 Vitis 2025.2 的 CSim、Synth、CoSim、Clock
和 PPA 报告作为唯一事实来源。

V2 必须做到：

1. 维护具有明确 parent-child 关系的 Candidate tree；
2. 按 verification、hard constraints、PPA、cost 的字典序比较 Candidate；
3. 每轮只允许一种优化类；
4. 只提升经过真实验证且严格更优的 Candidate；
5. 失败或不更优 Candidate 不得污染 best；
6. 最终输出来自重新通过完整验证的 best verified Candidate；
7. 所有 Token、工具、credits、Trace、Ledger、Registry、Manifest 和 Vitis 证据可对账；
8. 生成 `runs/` 根目录中的中英文 Markdown 单文件验收报告，不生成 HTML。

V2 不实现完整 LangGraph、通用 checkpointer、多模型对比、hidden-like benchmark、
Docker 最终复现或 demo 打包；这些分别属于 V3/V4。

## 2. 已批准决策

- fixture：现有 U55C `vector_add` 的专用 optimize 版本；
- PPA 偏好：latency/II 为主，资源为次要指标；
- 正式 best 来源：真实 OpenAI-compatible `deepseek-v4-pro`，fallback 关闭；
- deterministic Patch：只用于 rejected/safety 验证，不得成为正式 LLM best；
- 最大优化轮次：4；
- 连续无改善停止阈值：2；
- 优化类选择：确定性 `OptimizationSelector`；
- LLM 权限：只在本轮唯一允许的优化类中生成一个受限 unified diff；
- 报告：中英文 Markdown-only，平铺在 `runs/` 根目录。

所有预算、次数、权重、时钟和资源阈值必须来自配置或 CLI，不得写死在比较器或流程中。

## 3. 方案选择

采用模块化的普通 Python V2 闭环：抽取可复用 Candidate 管理服务，并新增独立的
scoring、optimization、V2 acceptance 和 V2 review 模块。

不采用以下方案：

- 直接把多轮循环继续堆入 `repair.py`：会耦合修复、优化、候选、评分与报告；
- V2 直接引入 LangGraph：会把 Candidate/PPA 与 V3 编排、checkpoint 问题混在一起。

模块化普通 Python 服务可以在 V3 中直接包装为图节点，同时让 V2 的比较器、Selector、
预算门和 Candidate 不变量独立测试。

## 4. 总体数据流

```text
load optimize task
  -> immutable candidate_000
  -> real baseline csim/synth/cosim/clock
  -> parse baseline PPA and set initial best
  -> while round < 4 and no_improvement < 2
       -> preserve final verification reserve
       -> deterministic OptimizationSelector chooses one class
       -> build localized PPA prompt
       -> real DeepSeek proposal
       -> schema + Patch policy + dry-run
       -> materialize Candidate only after Patch passes
       -> candidate csim/synth/cosim/clock
       -> calculate comparator key
       -> promote if strictly better, otherwise reject
  -> independent final csim/synth/cosim/clock on best
  -> affordable ranked fallback or explicit failure
  -> result + report + manifest
```

首个 fixture 设置 `requires_cosim=true`。因此每个可晋级 Candidate 都必须达到 CoSim
PASS，与已完整验证的 baseline 处于相同 verification tier；不得用仅通过 Synth 的
Candidate 击败已通过 CoSim 的 baseline。

## 5. U55C Vector-Add Optimize Fixture

新增：

```text
llm4hls_harness/examples/u55c_v2_optimize_task/
├── task.toml
├── description.md
├── kernel.cpp
├── kernel.h
└── kernel_tb.cpp
```

契约：

- `task_type = "optimize"`；
- top function 为 `vector_add`；
- part 为 `xcu55c-fsvh2892-2L-e`；
- 目标工具链为 Vitis 2025.2；
- target clock 为 10 ns，最低频率为 100 MHz；
- `requires_cosim = true`；
- baseline 功能正确且具有可由循环优化改善的性能空间；
- public testbench 覆盖正数、负数、零值和边界整数；
- top 名、签名、参数顺序、类型、接口和数值语义不可变；
- runtime 不读取 `hidden/` 或 `reference/`；
- Prompt 不包含最佳代码或 golden answer。

fixture 必须先通过真实 baseline preflight。不能在未观察真实 Vitis baseline 指标前
硬编码期望 latency、II 或资源数值；正式验收只要求机器计算出的严格改善。

## 6. Candidate Tree 与 Registry

Candidate 默认从创建该轮时的 current best 克隆：

```text
candidate_000 baseline
├── candidate_001 LOOP_PIPELINE     rejected
└── candidate_002 LOOP_UNROLL       promoted
    ├── candidate_003 MEMORY_LAYOUT rejected
    └── candidate_004 LOOP_RESTRUCTURE promoted
```

改善形成主链；拒绝后下一轮仍从 best 出发，从而形成分支。默认不在失败 Candidate 上
叠加修改。无效 Patch 在 Candidate ID 分配和物化之前被拒绝。

Candidate Registry 记录至少包含：

```text
candidate_id, parent_id, round, kind, optimization_class,
source_ref, patch_ref, code_hash, patch_sha256,
provider, model, revision,
input_tokens, output_tokens, cached_input_tokens,
validation, clock_constraint, metrics_ref,
score_ref, comparison_ref, status,
promotion_reason, rejection_reason, credits_used
```

源码、Patch 和物化时的 `candidate.json` 采用 staging 后原子移动，并在物化后只读。
动态 validation/selection 状态保存在 Registry、独立结果和 Trace 中。每个 validation
必须绑定 `candidate_id + code_hash + tool_config_hash + validation_scope`。

状态至少区分：

```text
MATERIALIZED
EVALUATING
VERIFIED
PROMOTED
REJECTED_VALIDATION
REJECTED_HARD_CONSTRAINT
REJECTED_NOT_BETTER
FINAL
```

曾经被提升但后来被更优 Candidate 替代的历史不得删除；Registry 的 current best 指针
与 Trace 的 promotion/supersession 事件共同表达历史。

## 7. OptimizationSelector

首版允许以下优化类：

1. `LOOP_PIPELINE`；
2. `LOOP_UNROLL`；
3. `MEMORY_LAYOUT`；
4. `LOOP_RESTRUCTURE`。

Selector 输入为 current-best 源码结构摘要、结构化 Vitis 指标/瓶颈、已尝试类与失败
记忆。规则优先级：

- II/interval 较高且循环未显式流水：`LOOP_PIPELINE`；
- pipeline 后 latency 仍高且 trip count 静态：`LOOP_UNROLL`；
- 报告出现 memory-port 或 array bottleneck：`MEMORY_LAYOUT`；
- 其他有结构化循环证据的瓶颈：`LOOP_RESTRUCTURE`。

Selector 每次返回一个类或明确 `NO_DISTINCT_OPTIMIZATION`。没有新证据时不得重复失败
类。LLM 输出的 `optimization_class` 必须与允许值完全一致，且 Patch 只能实现该类；
混合多个优化类的 Patch 被拒绝。

## 8. LLM 优化调用契约

正式 Provider 为 OpenAI-compatible API，模型为 `deepseek-v4-pro`。endpoint、模型和
API key 仍从 CLI/环境变量读取；API key 不得进入源码、配置、Prompt、Trace、报告或
Artifact Manifest。正式 acceptance 要求 fallback=`NOT_USED`。

输入仅包含：

- current best 的局部 kernel；
- top/interface/numeric/clock 约束；
- baseline 和 current-best 的结构化 PPA；
- 当前瓶颈和本轮唯一允许优化类；
- 已失败动作摘要；
- 剩余轮次、Token、credits 和最终验证 reserve；
- Patch 文件与改动行数策略。

输出为严格 JSON：

```json
{
  "hypothesis": "...",
  "optimization_class": "LOOP_PIPELINE",
  "expected_effect": "...",
  "risk": "low",
  "required_validation": ["csim", "synth", "cosim"],
  "patch": "--- kernel.cpp\n+++ kernel.cpp\n..."
}
```

LLM 无权选择/promote best、宣布 PPA 改善、降低必须验证的阶段、修改预算、调用 Vitis/
shell 或修改 testbench/header/interface。结构化格式修复最多一次；仍失败则本轮不物化
Candidate，并记为一次无改善尝试。

一个 optimization round 指一次主要优化 proposal。格式修复调用只消耗额外 LLM call
和 Token，不增加 optimization round；但若格式修复仍失败，该主要 proposal 所在轮次
仍计为一次无改善。`max_llm_calls` 是独立硬上限，因此并不保证每个轮次都有格式重试。

Patch 继续执行 V1 的 parse、hunk normalization、policy、dry-run 和原子物化规则。
默认最大改动 30 行，仅允许配置的 kernel `.cpp`。

## 9. Candidate Comparator

比较使用严格字典序，不把正确性与 PPA 混成一个总分：

```text
verification tier
  > hard constraints
  > official score, when configured
  > development ppa_cost
  > lower incremental token/tool cost
  > stable candidate_id
```

verification tier：

```text
cosim pass
  > synth + csim pass
  > csim pass
  > static pass
  > failed
```

hard constraints 至少包括：

- 任务要求的 correctness tier；
- estimated clock period ≤ 10 ns；
- latency、II、LUT、FF、DSP、BRAM、URAM 和 available resources 完整有效；
- 每类资源不超过配置上限；
- top/interface/public inputs 未变化。

缺失、布尔值、负数、NaN、Infinity、零/负 available resource 均使 Candidate 不满足
比较前提。PPA 永远不能挽救低 verification tier 或 hard-constraint failure。

## 10. Development PPA Cost

配置文件为 `llm4hls_agent/config/v2_scoring.yaml`。为保持标准库-only runtime，文件
使用 JSON-compatible YAML 1.2 语法并由标准库 `json` 严格解析；不新增 PyYAML 依赖。

已批准默认权重：

```yaml
{
  "ppa_weights": {
    "latency": 0.45,
    "ii": 0.35,
    "lut": 0.06,
    "ff": 0.04,
    "dsp": 0.04,
    "bram": 0.04,
    "uram": 0.02
  }
}
```

权重必须为非负有限数且总和为 1。开发期 cost 越低越好：

```text
latency_component = candidate_worst_latency / baseline_worst_latency
ii_component      = candidate_max_interval / baseline_max_interval
resource_component(r)
  = 1 + candidate_r / available_r - baseline_r / available_r

ppa_cost = Σ weight_i * component_i
```

这种资源归一化在 baseline 资源为 0 时仍有定义：两者为 0 得到 1；新增资源得到大于 1
的惩罚；减少资源得到小于 1 的改善。硬资源上限在计算 PPA 前单独检查。

Clock 在首版只作为 hard constraint，不进入加权 PPA。配置保留 official score 字段但
默认 disabled；不得伪造官方评分。成本 tie-breaker 使用 Candidate 增量
`input + output Tokens`、验证 credits，再使用稳定 Candidate ID。Cached Token 是 input
Token 子集，只单独披露，不重复加入总 Token。

## 11. Budget 与停止

fixture 建议默认：

```text
credit limit                  160
token limit                   32768
max optimization rounds       4
max no-improvement rounds      2
max LLM calls                  6
max csim/synth/cosim calls     6/6/6
final verification reserve     25 credits
```

开发价格仍为 CSim=1、Synth=4、CoSim=20、LLM=0 credits；LLM 使用真实 Provider Token
计量。所有值可由配置/CLI 覆盖。

调用 LLM 前必须证明剩余预算能够承担本轮必需验证和 final reserve。每个 charged action
继续执行：

```text
estimate -> reserve -> STARTED -> execute -> artifact -> reconcile -> COMPLETED
```

最终验证使用独立 `validation_scope=final` action identity；探索缓存不能伪装成新的
最终验证。V2 只实现本闭环需要的 reserve、稳定 action ID 和进程重启复用；通用图级
checkpointer 与多维动态策略属于 V3。

停止原因至少包括：

```text
MAX_OPTIMIZATION_ROUNDS
NO_IMPROVEMENT_LIMIT
NO_DISTINCT_OPTIMIZATION
FINAL_RESERVE_REACHED
TOKEN_LIMIT_REACHED
TOOL_LIMIT_REACHED
CANDIDATE_VERIFIED
NO_VALID_CANDIDATE
```

## 12. Promotion、Reject 与 Final Fallback

正常循环：

```text
best -> clone -> one Patch -> validate -> compare
  better     -> promote and reset no_improvement
  not better -> reject and increment no_improvement
  failed     -> reject and increment no_improvement
```

Provider/Patch 在物化前失败时不创建 Candidate。物化后发生 CSim、Synth、Clock、CoSim
失败时保留 Candidate 和全部证据，标记明确 rejection reason，best 不变。

探索结束后对 current best 执行独立 final CSim/Synth/CoSim/Clock。若 final 失败，按完整
字典序从已验证 Candidate 中选择预算可承担的 fallback 并重新 final 验证；不存在可承担
fallback 时终止为 `FAILED(NO_VALID_CANDIDATE)`。deterministic fallback 不得满足正式
DeepSeek V2 acceptance。

若没有 LLM Candidate 严格改善，安全工作流可以最终返回 baseline，但 V2 里程碑机器
验收必须 FAIL，不能把安全返回 baseline 解释为 PPA 优化成功。

## 13. 运行产物

单个优化运行至少包含：

```text
task_spec.json
run_config.json
scoring_config.json
baseline/source/...
candidates/candidate_NNN/...
optimization_rounds/round_NNN.json
llm_actions/.../result.json
actions/.../result.json
candidate_registry.json
budget_ledger.jsonl
budget_state.json
trace.jsonl
v2_result.json
experimental_report.md
artifact_manifest.json
```

所有引用使用相对 run-dir 的路径。Prompt、完整日志和大报告保存在 Artifact Store，State/
Registry 只保存小型结构与引用。Manifest 排除自身并覆盖所有必须审计的机器产物。

## 14. 正式验收矩阵

### 14.1 Real DeepSeek Optimization

正式目录：`runs/v2-vector-add-final/`。

必须满足：

- baseline 真实通过 Vitis 2025.2 CSim/Synth/CoSim/Clock；
- 至少 2 个 `openai-compatible/deepseek-v4-pro` Candidate 完整验证通过；
- 至少 1 个 DeepSeek Candidate 相对 baseline 严格改善并被 promote；
- 每轮只有一个允许优化类；
- Candidate lineage、Patch、hash、validation 和 PPA 绑定一致；
- 重新计算 Comparator 得到的 best 与 Registry 一致；
- final best 独立通过完整验证；
- fallback=`NOT_USED`；
- Token、工具、credits、Trace、Ledger、action 和 Manifest 完整一致。

允许最多四轮才达到上述最低证据。若真实模型未产生两个可验证 Candidate 或没有严格
改善，保留失败运行并使用新 run-dir 重试，禁止修改原始运行或硬编码 PASS。

### 14.2 Candidate Rejection Safety

正式目录：`runs/v2-rejection-final/`。

使用 deterministic、kernel-only、语义回归 Patch：Patch 合法并物化 Candidate，随后
真实 CSim 失败；Candidate 被标记 `REJECTED_VALIDATION`，原 best 不变，testbench、
header、接口和 baseline 不变。该场景证明 Candidate safety，不作为新的 HLS 错误类别，
也不能产生正式 best。

### 14.3 Unified Acceptance

机器结果写入：

```text
runs/v2-acceptance/acceptance_result.json
```

确定性 Acceptance Evaluator 重新检查：

- Candidate tree/lineage；
- real Vitis backend/toolchain；
- real DeepSeek/no fallback；
- 两个以上正确 Candidate；
- 至少一个 rejected Candidate 且 best 未污染；
- verification/hard constraints/PPA/cost 字典序；
- best/final 指针与独立 final validation；
- Ledger/Trace/action/Registry/Manifest；
- Token/credits 完整性；
- baseline/public inputs 不变。

任一缺失、篡改、不一致或不可解析均 fail closed。Renderer 不得硬编码 PASS。

## 15. Markdown-Only 人工验收

离线生成：

```text
runs/V2_ACCEPTANCE_REPORT.md
runs/V2_ACCEPTANCE_REPORT_CN.md
```

报告从同一个只读 ReviewEvidence 生成，不调用 LLM/Vitis、不修改机器证据、不生成
HTML。正文直接展示：

- overall recorded/recomputed PASS/FAIL；
- baseline 与 final 的完整 PPA；
- Candidate tree；
- 每轮 parent、优化类、Provider、Model、fallback、Token、工具与 credits；
- 每个 Candidate 的 CSim/Synth/CoSim/Clock；
- latency、II、LUT、FF、DSP、BRAM、URAM；
- PPA cost、Comparator key 和 promote/reject 原因；
- no-improvement、stop reason 与 final reserve；
- final/fallback 状态；
- Ledger/Trace/Registry/Manifest/action 一致性；
- 小 Patch 完整 diff，大型原始文件的相对审计链接。

生成前后比较所有机器证据的 hash、size、mtime；输出包含绝对路径或与
`acceptance_result.json` 不一致时总体 FAIL。

## 16. 模块与接口

新增：

```text
llm4hls_agent/candidate.py
llm4hls_agent/scoring.py
llm4hls_agent/optimization.py
llm4hls_agent/v2_acceptance.py
llm4hls_agent/v2_review.py
llm4hls_agent/config/v2_scoring.yaml
llm4hls_agent/config/v2_acceptance.json
```

职责：

- `candidate.py`：Candidate ID、staging、物化、lineage、Registry 不变量；
- `scoring.py`：metrics 校验、hard constraints、归一化、Comparator；
- `optimization.py`：Selector、Prompt、轮次、验证、promotion/final；
- `v2_acceptance.py`：机器验收与中英基础 acceptance report；
- `v2_review.py`：平铺的中英文单文件人工报告。

V1 CLI 和证据 schema 必须保持兼容。抽取 CandidateManager 时先以 characterization tests
固定 V1 行为，再让 V1 wrapper 使用共享服务，避免无关重写。

## 17. CLI

真实优化：

```bash
python3 -m llm4hls_agent optimize examples/u55c_v2_optimize_task \
  --run-dir runs/v2-vector-add-final \
  --vitis-root /home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis \
  --clock-ns 10 \
  --minimum-frequency-mhz 100 \
  --model deepseek-v4-pro \
  --scoring-config llm4hls_agent/config/v2_scoring.yaml \
  --max-optimization-rounds 4 \
  --max-no-improvement-rounds 2 \
  --credit-limit 160
```

统一机器验收：

```bash
python3 -m llm4hls_agent accept-v2 \
  --optimization-run runs/v2-vector-add-final \
  --rejection-run runs/v2-rejection-final \
  --output-dir runs/v2-acceptance
```

人工报告：

```bash
python3 -m llm4hls_agent review-v2 --runs-root runs
```

CLI 输出机器可读 JSON summary；错误返回结构化 JSON 和非零退出码。

## 18. 测试与真实证据

### 18.1 单元测试

- Candidate ID、parent、branch、原子物化和只读性；
- invalid Patch 不分配 Candidate；
- verification tier 全排列；
- hard constraints 优先于 PPA；
- PPA 优先于 Token/credits；
- latency/II 权重；
- baseline 资源为 0 的安全归一化；
- missing/NaN/negative metrics fail closed；
- Selector 每轮一个类且无证据不重复；
- promote、validation reject、not-better reject；
- best 不受失败 Candidate 污染；
- exploration/final action identity 分离；
- final reserve 和 2 轮无改善停止；
- Provider class mismatch；
- 进程重启复用 completed action、无重复计费；
- 中英文报告同源、相对链接、无 HTML。

### 18.2 Fake Backend Integration

构造 baseline PASS、Candidate 1 改善、Candidate 2 再改善、Candidate 3 CSim 失败、
Candidate 4 PPA 退化、final best PASS，核对 tree、Comparator、Ledger、Trace、stop
reason、best/final 与 budget。

### 18.3 Real Evidence

1. Vitis 2025.2 baseline preflight；
2. 真实 DeepSeek optimize run；
3. 真实 Candidate rejection run；
4. `accept-v2`；
5. 离线生成两份 Markdown；
6. 生成前后机器证据 hash/size/mtime 一致；
7. 全部快速测试、compileall、`git diff --check`；
8. 人工复核 Candidate/PPA/Token/credits/Trace/Ledger。

## 19. 实施顺序

1. 固化并提交本设计；
2. 编写详细 implementation plan；
3. 在 V2 feature branch 中按 TDD 工作；
4. 抽取 CandidateManager 并保持 V1 回归；
5. 实现配置化 metrics、hard constraints 和 Comparator；
6. 实现 OptimizationSelector；
7. 实现 DeepSeek optimize Prompt 和响应校验；
8. 实现四轮 Candidate/PPA loop；
9. 实现 final reserve、独立 final validation 和 ranked fallback；
10. 创建 U55C optimize fixture；
11. 实现 deterministic rejection safety case；
12. 实现 `accept-v2`、Manifest 和机器一致性检查；
13. 实现 Markdown-only `review-v2`；
14. 完成 unit/fake/recovery 测试；
15. 执行真实 Vitis preflight；
16. 执行真实 DeepSeek + Vitis 运行；
17. 生成统一机器验收和中英文人工报告；
18. 更新 readiness 问题复盘；
19. 验证后提交并合并。

## 20. 完成标准

V2 只有在以下条件全部成立时完成：

- 真实运行具有 Candidate tree 和可审计 lineage；
- 至少两个 DeepSeek Candidate 通过完整真实验证；
- 至少一个 DeepSeek Candidate 严格改善并成为 best；
- 至少一个物化 Candidate 被安全拒绝且 best 未污染；
- 每轮恰好一个优化类；
- Comparator 的机器重算与实际 promotion 一致；
- final best 独立通过 CSim、Synth、CoSim 和 Clock；
- PPA、Token、工具、credits、stop reason 完整；
- Registry、Ledger、Trace、action、Manifest 相互一致；
- `acceptance_result.json` 为 REAL/PASS；
- 两份平铺 Markdown 与机器验收一致且无绝对路径；
- 全部测试通过，且有新鲜 Vitis 2025.2 命令输出证明真实结果。

单元测试 PASS、fake backend PASS、baseline 安全返回或 deterministic best 均不能单独
宣布 V2 完成。
