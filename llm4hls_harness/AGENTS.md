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
2. `../doc/materials/04_agent_basics/2026-07-14-budget-aware-langgraph-llm4hls-agent-design.md`
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
- Enforce final verification reserves and stopping rules.
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
4. It must call csim, synth, and cosim through a metered, audited interface equivalent to `ToolServer`.
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
token budget: 32768
acceleration score cap: 8x
```

All of them must be overridable by config or environment.

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
7. Final output must come from a candidate that passes final csim, synth, cosim, and clock constraints.
8. Final validation failure must try an affordable ranked fallback or terminate explicitly.

Normal validation order:

```text
static -> csim -> synth -> optional exploration cosim
```

Final validation order:

```text
final csim -> final synth -> final cosim
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

Exploration is allowed only when the action remains affordable after preserving final verification reserves. Budget denial routes to finalization when an eligible candidate exists, otherwise to `FAILED(NO_VALID_CANDIDATE)`.

Recovery must reuse a completed `action_id` without repeating the call or charge. An ambiguous started action must be recorded and handled conservatively.

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
