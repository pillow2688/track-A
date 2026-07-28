# LLM4HLS Agent Development Rules

This file applies to the entire `llm4hls_harness/` tree.

## 文档语言约定

- 所有新增或实质修改的用户文档都必须提供简体中文内容。
- 如果英文与中文使用独立文件，中文文件采用 `_CN.md` 后缀，并在两个版本顶部添加双向链接。
- 同一次变更必须同步更新中英文版本，避免命令、配置、验收结果或风险说明不一致。
- 面向用户的最终交付说明默认使用中文。

## Mission

Build a budget-aware LangGraph agent for FPT 2026 Track A. The system must repair or optimize an HLS kernel, validate it with real Vitis results, preserve the best verified candidate, and stop within configurable Token, tool, credit, and time budgets.

Correctness has priority over PPA. Reproducibility has priority over a convincing-looking demo.

## Read Before Editing

Read these sources in order before making architectural changes:

1. `../doc/materials/01_official/2026-07-10-track-a-submission-guidelines-zh.md`
2. `../doc/materials/04_agent_basics/2026-07-17-budget-aware-langgraph-llm4hls-agent-design.md`
3. `../doc/materials/02_harness/2026-07-10-reference-harness-analysis.md`
4. `../_external/fpt26-harness/README.md`
5. Relevant code under `../_external/fpt26-harness/llm4hls/`

Priority when sources disagree:

```text
latest official rule
  > team design contract
  > reference harness behavior
  > local implementation convenience
```

The reference harness is an example and compatibility fixture, not the official scoring specification. Keep uncertain values configurable and document the assumption.

## Scope and Milestones

Implement one runnable vertical slice at a time. Work on the lowest incomplete milestone and do not scaffold all later modules in advance.

### V0: Deterministic Harness

- Load a reference-compatible task package.
- Create an immutable baseline candidate.
- Run metered csim, synth, and cosim.
- Parse structured results and PPA metrics.
- Persist candidate records, budget ledger, trace, and final result.
- Run without an LLM.

### V1: Minimal Repair Loop

- Classify a failure and localize relevant source.
- Request one minimal unified diff.
- Validate and apply the Patch to a new candidate.
- Verify, promote or reject, and roll back safely.

### V2: Candidate and PPA Loop

- Maintain a candidate tree.
- Compare candidates lexicographically: verification, hard constraints, PPA, then cost.
- Attempt one optimization class per round.
- Preserve the best verified candidate.

### V3: Budget-Aware LangGraph

- Use the specified State, nodes, conditional edges, reducers, and checkpointer.
- Enforce search-closeout reserves and stopping rules; mandatory final certification is outside Agent Token/Credit metering.
- Track Token, tool, credit, and wall-time budgets.
- Resume without duplicate side effects or charges.

### V4: Competition Hardening

- Add hidden-like proxy tasks and edge cases.
- Harden cosim/deadlock handling.
- Verify Docker reproduction.
- Produce multi-model experiments, report data, and demo evidence.

## Reference Harness Contract

Treat `../_external/fpt26-harness` as read-only. Do not modify it to make this project pass.

The new project must be self-contained. It may use public reference tasks as development fixtures, but production code must not import from or require `../_external/fpt26-harness` at runtime. Reimplement or adapt the required public interfaces inside this project.

Task packages use:

```text
task.toml
description.md
<kernel>.cpp
<kernel>.h
<kernel>_tb.cpp
optional hidden/<kernel>_tb.cpp
optional reference/<kernel>.cpp
```

Required compatibility rules:

1. The runtime agent may modify only the kernel source supplied to the tool interface.
2. It must not modify headers, testbenches, task metadata, assertions, build scripts, hidden tests, or reference solutions.
3. It must preserve the top function name, signature, argument order, types, interfaces, and specified numerical semantics.
4. Agent-search csim, synth, and cosim must use a metered, audited interface equivalent to `ToolServer`. Final certification must use a separate audited scorer-style path outside the Agent Budget Ledger.
5. It must not call Vitis directly from an LLM tool path to bypass the BudgetManager.
6. Hidden grading is outside the agent loop. Hidden tests and golden answers must never enter prompts or runtime context.
7. `requires_cosim = true` makes cosim part of correctness, not an optional PPA check.
8. The `scripted` backend replays a golden answer and is only a harness smoke test. Its score is not evidence of agent quality.

Do not inspect or copy `tasks/*/reference/*.cpp` when designing repair or optimization behavior. Do not inspect `hidden/` contents. Public task descriptions and public testbenches are allowed inputs.

The reference defaults are development configuration, not confirmed final rules:

```text
csim cost: 1 credit
synth cost: 4 credits
cosim cost: 20 credits
example development token fallback: 32768
```

These are reference-harness development defaults, not organizer-confirmed limits. Task-level limits take precedence: `max_tokens` overrides the Token fallback and `max_credits` overrides any development Credit fallback. A run-level override may only narrow a task-level limit unless the task format explicitly authorizes otherwise. If a task omits either limit, the run may use a configurable development fallback, but it must record the selected value, source, and fallback assumption in its trace and experimental report. Missing limits must never silently become unlimited. All per-tool costs and fallback values must be overridable by config or environment.

## Official Scoring Contract

The team-provided official `scoring.py` is the executable scoring authority for current development. If a newer organizer-issued scorer or written rule conflicts with this section, update the implementation, tests, and this file together.

Current scoring behavior:

```text
functional_pass = hidden_csim_pass and (hidden_cosim_pass if requires_cosim else true)
acceleration = baseline_latency / candidate_latency
ppa_norm = min(acceleration, 8.0) / 8.0
score = difficulty * (0.5 * correct + 0.2 * synthesizable + 0.3 * ppa_norm)
```

- Hidden functional failure is an absolute gate: `score = 0`, regardless of synthesis or PPA.
- For `requires_cosim = true`, hidden CoSim is part of `functional_pass`; otherwise the scorer does not run CoSim.
- Acceleration and PPA credit exist only when functional correctness passes, candidate synthesis passes, and both baseline and candidate latency are available.
- The latency baseline is the task's original starting kernel synthesized under the same task part and clock, not the best public candidate or a hand-tuned reference.
- `is_opt` means strictly `acceleration > 1.0`; acceleration above `8x` earns no additional PPA credit.
- LUT, FF, DSP, and BRAM are recorded by the supplied scorer but do not directly enter its current numeric formula. Never trade correctness for an unscored resource reduction.
- Token consumption is identified by the submission guideline as an important final-evaluation factor, but its numeric weight is not defined in the supplied scorer. Report it faithfully and do not invent a combined score.
- The project-wide final-certification rule remains deliberately stricter than the scorer: every reported final candidate must pass public CSim, Synth, CoSim, and the 100 MHz gate, even when hidden CoSim is conditional.

## Toolchain Contract

Development target:

```text
Vitis: 2025.2
command: vitis-run --mode hls
part: xcu55c-fsvh2892-2L-e
development clock: 5.0 ns / 200 MHz
minimum submitted hardware frequency: 100 MHz
WSL install root: /opt/xilinx/2025.2/Vitis
```

Do not hardcode machine-specific Windows paths. Support at least:

```text
LLM4HLS_VITIS_HLS_ROOT
LLM4HLS_PART
LLM4HLS_CLOCK_NS
LLM4HLS_TOKEN_BUDGET
LLM4HLS_COST_CSIM
LLM4HLS_COST_SYNTH
LLM4HLS_COST_COSIM
```

Use timeouts for every subprocess. Preserve stdout, stderr, Tcl, XML, and report artifacts for audit. Never fabricate a pass or PPA metric when a report is missing.

## Correctness and Candidate Safety

These are hard invariants:

1. Baseline is read-only.
2. Every Patch creates a new candidate directory.
3. Every validation result is bound to `code_hash + tool_config_hash`.
4. Source changes reset downstream validation states to `NOT_RUN`.
5. Failed candidates never overwrite `best_candidate_id`.
6. A PPA gain never outranks a higher correctness tier.
7. Final output must come from a frozen candidate that passes final csim, synth, cosim, and the 100 MHz clock gate.
8. Final certification failure must be reported explicitly. Any repair or alternate-candidate search requires a new metered Agent run with fresh budgets.

Normal validation order:

```text
static -> csim -> synth -> optional exploration cosim
```

Final validation order:

```text
final csim -> final synth -> final cosim -> final 100 MHz gate
```

Run exploration cosim for structural, DATAFLOW, stream, interface, numerical, or explicit `requires_cosim` risk. Do not run it blindly after every low-risk PPA attempt.

## LangGraph Contract

Follow the State and routing contract in the total design specification.

- State stores small structured values and artifact references only.
- Full code, logs, reports, prompts, and chat history stay outside checkpoints.
- Route functions are pure and perform no I/O.
- Nodes return partial State updates.
- Use conditional edges consistently; do not mix a static edge and dynamic routing from the same node.
- Define reducers for nested or accumulating fields. A partial validation update must not erase other stage results.
- Use `thread_id = run_id` with a durable checkpointer.
- Use stable SHA-256 action IDs. Never use Python `hash()` for persistent IDs.
- One LLM or Vitis side effect belongs in one checkpointable node.
- Every loop has a retry cap, budget gate, and explicit stop reason.

Do not add duplicate fields such as both `remaining_budget` and values from which it is derived. Do not hide routing state inside mutable service objects.

## Budget Contract

There is exactly one authoritative append-only Budget Ledger.

Before any charged action:

```text
estimate -> reserve -> write STARTED -> execute
  -> save result -> reconcile actual use -> write COMPLETED
```

Enforce all configured dimensions independently:

- provider-reported input and output Tokens;
- unified credits;
- per-tool call limits;
- wall time;
- repair, optimization, Patch retry, and fallback limits.

Exploration is allowed only when the action remains affordable after preserving configured search-closeout reserves. Mandatory final certification does not consume Agent Token/Credit reserves. Budget denial routes to finalization when an eligible candidate exists, otherwise to `FAILED(NO_VALID_CANDIDATE)`.

Recovery must reuse a completed `action_id` without repeating the call or charge. An ambiguous started action must be recorded and handled conservatively.

## Agent 搜索与最终认证预算边界

以下两项为已确认的项目级硬约束。Agent 搜索与最终认证属于两个独立预算域，不得混合计量、核销或复用为同一轮反馈。

### Agent 搜索阶段

- 受题目级 `max_tokens` 与 `max_credits` 硬约束。
- `max_tokens` 统计 Agent 搜索中全部模型调用的输入与输出 Token，不得与单次请求的 `max_output_tokens` 混淆。
- `max_credits` 统计 Agent 搜索中的计量型工具开销；所有收费动作必须进入唯一的 Agent Budget Ledger。
- 题目提供 `max_credits` 时必须优先采用；运行参数只能收紧该额度，不能静默放宽。题目未提供时才允许使用明确配置的开发回退值，并在 trace、Ledger 配置和实验报告中记录其来源。
- CSim/Synth/CoSim 的 `1/4/20` 只是不确定规则下的可配置开发参考成本，不能写成官方固定收费标准。
- 仅当任务声明 `requires_cosim = true`，或候选存在可审计的结构风险时，才在搜索阶段执行昂贵 CoSim。
- 结构风险包括 DATAFLOW、stream/FIFO、死锁、接口/协议、并发调度以及 RTL/C 模型不一致风险；触发原因必须写入 trace 和 evidence。
- 不满足 CoSim 触发条件时，不得仅为形式完整性对每个搜索候选无条件运行 CoSim。
- 任何名为 `final_reserve_credits` 的旧字段都不得表示最终认证预算。迁移时必须删除它，或将仍有必要的搜索期语义重命名为 `search_closeout_reserve_credits`。
- `search_closeout_reserve_credits` 只能保留 Agent 搜索阶段用于候选确认、失败收口或安全停止的计量型动作预算；它不得包含或估算独立最终认证的成本。

### 最终认证与实验报告

- Agent 选定并冻结 final kernel 后，使用独立的 scorer-style 认证路径统一执行 CSim、Synth、CoSim 和 100 MHz Gate。
- 最终认证不受 Agent 的 `max_tokens` 或 `max_credits` 计量；其开销不得写入或扣减 Agent Budget Ledger。
- Agent 搜索终态必须先冻结并持久化 final `candidate_id`、`code_hash` 和工具配置，认证入口只接受该不可变绑定。
- 100 MHz Gate 要求最终时钟周期不大于 10 ns。缺失、不可解析或不满足阈值的时序证据均不得判定为通过。
- 最终认证必须独立保存 receipt、日志、报告、工具配置和 `code_hash`，并明确标注 certification budget domain。
- 最终认证只验证已经冻结的 final kernel，不得调用 LLM、生成 Patch、切换候选或向同一轮 Agent 搜索回灌免费反馈。
- 认证失败必须如实进入实验报告；若需要修复或重新选择候选，必须开启新的 Agent 搜索轮次并重新应用 `max_tokens`/`max_credits`。

## LLM and Patch Contract

Use deterministic parsing and rules before calling an LLM. The LLM may:

- diagnose a low-confidence failure;
- propose one repair Patch;
- propose one PPA optimization class per round.

The LLM may not approve its own budget, select an unverified final candidate, modify tests, or invoke arbitrary shell commands.

Prompts should contain only localized code, structured evidence, constraints, relevant failed-action memory, and remaining exploration budget. Do not resend the complete repository or raw logs by default.

Model output must be structured data or a unified diff. Validate every Patch before application:

- syntax and dry-run application;
- allowed file and extension;
- changed-line limit;
- no test, hidden, reference, interface, or assertion modification;
- no path traversal;
- no full-file replacement unless explicitly allowed by config.

Use open-weight/open-source models allowed by the competition. Record provider, exact model ID, revision when known, quantization, reasoning mode, Token usage, and date.

## Model Matrix and Current Provider Status

The organizer guideline recommends, but does not state as a hard eligibility requirement, experiments with these three models:

1. `deepseek-ai/DeepSeek-V4-Pro`
2. `cyankiwi/Qwen3.5-122B-A10B-AWQ-4bit`
3. `Qwen/Qwen3.6-27B-FP8`

Current project status is DeepSeek-only. Treat that as an incomplete experiment matrix, not as permission to hardcode DeepSeek-specific behavior into planning, parsing, budgets, or persistence.

- Keep the model client behind the existing provider/model configuration boundary.
- A run counts as evidence for a recommended model only when the actual provider/backend, exact model ID, revision, quantization, context limit, and generation/reasoning settings are recorded. A generic DeepSeek API model must not be labeled `DeepSeek-V4-Pro` unless that exact backend is verified.
- Before claiming multi-model readiness, run the same task set, prompt/policy version, Token/Credit budgets, toolchain configuration, seeds or repeat policy, and acceptance gates for every model.
- Preserve failed and incomplete runs. The technical report must distinguish unavailable model access, infrastructure failure, budget exhaustion, invalid output, correctness failure, and PPA outcome.
- Provider-specific response formats and Token accounting must be normalized at the adapter boundary and covered by focused tests.

## Secrets and Data

- Read API keys from environment variables only.
- Never write keys, passwords, cookies, or account details to source, config, prompts, traces, tests, or reports.
- Keep `.env`, run artifacts, Vitis work directories, model caches, and credentials out of Git.
- Redact secrets from exception text before persistence.

## Engineering Workflow

At the start of a task:

1. Read this file and the relevant specification section.
2. Inspect existing code and tests before proposing abstractions.
3. Identify the smallest incomplete vertical slice.
4. State the acceptance evidence, then implement it end to end.

While coding:

- Prefer existing project patterns and standard structured parsers.
- Keep modules focused and avoid speculative abstractions.
- Do not create the full target directory tree as empty placeholders.
- Add tests with each behavior change, especially routes, budgets, rollback, and resume.
- Keep expensive Vitis integration tests separate from fast unit tests.
- Preserve user changes and avoid unrelated refactors.
- Ask only when an official-rule ambiguity or destructive choice cannot be resolved safely.

After coding:

1. Run the smallest relevant tests.
2. Run the full fast test suite.
3. For toolchain changes, run at least one real Vitis end-to-end case.
4. Check generated ledger, trace, candidate registry, and reports.
5. Run `git diff --check` and review the diff.
6. Report exact commands, outcomes, remaining risks, and the next milestone.

Do not claim csim, synth, cosim, Docker, or recovery success without fresh command output proving it.

## Submission Evidence Contract

The submission guideline expects an experimental report and reproducible materials in addition to source code.

- Run and report final evidence on Alveo U55C (`xcu55c-fsvh2892-2L-e`) with Vitis 2025.2.
- For every reported final candidate, retain CSim, Synth, CoSim, target clock, achieved/estimated clock, latency/II, resource, Token, tool-call, wall-time, stop-reason, and candidate-hash evidence.
- The report must separate official scorer results, stricter project certification results, and Agent-search budget consumption.
- Build and run the submission in Docker from a clean checkout. Include a Dockerfile, pinned dependencies, exact build/run commands, and documented Vitis/license mounting or provisioning.
- Produce a secret-free reproduction archive containing source, permitted testbenches, configuration templates, tests, scripts, report inputs, and one clear entry-point README. Do not package hidden tests, reference solutions, credentials, model caches, Vitis build trees, or unrelated run artifacts.
- The demonstration video must be no longer than five minutes and show the project actually running on the target platform with a clear explanation; slides alone are insufficient evidence.
- Do not claim the submission package, three-model matrix, report, or video complete until each corresponding artifact has been freshly checked.

## V0 Acceptance Evidence

V0 is complete only when fresh runs demonstrate all of the following:

1. `dotProduct_optimize` baseline passes real csim, synth, and cosim.
2. `projection_bugfix` baseline failure is captured as structured evidence without changing fixed files.
3. `residual_stream_deadlock` passes csim and is correctly reported as cosim failure or timeout without hanging indefinitely.
4. Tool charges and Token usage, when applicable, appear in the ledger and trace.
5. Re-running an identical completed action uses the cache or idempotency record without duplicate charging.
6. Baseline source remains byte-for-byte unchanged.
7. One documented CLI reproduces the run.

Do not begin V1 merely because unit tests pass; V0 requires real Vitis evidence.

## Definition of Done for Any Change

A change is done only when:

- behavior is covered by focused tests;
- harness compatibility and hard invariants still hold;
- budgets and loop bounds remain configurable;
- no metered action can bypass the ledger;
- failures produce structured evidence and an explicit route;
- relevant real-tool verification has run when required;
- documentation states how to reproduce the result.
