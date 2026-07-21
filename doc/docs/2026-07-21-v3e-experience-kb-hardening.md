# V3-E 经验知识库：当前真实架构与组员上手说明

更新时间：2026-07-21
分支：`feat/v3e-token-budget-policy`

## 1. 这一阶段究竟在做什么

V3-E 不再修改 HLS 主执行流程。它只把已经发生过的真实 Agent 实验整理成一个安全、可解释的经验知识库，回答三个问题：

1. 以前遇到相似问题时，模型实际改了什么；
2. 哪些修改通过了 CSim、Synth、CoSim 和 fresh final，哪些失败或没有收益；
3. 当前证据是否足够可靠，值得把一条短建议提供给 Planner。

经验层只有“建议权”，没有“执行权”。它不能改 Candidate、调用 Vitis、增加预算、晋升 Candidate、选择 final，也不能绕过 Patch Validator、TopInterfaceGuard、`task.requires_cosim` 或 fresh final。

## 2. 数据怎样流动

```text
真实 public train/dev 运行目录
  └─ Planner、Candidate、CSim/Synth/CoSim/final 结构化 Artifact
          ↓ Historical Importer（只抽取真实 LLM + Vitis）
不可变 Experience v1 Store
          ↓ Migration + StrategyNormalizer（读取真实 diff）
派生 Experience v2 Store / Snapshot / Secondary Index
          ↓ SimilarCaseRetriever（硬过滤 + 可解释相似度）
          ↓ BayesianStrategyRanker（按 strategy atom 统计）
          ↓ GuidanceQualityGate（INJECT 或 ABSTAIN）
最多 600 token 的建议摘要
          ↓ 仅 guided 模式会进入 Planner；shadow 只旁路记录
Planner 自己选择策略并生成 Patch
          ↓ 真实工具结果
Recommendation Attribution（相关性记录，不宣称因果）
```

主 LangGraph、PhaseRouter、预算、Candidate 和工具链都位于这条数据流之外，仍按 V3-D/V3-C 的确定性规则工作。

## 3. 组件和文件分别做什么

| 组件 | 主要文件 | 通俗职责 |
|---|---|---|
| v2 Schema 与 taxonomy | `llm4hls_agent/v3_experience_v2.py` | 定义每条经验必须有哪些字段，以及四种 mode 的 failure、bottleneck、strategy 标准词表。 |
| StrategyNormalizer | `llm4hls_agent/v3_experience_normalizer.py` | 比较 parent/candidate/Patch，识别模型实际做的修改；Planner 自报仅作次级证据。 |
| Historical Importer | `llm4hls_agent/v3_experience_importer.py` | 从已有 run 中抽取 v1 记录；排除 fixture、Oracle、golden 和来源不可信的结果。 |
| v1 Store | `llm4hls_agent/v3_experience_store.py` | 保存原始、append-only、幂等的 v1 经验；历史内容不回写。 |
| v2 Knowledge Base | `llm4hls_agent/v3_experience_kb.py` | 保存派生 v2、建立二级索引、冻结 snapshot、隔离损坏记录。 |
| SimilarCaseRetriever | `v3_experience_kb.py` | 先按来源/split/mode 等硬过滤，再按 subtype、algorithm、结构和 toolchain 加权；输出 matched/mismatched 原因。 |
| BayesianStrategyRanker | `llm4hls_agent/v3_experience_kb_quality.py` | 对单个 canonical strategy atom 统计成功、失败、无收益、加速和成本，并用 Beta(1,1) 平滑小样本。 |
| GuidanceQualityGate | `v3_experience_kb_quality.py` | 检查支持数、独立 run/family、相似度、冲突、版本和 600-token 上限；证据不足就 ABSTAIN。 |
| Recommendation Attribution | `llm4hls_agent/v3_experience_attribution.py` | 记录建议被 FOLLOWED、PARTIALLY_FOLLOWED、IGNORED 或 CONTRADICTED，以及后续验证、收益和成本。 |
| Coverage / Collection | `llm4hls_agent/v3_experience_analysis.py` | 统计 mode/subtype/strategy/family 正负样本缺口，并生成下一批真实采集队列。 |
| 泛化评估 | `llm4hls_agent/v3_experience_evaluation.py` | 做 Leave-One-Run/Task/Task-Family/Algorithm-Family-Out，确保被留出的组不进入支持集。 |
| ML 数据导出 | `llm4hls_agent/v3_experience_ml.py` | 按 task family 分组切分，导出 Strategy、CoSim Risk、Continue Value、Evidence Selection 四类数据。 |
| 操作 CLI | `llm4hls_agent/v3_experience_kb_cli.py` | 提供 import、migrate、validate、snapshot、stats、coverage、retrieve、rank、evaluate、plan-collection、export-ml、readiness。 |
| Budget Ledger | `llm4hls_agent/budget.py` | 只记录真实 Token、Credit、工具次数和时间，并执行硬预算检查；原有记账口径未改变。 |
| TokenEstimator | `llm4hls_agent/budget.py` | 按 kernel、header、description、Evidence、history、Guidance 和固定模板估算 Planner 输入，不保存原文或秘密。 |
| TokenBudgetPolicy / TokenEnvelope | `llm4hls_agent/budget.py` | 在每轮 Planner 调用前计算可用输出、后续轮次预留、动态 Guidance cap 和 Token pressure；不是新 Agent 或 Graph 节点。 |
| 两阶段 Planner Adapter | `llm4hls_agent/v3_openai_planner.py` | 先估算无 Guidance Prompt，再限长生成 Guidance，最后重新估算实际 Provider 请求并生成不可变 Envelope。 |
| Provider 与截断处理 | `llm4hls_agent/openai_provider.py`、`v3_planner_action.py` | 确保 Prompt 与 API 使用同一个输出上限；识别 length、JSON/Patch incomplete 和 context limit，截断时不创建 Candidate、不运行 Vitis。 |

## 4. v2 一条经验包含什么

`v3e.experience.v2` 只保存可检索、可审计的结构化事实：

- `source`：匿名 run/candidate、真实 provider/model、Prompt/toolchain 版本、split、family hash；
- `problem`：mode、failure subtype、bottleneck subtype、数值语义和 CoSim 要求；
- `structure_features`：循环、II、TripCount bucket、transaction interval、pipeline/dataflow/stream/FIFO、内存和资源压力；
- `strategy`：declared bundle、observed atoms、归一化置信度、Patch 复杂度和 digest；
- `validation`：Patch/interface guard、CSim/Synth/CoSim/final、promote/reject；
- `performance`：前后 latency/interval、acceleration、resource delta；
- `cost`：Token、Credit、工具次数和 wall time；
- `token_policy`：调用前剩余 Token、分项估算、configured/effective 输出上限、Provider 实际 usage、Guidance、pressure、finish reason 和截断原因；
- `provenance`：相对 Artifact 引用、hash、检索/排序资格和排除原因。

完整源码、完整 Patch、完整 Prompt、完整日志、API key、Authorization、本机绝对路径和 task ID 都不进入 v2 Store。task ID 只允许留在独立审计 Artifact 中，不能参与相似度或排序。

## 5. declared strategy 与 observed strategy 的区别

例如 Planner 声称使用 `PARALLEL_REDUCTION`，但 Patch 实际只增加 `#pragma HLS PIPELINE`：

- declared：`PARALLEL_REDUCTION`；
- observed：`LOOP_PIPELINE`；
- 记录冲突 reason code；
- Ranker 使用真实 observed atom，不把自报策略当成功证据。

Normalizer 的证据优先级是：真实 Patch/diff > 当前 Evidence > Planner 声明。不确定就落入对应 `OTHER_*`，同时降低 confidence，绝不凭 task 名称猜答案。

## 6. Retriever、Ranker 和 Gate 的分工

Retriever 负责“找相似历史”；Ranker 负责“算哪种 atom 的历史结果更好”；Gate 负责“证据够不够进入 Prompt”。

Gate 默认要求 mode、subtype、algorithm、toolchain、Prompt/backend fingerprint 兼容，并至少有两条 ranking-eligible 真实记录、来自两个独立 run；配置要求时还必须跨两个 task family。若历史与当前 Evidence 冲突、只来自同族近重复代码、策略已经明确失败或摘要超过 600 token，结果都是 `ABSTAIN`。

`ABSTAIN` 是安全回退，不是系统失败：Planner 会收到原始 Prompt，行为与没有经验层时相同。

## 7. off、shadow、guided 的含义

- `off`：不检索、不生成建议；用于验证与原 V3-D 完全等价。
- `shadow`：真实检索、排名、Gate 和归因，但不改 Planner Prompt；用于安全采集和离线观察。
- `guided`：只有 Gate 输出 INJECT 时，才把短建议加到 Planner Prompt；仍不能影响预算、工具、promote 或 final。

当前真实扩充全部使用 `off` 或 `shadow`，没有用 guided 污染新经验。

## 8. 统计、泛化和学习准备度

固定产物位于 `llm4hls_harness/experiments/v3e/kb_v2/`：

- `experience_v2_backfill.jsonl`：v2 派生记录；
- `experience_v2_data_quality.json`：迁移、排除、一致率、OTHER、family 和正负样本质量；
- `experience_coverage_matrix.json/.csv`：覆盖矩阵；
- `experience_collection_queue.json`：缺口驱动采集队列；
- `experience_generalized_evaluation.json`：四种 leave-one-group-out 结果；
- `datasets/experience/`：四类未来模型数据与 family split；
- `experience_learning_readiness.json`：四类模型是否 READY 的硬条件。

学习准备度只报告事实，不会为了“能训练”降低门槛。数据量或泛化覆盖不足时必须是 `NOT_READY`。

## 9. 常用操作

从仓库根目录执行：

```bash
PYTHONPATH=llm4hls_harness .venv/bin/python -m llm4hls_agent.v3_experience_kb_cli --help

# 校验外部 v2 JSONL；损坏内容只以 digest 进入 quarantine
PYTHONPATH=llm4hls_harness .venv/bin/python -m llm4hls_agent.v3_experience_kb_cli \
  validate --kb-root <kb-root> --input <records.jsonl>

# 基于 frozen snapshot 检索或排名
PYTHONPATH=llm4hls_harness .venv/bin/python -m llm4hls_agent.v3_experience_kb_cli \
  retrieve --kb-root <kb-root> --snapshot <snapshot.json> --query-json <query.json>

# family-group split 导出及 readiness
PYTHONPATH=llm4hls_harness .venv/bin/python -m llm4hls_agent.v3_experience_kb_cli \
  export-ml --records <experience_v2_backfill.jsonl> \
  --generalization <experience_generalized_evaluation.json> \
  --output-root <datasets/experience>
```

这些知识库命令本身不会调用 LLM 或 Vitis；真实 collection 必须显式使用 benchmark/单题运行入口，并使用新的 run 目录。

## 10. 当前验收结果

### 10.1 最终知识库快照

- v1 / v2 记录：103 / 103；
- quarantine：0；
- ranking/retrieval eligible：76；
- eligible mode：OPTIMIZE 26、REPAIR 25、SYNTH_FIX 19、STRUCTURAL_FIX 6；
- eligible outcome：SUCCESS 62、FAILURE 5、NO_IMPROVEMENT 9；
- declared/observed 完全一致率：49.51%；
- `OTHER_*` strategy atom 比例：18.13%。

总量超过最低 60，OPTIMIZE、REPAIR、SYNTH_FIX 达到 mode 目标；STRUCTURAL_FIX 仍缺 9 条。缺口没有用 scripted/golden/Oracle 补齐，已保留在 `experience_collection_queue.json`。

### 10.2 本轮真实 collection

| Batch | 任务结果 | E2E 成功 | Token | Credit | 说明 |
|---|---:|---:|---:|---:|---|
| A01 | 1 | 1 | 8,140 | 40 | OPTIMIZE，67 → 17 cycles，约 3.94×。 |
| A02 | 19 | 15 | 45,828 | 635 | Repair/Synth/Structural；第 20 题真实 XSIM deadlock partial artifact 保留后隔离。 |
| A02B | 8 | 7 | 52,667 | 385 | OPTIMIZE；真实加速包含约 1.025×、1.8×、2.09×、5.15×、8.7×。 |
| A03 | 28 | 24 | 47,443 | 786 | Repair/Synth 双 repeat；含预算失败与 executor error 负例。 |
| A04 | 1 | 0 | 0 | 0 | 外部 Planner dispatch ambiguity，未伪装为 Candidate 经验。 |
| A05 | 1 | 1 | 7,878 | 40 | 补齐 OPTIMIZE mode 覆盖。 |

A02 的长 XSIM deadlock 因没有产生完整 terminal Candidate，按资格规则没有进入可排名 Store；这也是当前 CoSim Risk 只有 64 个 PASS、没有真实 FAIL/TIMEOUT 标签的原因。

### 10.3 泛化评估

Leave-One-Run、Task、Task-Family、Algorithm-Family-Out 都评估了 76 个 query：

- INJECT 0、ABSTAIN 76、coverage 0%；
- harmful recommendation rate 0%；
- family leakage rate 0%；
- 主要回退原因：`NO_RECOMMENDED_STRATEGY` 60、`INSUFFICIENT_SUPPORT` 15、`INSUFFICIENT_TASK_FAMILIES` 1。

这不是“Gate 成功率高”，而是说明当前经验总量虽然够，但 subtype/strategy/family 的有效交叉支持仍不够。项目没有为提高 coverage 降低安全阈值。

### 10.4 ML 导出与 readiness

| 数据集 | Train | Validation | Test |
|---|---:|---:|---:|
| Strategy Ranking | 83 | 6 | 16 |
| CoSim Risk | 44 | 4 | 16 |
| Continue Value | 11 | 1 | 0 |
| Evidence Selection | 55 | 5 | 16 |

四类 readiness 均为 `NOT_READY`：

- Strategy Ranker：Candidate 数够，但 STRUCTURAL_FIX 不足且 Leave-One-Task coverage 为 0；
- CoSim Risk：样本数够，但只有 PASS、没有 FAIL/TIMEOUT；
- Continue Predictor：仅 12 个可识别继续决策，低于 50；
- Evidence Selector：76 个可归因 Planner round，低于 100。

因此当前不得训练复杂模型。下一轮应优先按 collection queue 补真实 STRUCTURAL_FIX、CoSim FAIL/TIMEOUT 和跨 family 正负例。

## Token Budget Policy 正式增补：统一报告第 23～35 项

下面编号沿用 V3-E 最终统一报告的编号，不代表本文缺少第 11～22 章。

### 23. Budget 组件内部结构

Token Policy 没有成为新 Agent、Controller 大组件或 LangGraph 节点，而是留在现有 Budget 横向组件内部：

```text
budget.py
├─ BudgetLedger          真实发生量、硬预算、append-only 与幂等
├─ BudgetConfig          Run Token/Credit/工具次数/时间硬上限
├─ TokenEstimator        当前 Planner 请求的保守分项估算
├─ TokenBudgetLimits     mode cap、context、reserve、Guidance 配置
├─ TokenBudgetPolicy     本轮配额与 pressure 的确定性计算
└─ TokenEnvelope         v3.token-envelope.v1，不可变、可 hash 的本轮结果
```

`BudgetLedger.total_tokens` 继续等于 Provider 报告的 `input_tokens + output_tokens`。Estimator 数值只用于调用前 reserve，绝不冒充实际 usage。Token 和 Vitis Credit 始终分别报告。

### 24. TokenEstimator 实现方式

`TokenEstimator.for_model()` 优先使用模型对应的 `tiktoken` encoding；模型未知时使用 `cl100k_base` 并记录误差来源；环境没有 tokenizer 时回退到确定性的 `unicode-codepoint-upper-bound/v1`。Fallback 按 Unicode code point 计数，方向偏保守，不保存 kernel、Prompt、Guidance 或 API key，只输出每个组件的整数计数和 estimator 身份。

Planner Adapter 对最终实际 `messages` 再估算一次，并把总数分解到：kernel、headers、description、Evidence、history、Experience Guidance、Token Budget 与固定模板。分项之和必须严格等于总估算。

### 25. TokenBudgetPolicy 分配公式

当前实现采用：

```text
remaining = run_token_limit - tokens_used

run_output = remaining
  - estimated_input_tokens
  - future_round_token_reserve
  - final_token_reserve
  - token_safety_margin

context_output = context_window_tokens
  - estimated_input_tokens
  - context_safety_margin_tokens

effective_max_output_tokens = min(
  mode configured cap,
  global configured cap（若设置），
  Provider hard cap,
  max(0, run_output),
  max(0, context_output)
)
```

如果有效输出低于 minimum viable、输入越过 context、只剩 final token reserve，或原有 Budget gate 已禁止继续，则 `planner_call_allowed=false`。系统复用现有预算条件边进入 final/FAILED，没有新增 Graph 节点。

### 26. 各 mode configured/effective max output

| Mode | 默认 configured cap | 默认 minimum viable | 实际 effective 还受什么限制 |
|---|---:|---:|---|
| REPAIR | 1,400 | 700 | Provider、Context、Run 剩余、future/final reserve |
| SYNTH_FIX | 1,800 | 800 | 同上 |
| STRUCTURAL_FIX | 2,200 | 1,000 | 同上 |
| OPTIMIZE | 2,400 | 1,000 | 同上 |

CLI 兼容入口默认仍是 `--token-budget-policy fixed`。启用动态策略使用 `--token-budget-policy dynamic`。Live Run 默认 `run_token_limit=32768`，scripted 默认 4096；`--run-token-limit` 的优先级高于旧 `--token-budget`。当前 Provider hard cap 默认来自 `--llm-max-output-tokens=1000`，因此在预算充足且未覆盖该参数时，各 mode 的 effective 上限最多为 1000。

测试中的动态 REPAIR 请求配置上限为 400、Provider hard cap 为 512，最终 Prompt、Prepared action 和真实 HTTP body 三处均为 400。这里是 mock Provider 工程证据，不冒充真实外部模型运行。

### 27. Prompt 与 Provider max output 一致性

最终 `TokenEnvelope` 会进入 Prompt 的 `TOKEN BUDGET` 区块；`Maximum output for this request` 与 Provider HTTP body 的 `max_tokens` 都直接读取同一个 `effective_max_output_tokens`。Planner request Artifact 另外保存 configured、effective 和 Provider 参数名。

因为 Envelope 自身也会改变 Prompt 长度，Adapter 最多进行 8 次确定性 fixed-point 计算；只有“最终请求的估算值”和“该请求携带的 Envelope”完全稳定才允许 dispatch，否则在 Provider 调用前失败。不会出现 Prompt 告诉模型 1800、API 实际只给 900 的情况。

### 28. Experience Guidance 动态 Token cap

Guidance 硬上限仍为 600，但每轮实际 cap 还受到可用 Context、Run Token、默认 15% ratio 和 Token pressure 限制：MEDIUM 再减半，HIGH 再缩到四分之一，CRITICAL 为 0；Quality Gate ABSTAIN 或 Experience off 时为 0。Guidance 生成后必须重建 Prompt、重新估算并重新分配输出预算。

动态 cap 小于可用 Guidance 最小结构时，Quality Gate 返回 `ABSTAIN/PROMPT_TOKEN_LIMIT`，原 Planner fail-open 继续，不会因为经验不足阻塞 HLS 主流程。`shadow` 仍不向模型注入建议，`guided` 也只能在 cap 内注入。

### 29. Token Estimator 误差

运行报告已经按轮计算 `actual_input_tokens - estimated_input_tokens`，并汇总平均误差。当前分支还没有新的真实外部模型 dynamic run，因此没有足够样本给出真实误差分布；不能用 mock usage 宣称 estimator 准确。Provider usage 缺失时 actual 字段保持 `null/UNKNOWN`，不会用估算值补写。

### 30. 截断率和截断原因

Provider 层当前识别：`PROVIDER_LENGTH_LIMIT`、`JSON_INCOMPLETE`、`PATCH_INCOMPLETE`、`CONTEXT_LIMIT` 和 `UNKNOWN_TRUNCATION`。本次工程测试覆盖了五种路径；真实 dynamic run 样本数为 0，所以真实截断率暂不可计算。

疑似截断时系统会保存原始 Provider excerpt、finish reason、精确 usage（若 Provider 提供）和失败分类；不创建 Candidate、不运行 CSim/Synth/CoSim，也不静默重试。已有精确 usage 的失败可安全 checkpoint replay 且不重复调用；usage 缺失则按既有非重放协议记作 ambiguous conservative。

### 31. 各 mode 成功运行所需输出 Token 分布

Experience v2 和 ML schema 已具备按 mode 统计 `actual_output_tokens` 的字段，但历史 103 条记录生成时尚未启用 `v3.token-policy.v1`，不能把旧 `total_tokens` 伪装成动态策略结果。因此当前四个 mode 的“真实 dynamic 成功输出分布”均标记为 `NOT_YET_COLLECTED`。后续公开 train/dev collection 会直接从 hash-bound Planner request/outcome 解析 Envelope 与实际 usage。

### 32. Token Policy 是否减少无效调用

确定性测试已证明两类无效调用会被提前消除：低于 minimum viable 时不 dispatch Planner；Provider 输出截断/Schema incomplete 时不物化 Candidate，也不调用 Vitis。尚未运行同模型、同题、同配置的 A/B/C 真实对照，因此不能宣称 Token、Credit 或比赛分数获得统计改善。

本阶段工程验收口径如下：

| 组 | 配置 | 覆盖 | 工程结果 | 不能据此宣称的内容 |
|---|---|---|---|---|
| A | fixed max output | 原有 task-aware/optimize/Experience 回归 | PASS，默认入口参数级兼容 | 动态策略优于固定策略 |
| B | dynamic + Experience off | 四 mode cap/pressure、两阶段 REPAIR 请求、Prompt/API 一致性 | PASS | 真实模型成功率或 PPA 提升 |
| C | dynamic + shadow/guided 约束 | 动态 Guidance cap、ABSTAIN、off/shadow/guided 权限边界 | PASS | Guidance 有因果收益 |

这些是公开 fixture/mock 的工程验收，不使用 hidden-like 外部模型，不产生新的 Vitis 成绩结论。

### 33. Token 字段是否进入 Experience v2

已进入。`token_policy` 保存 Run/Context/reserve、base/guidance/final estimate、configured/effective/actual、pressure、finish/truncation 和 estimator/version。新 Run 的 v1 Candidate 仍保持不可变；派生 v2 时，Resolver 通过 proposal round 找到 hash-bound Planner started/request/outcome，读取真实 Envelope 和 usage。旧 v2 记录继续兼容读取，不重写旧 record hash。

### 34. Token 数据是否满足 Continue Predictor 准备条件

字段准备已经完成：Continue 数据包含调用前剩余 Token、effective output、future reserve、remaining rounds、pressure、本轮实际 Token，以及下一轮是否 improvement 和后续 Token/Credit 成本。Strategy、CoSim Risk、Evidence Selection 也分别获得成本/辅助/Context 占比字段。

数据准备不等于模型 READY。当前 Continue Value 仍只有 12 个可识别决策，低于 50 条门槛；新的 dynamic policy 真实样本还是 0，因此 Continue Predictor 仍为 `NOT_READY`。

### 35. Token Policy 测试结果

截至本次分支验收：

- Token Policy/Provider/Planner/Experience/ML 聚焦测试：78 项通过；
- Experience Resolver 与 schema/ML 增补聚焦测试：43 项通过；
- package-aware 完整 unittest：506 项通过；
- 默认 fixed、Experience off/shadow/guided、Candidate promote、CoSim gate、Checkpoint、fresh final 均保留原回归结果；
- Token Policy 新增主 Graph 节点数：0；
- `compileall`：通过；`git diff --check`：通过。

可复现命令：

```bash
cd llm4hls_harness
PYTHONPATH=. ../.venv/bin/python -m unittest discover

# 同时兼容 tests 中两种 package import 的仓库根目录命令
cd ..
PYTHONPATH=.:llm4hls_harness .venv/bin/python -m unittest discover \
  -s llm4hls_harness/tests -t .

PYTHONPATH=llm4hls_harness .venv/bin/python -m compileall -q \
  llm4hls_harness/llm4hls_agent llm4hls_harness/tests
git diff --check
```

最终结论：Token Policy 没有改变主 Graph；它是现有 Budget 横向组件内部能力。它只决定“这一轮 Planner 最多可以看/写多少 Token”，不能提高预算、选择 HLS 策略、批准工具、晋升 Candidate 或绕过 fresh final。
