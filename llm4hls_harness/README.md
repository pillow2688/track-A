# LLM4HLS Agent — Internal Milestone V0: Deterministic Harness

English | [简体中文](README_CN.md)

V0–V4 are this project's internal engineering milestones, not official contest
stages. The contest-provided example is named **Reference Agent & Evaluation
Harness**. See the [Chinese comparison](../doc/materials/02_harness/2026-07-14-official-reference-vs-internal-v0.md).

This directory contains only the V0 vertical slice: an immutable baseline is
loaded from a reference-compatible public task package, then run through the
budget-charged `csim -> synth -> cosim` sequence. There is no LLM, LangGraph,
patch generation, optimization loop, hidden grading, or reference-solution use.

The runtime is self-contained and uses only Python 3.11+ standard-library
modules. It never imports the reference harness. The task loader reads only
`task.toml`, `description.md` when present, the configured kernel, configured
headers, and the configured public testbench. Paths entering `hidden/` or
`reference/` are rejected.

## How the official reference maps to our milestones

The official example does not fit one internal milestone; its feature breadth
and engineering depth need separate assessment:

| Internal milestone | Official coverage | Assessment |
| --- | --- | --- |
| V0 deterministic harness | Core flow only | It has task loading, csim/synth/cosim, structured results, and credit charging, but lacks an immutable baseline, durable ledger and trace, candidate registry, idempotent charging, and recovery |
| V1 minimal repair loop | Prototype | It has an LLM repair loop, but returns a complete `.cpp` without constrained patches, candidate isolation, structured diagnosis, or safe rollback |
| V2 candidate/PPA loop | Partial | It synthesizes candidates and retains lower-latency code, but has no candidate tree, verification tiers, constraint-first ranking, or multi-objective PPA policy |
| V3 budget-aware LangGraph | Essentially absent | It has no LangGraph, checkpointer, durable State, multidimensional budget, or recoverable side-effect nodes |
| V4 competition hardening | Example material only | It includes hidden grading, a deadlock task, a scorecard, and Docker material, but is not competition-hardened and its auxiliary scripts retain 2023.2/2025.2 version drift |

In short, the official implementation reaches roughly a V2 prototype by
feature breadth, but it does not replace our V0 engineering foundation for
reproducibility, auditing, and recovery. We retain the local V0, use the
official loops as V1/V2 references, implement durable LangGraph ourselves in
V3, and treat the official grader, deadlock task, and container files as V4
fixtures.

## Reproducible WSL run

The checked development environment uses Ubuntu WSL and Vitis 2025.2 at
`/opt/xilinx/2025.2/Vitis`. The example assumes that the official public task
snapshot is present at `_external/fpt26-harness/` under the repository root;
that local fixture is not committed to Git. From WSL:

```bash
REPO=/mnt/c/path/to/track-A
PROJECT="$REPO/llm4hls_harness"
TASKS="$REPO/_external/fpt26-harness/tasks"
cd "$PROJECT"

python3 -m llm4hls_agent run "$TASKS/dotProduct_optimize" \
  --run-dir "$PROJECT/runs/v0-dotproduct" \
  --csim-timeout 180 --synth-timeout 600 --cosim-timeout 600

python3 -m llm4hls_agent run "$TASKS/projection_bugfix" \
  --run-dir "$PROJECT/runs/v0-projection" \
  --csim-timeout 180 --synth-timeout 600 --cosim-timeout 600 || test $? -eq 2

python3 -m llm4hls_agent run "$TASKS/residual_stream_deadlock" \
  --run-dir "$PROJECT/runs/v0-residual" \
  --csim-timeout 180 --synth-timeout 600 --cosim-timeout 300 || test $? -eq 2
```

Exit code `0` means the baseline passed csim, synth, cosim, and the configured
minimum-frequency check. Exit code `2` means the deterministic workflow
captured a baseline validation failure. Exit code `3` is a task, budget, or run
artifact/configuration error.

Run the same command again with the same `--run-dir` to reproduce idempotent
recovery: completed action IDs are loaded from their durable result files,
`trace.jsonl` records `TOOL_CACHE_HIT`, and the ledger receives no additional
charge.

## Configuration

Task `part`, `clock_ns`, and credit budget are used by default. CLI flags can
override them. The main environment variables are:

- `LLM4HLS_VITIS_HLS_ROOT` (default `/opt/xilinx/2025.2/Vitis`)
- `LLM4HLS_TOOLCHAIN_ID` (default `Vitis 2025.2`; part of the cache key)
- `LLM4HLS_PART`, `LLM4HLS_CLOCK_NS`
- `LLM4HLS_CREDIT_BUDGET`
- `LLM4HLS_COST_CSIM`, `LLM4HLS_COST_SYNTH`, `LLM4HLS_COST_COSIM`
- `LLM4HLS_CSIM_TIMEOUT_S`, `LLM4HLS_SYNTH_TIMEOUT_S`,
  `LLM4HLS_COSIM_TIMEOUT_S`
- `LLM4HLS_TOKEN_BUDGET` (recorded as zero used in no-LLM V0)
- `LLM4HLS_RUNTIME_LIMIT_S`, `LLM4HLS_MIN_FREQUENCY_MHZ`

Use `python3 -m llm4hls_agent run --help` for every override and per-tool call
limit.

## Durable artifacts

Each run directory contains:

```text
task_spec.json                 public metadata and public-file hashes
run_config.json                exact budget and tool configuration
workflow_result.json           final structured status and stop reason
candidate_registry.json        immutable candidate_000 and validation refs
budget_ledger.jsonl            locked STARTED/COMPLETED/AMBIGUOUS accounting
budget_state.json              derived authoritative budget snapshot
trace.jsonl                    workflow, tool, recovery, and cache events
baseline/source/<kernel>.cpp   read-only byte-for-byte baseline snapshot
actions/<action_id>/result.json
actions/<action_id>/work/      Tcl, stdout, stderr, XML, reports, Vitis work tree
```

Every action uses a full SHA-256 ID and is bound to the candidate, code, full
public task fixture, declared tool configuration, toolchain ID, and backend fingerprint. A completed
ledger event stores the exact `result.json` digest; every referenced Tcl, log,
XML, and report has its own digest and is rechecked on cache hits. The effective
tool timeout is capped by the remaining run-time budget. Concurrent writers to
the same run directory are rejected.

Reference-compatible
phases are `pass`, `compile_error`, `runtime_fail`, `synth_error`, `cosim_fail`,
and `timeout`; infrastructure exceptions are recorded as `tool_error`. A missing
or unparseable synthesis report is never treated as a pass.

## Fast tests

```bash
python3 -m unittest discover -s tests -v
```

The tests use deterministic fake process/tool backends and do not require
Vitis. Real Vitis evidence must be produced separately with the commands above.
