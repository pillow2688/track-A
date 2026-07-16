# V1 三类错误验收补全设计

状态：待用户审阅  
日期：2026-07-16  
范围：`llm4hls_harness` 内部里程碑 V1

## 1. 背景与目标

总设计规范要求 V1 形成“至少三类错误能修复或安全回滚”的证据。当前只有一类完整真实证据：`FUNCTIONAL_MISMATCH` 由 `deepseek-v4-pro` 生成 Patch，候选通过真实 Vitis 2025.2 的 csim、synth、cosim 和最低时钟约束。

本设计采用方案 A，补齐：

1. `COMPILE_ERROR`：真实模型修复并通过三级 Vitis；
2. `PATCH_INVALID`：非法 Patch 在候选物化前被拒绝，并安全保持 baseline/best；
3. 与已有 `FUNCTIONAL_MISMATCH` 一起构成 V1 三类验收矩阵。

本设计不进入 V2 候选/PPA 循环，不增加 synth-only 修复或 cosim deadlock 修复。

## 2. 验收矩阵

| 类别 | Provider | 预期结果 | 必要证据 |
|---|---|---|---|
| `FUNCTIONAL_MISMATCH` | OpenAI-compatible DeepSeek V4 Pro | 修复成功 | 现有 `runs/v1-deepseek-final`，LLM Token usage 完整，csim/synth/cosim PASS |
| `COMPILE_ERROR` | OpenAI-compatible DeepSeek V4 Pro | 修复成功 | baseline csim=`compile_error`；新候选通过 csim/synth/cosim、clock；Token/credits 完整 |
| `PATCH_INVALID` | deterministic malicious fixture | 安全拒绝 | `PATCH_INVALID`；无候选源码物化；baseline hash 不变；best/final 不被污染；trace/ledger/报告完整 |

三项全部满足后，readiness 才可标记 V1 三类错误验收完成。

## 3. Compile Repair 任务

新增公开任务 `examples/u55c_compile_repair_task/`，遵循现有 task package 结构：

```text
task.toml
description.md
kernel.cpp
kernel.h
kernel_tb.cpp
```

任务使用 U55C、Vitis 2025.2、10 ns/100 MHz 最低约束。baseline 仅包含一个局部、明确、可泛化的 C++ 编译错误；错误不得修改函数接口、测试平台或任务元数据，也不得依赖 hidden/reference 内容。

推荐错误为函数体内使用一个未声明的局部标识符，例如正确表达式应使用现有参数 `b[i]`，baseline 却引用 `rhs[i]`。这样可确保：

- Vitis csim setup 在编译阶段失败；
- 诊断分类为 `COMPILE_ERROR`；
- SourceLocalizer 可从编译日志提取相关行；
- 最小正确 Patch 只修改 kernel `.cpp` 一行；
- 修复后语义明确且可由公开 testbench 验证。

真实验收命令必须使用 API Provider，不允许 `--provider static` 或 `--allow-deterministic-fallback`。成功候选必须满足：

```text
candidate.provider == openai-compatible
v1_acceptance.accepted == true
input/output Token > 0
token_usage_complete == true
csim == PASS
synth == PASS
cosim == PASS
clock_constraint.passed == true
```

## 4. Invalid Patch 安全回滚

新增确定性恶意 Patch fixture，首选修改 `kernel_tb.cpp`，因为 reference contract 明确禁止修改测试平台。该场景不调用外部模型，目的是稳定验证 Patch 安全边界，而不是评价模型能力。

执行路径：

```text
baseline failure
  -> deterministic provider returns forbidden-path Patch
  -> PatchValidator rejects before materialization
  -> status=FAILED, stop_reason=PATCH_INVALID
  -> rollback candidate_000 -> candidate_000
```

硬性断言：

- `candidates/candidate_001/source/` 不存在；
- baseline kernel SHA-256 与任务原文件相同；
- `best_candidate_id` 和 `final_candidate_id` 不指向失败候选；
- Patch、错误原因、provider action、Token/credit 和 trace 可审计；
- 不运行候选 csim/synth/cosim，不产生对应额外 credits；
- 生成 `experimental_report.md`，明确显示 `PATCH_INVALID` 和安全回滚。

## 5. 数据与报告

每个验收运行保留：

- `v1_result.json`；
- `experimental_report.md`；
- `candidate_registry.json`；
- `budget_ledger.jsonl` 与 `budget_state.json`；
- `trace.jsonl`；
- `llm_actions/` 或 deterministic provider result；
- Vitis action result、Tcl、stdout/stderr、XML 和报告。

实验报告必须展示：

- error class、baseline failure phase；
- candidate provider/model；
- LLM-based V1 acceptance；
- input/output/cached-input/total Token；
- Tool calls、unit cost 和 credits 分解；
- csim/synth/cosim 状态；
- clock、latency、II 和资源（若完成 synth）；
- rollback 与候选污染检查。

## 6. 测试策略

快速测试新增：

1. compile fixture 能被 task loader 加载；
2. fake Vitis baseline `compile_error` 被分类为 `COMPILE_ERROR`；
3. 合法编译修复 Patch 创建隔离候选并晋级；
4. 修改 testbench 的 Patch 返回 `PATCH_INVALID`；
5. invalid Patch 不物化 candidate_001；
6. baseline/best/final 不被污染；
7. invalid Patch 的 ledger、trace 和报告对账；
8. 三类验收矩阵的 readiness 汇总逻辑不会把单元测试冒充真实 Vitis 证据。

真实工具验收新增一次 compile repair API/Vitis 运行。非法 Patch 使用确定性 fixture，避免依赖模型随机生成越权内容。

## 7. 停止原因与状态

Compile repair：

- Provider/格式失败：`LLM_REPAIR_FAILED`；
- Patch 越权或不可应用：`PATCH_INVALID`；
- 候选验证失败：对应 `CSIM_*`、`SYNTH_*`、`COSIM_*`；
- 全部验证成功：`CANDIDATE_VERIFIED`。

Invalid Patch：

- 必须终止为 `FAILED/PATCH_INVALID`；
- 这是预期的安全验收成功，但不能把 workflow 状态伪造为 `DONE`；
- readiness 通过独立验收矩阵记录该安全场景已满足。

## 8. 文档与复盘

实现完成后同步更新：

- `README.md` 与 `README_CN.md`；
- `examples/README.md` 与 `examples/README_CN.md`；
- `doc/materials/07_experiments/2026-07-15-v1-errors-and-v2-readiness.md`；
- 三类验收结果摘要和复现命令。

任何新问题都按材料库规则追加“现象、根因、修复、验证、防复发、里程碑状态”复盘。

## 9. 完成标准

只有以下条件全部成立，才宣布 V1 三类错误验收完成：

- 现有 functional mismatch 真实 LLM/Vitis 证据仍有效；
- compile error 由真实 DeepSeek Patch 修复并通过三级 Vitis；
- invalid Patch 在物化前被拒绝且 baseline/best/final 未被污染；
- Token、credits、工具次数和报告对账一致；
- 快速测试、compileall、`git diff --check` 通过；
- readiness 文档更新为三类全部完成，不再把单类证据表述为整个 V1 完成。
