# V3-E 经验知识库：当前真实架构与组员上手说明

更新时间：2026-07-21
分支：`feat/v3e-experience-kb-hardening`

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

## 4. v2 一条经验包含什么

`v3e.experience.v2` 只保存可检索、可审计的结构化事实：

- `source`：匿名 run/candidate、真实 provider/model、Prompt/toolchain 版本、split、family hash；
- `problem`：mode、failure subtype、bottleneck subtype、数值语义和 CoSim 要求；
- `structure_features`：循环、II、TripCount bucket、transaction interval、pipeline/dataflow/stream/FIFO、内存和资源压力；
- `strategy`：declared bundle、observed atoms、归一化置信度、Patch 复杂度和 digest；
- `validation`：Patch/interface guard、CSim/Synth/CoSim/final、promote/reject；
- `performance`：前后 latency/interval、acceleration、resource delta；
- `cost`：Token、Credit、工具次数和 wall time；
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
