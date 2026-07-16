# V1 三类 HLS 错误与统一验收设计

状态：用户已确认，实施中

日期：2026-07-16

范围：`llm4hls_harness` 内部里程碑 V1

## 1. 背景与目标

总设计规范要求 V1 形成“至少三类错误能修复或安全回滚”的可复现证据。当前只有一类完整真实证据：`FUNCTIONAL_MISMATCH` 由 `deepseek-v4-pro` 生成 Patch，候选通过真实 Vitis 2025.2 的 csim、synth、cosim 和最低时钟约束。

V1 补全以下三类 **HLS 任务错误**：

1. `FUNCTIONAL_MISMATCH`：沿用现有真实 LLM/Vitis 证据；
2. `COMPILE_ERROR`：新增真实 DeepSeek 修复与三级 Vitis 验证；
3. `SYNTHESIS_ERROR`：新增“csim 通过、synth 失败”的真实 DeepSeek 修复与三级 Vitis 验证。

`PATCH_INVALID` 不属于 HLS 任务错误，不计入上述三类。它是独立的平台安全负例，用来证明非法 Patch 在 Candidate 物化前会被拒绝，且 baseline、best、final 不受污染。

`COSIM_FAILURE` 明确留到 V2。本设计不进入 V2 的多候选/PPA 优化闭环。

## 2. 验收矩阵

### 2.1 HLS 任务错误矩阵

| HLS 错误类别 | Provider | Baseline 必须满足 | Candidate 必须满足 |
|---|---|---|---|
| `FUNCTIONAL_MISMATCH` | OpenAI-compatible DeepSeek V4 Pro | csim=`functional_mismatch` | LLM Patch；csim/synth/cosim PASS；clock PASS |
| `COMPILE_ERROR` | OpenAI-compatible DeepSeek V4 Pro | csim=`compile_error` | LLM Patch；csim/synth/cosim PASS；clock PASS |
| `SYNTHESIS_ERROR` | OpenAI-compatible DeepSeek V4 Pro | csim PASS；synth=`synthesis_error` | LLM Patch；csim/synth/cosim PASS；clock PASS |

三项都必须有真实 API、真实 Vitis 2025.2、完整 Token/credits 和可校验证据。fake Vitis、static provider、确定性 fallback、单元测试结果不能冒充这三项验收。

### 2.2 独立安全负例

| 安全场景 | Provider | Workflow 预期 | 安全验收预期 |
|---|---|---|---|
| `PATCH_INVALID` | deterministic malicious fixture | `FAILED/PATCH_INVALID` | PASS：无新 Candidate；baseline/best/final 不变；无候选 Vitis credits |

安全负例通过不改变 workflow 的失败状态。统一验收器单独记录 `safety.patch_invalid=PASS`。

## 3. 新增 HLS 修复任务

### 3.1 Compile Repair

新增公开任务 `examples/u55c_compile_repair_task/`，结构与现有 task package 一致：

```text
task.toml
description.md
kernel.cpp
kernel.h
kernel_tb.cpp
```

任务使用 U55C、Vitis 2025.2、10 ns/100 MHz 最低约束。baseline 只包含一个局部、明确的 C++ 编译错误，不修改函数接口、testbench 或任务元数据，也不依赖 hidden/reference 内容。

首选错误是函数体内使用未声明标识符：正确表达式应使用现有参数 `b[i]`，baseline 却引用 `rhs[i]`。预期性质：

- Vitis csim 在编译阶段失败并分类为 `COMPILE_ERROR`；
- SourceLocalizer 能从真实日志定位相关行；
- 最小正确 Patch 只修改 kernel `.cpp`；
- 修复后由公开 testbench 验证语义。

### 3.2 Synthesis Repair

新增公开任务 `examples/u55c_synthesis_repair_task/`。它必须满足以下阶段边界：

```text
baseline csim PASS
baseline synth SYNTHESIS_ERROR
```

首选 fixture 在 kernel 内使用 C/C++ 软件执行合法、但 Vitis HLS 不支持综合的动态分配，例如固定长度 `new[]/delete[]`；目标修复是等价的固定大小局部数组。该错误不能影响 testbench、函数接口或预期输出。

fixture 合入前必须用项目固定的真实 Vitis 2025.2 配置做 preflight：

1. csim 必须 PASS；
2. synth 必须稳定失败；
3. 诊断必须分类为 `SYNTHESIS_ERROR`；
4. 等价固定数组修复必须通过 csim、synth、cosim 和 clock。

若 `new[]/delete[]` 在当前 Vitis 配置下不能稳定满足这四条，该 fixture 必须被拒绝并换成另一个经同样 preflight 证明的 synthesis-only 构造；不得通过伪造错误类别或测试 mock 放宽条件。

### 3.3 两个新任务的真实验收约束

真实验收命令必须使用 API Provider，不允许 `--provider static` 或 `--allow-deterministic-fallback`。每个成功候选都必须满足：

```text
candidate.provider == openai-compatible
candidate.model == deepseek-v4-pro
v1_acceptance.accepted == true
input_tokens > 0
output_tokens > 0
token_usage_complete == true
csim == PASS
synth == PASS
cosim == PASS
clock_constraint.passed == true
```

## 4. Patch 与 Candidate 生命周期

Provider 返回的是 **Patch proposal**，不是 Candidate。执行顺序固定为：

```text
provider proposal
  -> parse/normalize
  -> path and policy validation
  -> dry-run apply
  -> validated Patch
  -> allocate candidate_id
  -> materialize isolated candidate source
  -> reset validation
  -> register Candidate
  -> Vitis validation
```

硬性不变量：

- Patch 验证成功前，不分配新的 `candidate_id`；
- Patch 验证成功前，不创建 `candidates/candidate_001/` 等 Candidate 目录；
- Patch 验证成功前，不向 Candidate Registry 写入候选记录；
- proposal 可用独立的 `action_id` 审计，但 `action_id` 不能冒充 `candidate_id`；
- dry-run 只能使用内存或 Candidate 树之外的临时 staging，成功后才原子地物化 Candidate，失败时必须清理 staging；
- reset validation 在物化后执行；失败候选可以留存审计，但不得晋级为 best/final。

因此 `PATCH_INVALID` 路径为：

```text
provider proposal
  -> parse/policy/dry-run failure
  -> FAILED/PATCH_INVALID
  -> no candidate allocation or materialization
  -> baseline/best/final unchanged
```

安全 fixture 首选尝试修改 `kernel_tb.cpp`，因为 reference contract 明确禁止修改测试平台。它不调用外部模型，以保证负例稳定；它验证的是平台边界，不是模型能力。

## 5. Artifact Manifest

每个运行生成机器可读的 `artifact_manifest.json`，作为统一验收器唯一认可的证据索引。Manifest 使用版本化 schema，条目按稳定键排序，禁止绝对路径和密钥。

顶层至少包含：

- `schema_version`；
- `run_id`、`task_id`；
- `baseline_candidate_id`、`best_candidate_id`、`final_candidate_id`；
- `provider`、`model`、`toolchain_version`；
- `code_hash`、`tool_config_hash`；
- `artifacts[]`。

每个 artifact 条目至少包含：

- `artifact_type`；
- 相对 run directory 的 `path`；
- `sha256` 和 `size_bytes`；
- `producer` 与关联 `action_id`；
- 可选 `candidate_id`；
- `required` 标志。

Manifest 覆盖：任务/run 配置、baseline 源码快照、合法 Patch proposal、Candidate 源码、LLM action 结果、各阶段 action result、Vitis Tcl/stdout/stderr/XML/report、ledger、registry、trace、最终结果和实验报告。敏感请求头、API key 和未经脱敏的环境变量不得进入 Manifest 或其索引文件。

为避免自引用循环：Manifest 不列出自身，也不列出随后生成的 `acceptance_result.json`。Manifest 写入后计算自身 SHA-256；Acceptance Evaluator 将该 digest 写入 `acceptance_result.json`。任何 Manifest 内文件缺失、大小变化或 hash 不匹配都必须 fail closed。

对于 `PATCH_INVALID`，Manifest 记录 proposal、校验错误、ledger、trace、result 和 report，但不得包含新的 Candidate 源码或候选 Vitis artifact。

## 6. 统一确定性 Acceptance Evaluator

新增一个普通 Python 服务 `AcceptanceEvaluator`。它不调用 LLM、不调用 Vitis、不修改四个证据运行目录，只对已生成证据做确定性评估。同一输入重复执行必须产生语义一致、稳定排序的结果。验收输出写入单独的 acceptance output directory。

输入：

- 版本化 V1 acceptance specification；
- 三个 HLS 真实运行目录；
- 一个 `PATCH_INVALID` 安全运行目录；
- 各运行的 `artifact_manifest.json`。

校验内容：

1. Manifest schema、必需条目、路径边界、size/hash；
2. task/error class 与 baseline 阶段结果匹配；
3. provider/model 是规定的真实 API 配置，不是 static/fallback；
4. LLM Token 使用量非零且 usage 完整；
5. csim/synth/cosim 与 clock 的 Candidate 结果全部通过；
6. Tool calls、unit cost、credits 和总计可从 ledger 对账；
7. baseline hash 不变，best/final 指向合法通过候选；
8. `PATCH_INVALID` 没有 Candidate 分配、目录、registry 记录或候选 Vitis credits；
9. fake/static/unit evidence 不得满足 real acceptance 标志。

输出 `acceptance_result.json`，至少包含：

- acceptance spec version、spec digest 与 evaluator version；
- 每个 case 的 `PASS/FAIL`、稳定 reason code 和 evidence refs；
- 三类 HLS 矩阵结果；
- 独立 safety 结果；
- 四个 Manifest digest；
- `overall_status`。

`overall_status=PASS` 的必要且充分条件是：三类 HLS 任务错误全部 PASS，且 `PATCH_INVALID` 安全负例 PASS。任何证据缺失、损坏、互相矛盾或不可对账都返回 FAIL，不允许“信息不足但通过”。

## 7. 数据、报告与复现

每个验收运行至少保留：

- `v1_result.json`；
- `experimental_report.md`；
- `artifact_manifest.json`；
- `candidate_registry.json`；
- `budget_ledger.jsonl` 与 `budget_state.json`；
- `trace.jsonl`；
- `llm_actions/` 或安全 fixture action result；
- Vitis action result、Tcl、stdout/stderr、XML 和报告。

实验报告必须展示：

- error class、baseline failure phase；
- candidate provider/model 与是否为 LLM 生成；
- LLM-based V1 acceptance；
- input/output/cached-input/total Token；
- Tool calls、unit cost 和 credits 分解；
- csim/synth/cosim 状态；
- clock、latency、II 和资源（完成 synth 时）；
- rollback、Candidate 生命周期和污染检查；
- Manifest 预定路径；最终 digest 由 `acceptance_result.json` 记录。

统一验收完成后生成矩阵摘要，链接四个运行的报告、Manifest 和 `acceptance_result.json`，并给出完整复现命令。

## 8. 测试策略

快速测试新增：

1. compile 与 synthesis fixture 均能被 task loader 加载；
2. fake Vitis 只验证分类/控制流，不计作真实验收；
3. baseline `compile_error` 分类为 `COMPILE_ERROR`；
4. baseline csim PASS、synth failure 分类为 `SYNTHESIS_ERROR`；
5. Patch proposal 通过 parse/policy/dry-run 后才分配并物化 Candidate；
6. 修改 testbench 的 Patch 返回 `PATCH_INVALID`，且没有 candidate_001 ID、目录或 registry 记录；
7. baseline/best/final 不被非法 Patch 污染；
8. Manifest 稳定排序并能检测缺失、越界和 hash 篡改；
9. Acceptance Evaluator 对相同输入确定性输出；
10. Evaluator 对 Token=0、fallback、fake Vitis、credits 不一致、阶段错误或证据篡改 fail closed；
11. readiness 汇总不会把安全负例描述成第三类 HLS 错误。

真实集成验收新增：

1. compile fixture 的真实 Vitis baseline preflight；
2. synthesis fixture 的真实 Vitis 阶段边界 preflight；
3. 两次真实 DeepSeek 修复及 Candidate 三级 Vitis 验证；
4. 四个 case 的统一 Acceptance Evaluator 运行。

## 9. 停止原因与状态

HLS repair：

- Provider/格式失败：`LLM_REPAIR_FAILED`；
- Patch 越权或不可应用：`PATCH_INVALID`；
- 候选验证失败：对应 `CSIM_*`、`SYNTH_*`、`COSIM_*`；
- 全部验证成功：`DONE/CANDIDATE_VERIFIED`。

安全负例：

- workflow 必须终止为 `FAILED/PATCH_INVALID`；
- Acceptance Evaluator 可将该安全 case 判为 PASS；
- 两个状态处于不同层级，不得把 workflow 伪造成 `DONE`。

## 10. 文档与复盘

实现完成后同步更新：

- `llm4hls_harness/README.md` 与 `README_CN.md`；
- `examples/README.md` 与 `README_CN.md`；
- `doc/materials/07_experiments/2026-07-15-v1-errors-and-v2-readiness.md`；
- 三类 HLS 验收矩阵、安全负例、Manifest 和统一验收结果；
- 四个 case 的复现命令。

任何新问题都按材料库规则追加“现象、根因、修复、验证、防复发、里程碑状态”复盘。`COSIM_FAILURE` 在 readiness 中只标记为 V2 待办，不能写成 V1 已验证。

## 11. 完成标准

只有以下条件全部成立，才宣布 V1 三类 HLS 错误验收完成：

- `FUNCTIONAL_MISMATCH`、`COMPILE_ERROR`、`SYNTHESIS_ERROR` 都有真实 DeepSeek Patch 与真实 Vitis 三级通过证据；
- 三类运行都满足非零、完整 Token usage，credits 与工具调用可对账；
- `PATCH_INVALID` 在 Patch 验证阶段被拒绝，未分配或物化新 Candidate，且 baseline/best/final 未被污染；
- 四个运行的 Artifact Manifest 完整且 hash 校验通过；
- 统一 Acceptance Evaluator 返回 `overall_status=PASS`；
- 快速测试、真实集成验收、compileall 和 `git diff --check` 通过；
- readiness 文档按三类 HLS 错误与独立安全负例分别更新；
- `COSIM_FAILURE` 未被误报为 V1 范围。
