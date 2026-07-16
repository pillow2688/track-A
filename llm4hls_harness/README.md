# LLM4HLS Agent — Internal Milestones V0–V1

English | [简体中文](README_CN.md)

V0–V4 are this project's internal engineering milestones, not official contest
stages. The contest-provided example is named **Reference Agent & Evaluation
Harness**. See the [Chinese comparison](../doc/materials/02_harness/2026-07-14-official-reference-vs-internal-v0.md).

V0 provides an immutable, budget-audited `csim -> synth -> cosim` baseline.
V1 adds deterministic failure diagnosis, compact repair context, one constrained
unified diff, an isolated candidate, real validation, promotion, and safe
rollback. The repair proposal may come from an OpenAI-compatible API or a
static patch fixture. V2 candidate/PPA optimization, V3 LangGraph orchestration,
hidden grading, and reference-solution use are not included.

`runs/v1-deepseek-final` is historical evidence that DeepSeek repaired
`FUNCTIONAL_MISMATCH`, but it predates strict candidate/action binding and the
Artifact Manifest. It must be regenerated and is not the completion of V1.
V1 is complete only after `FUNCTIONAL_MISMATCH`, `COMPILE_ERROR`, and
`SYNTHESIS_ERROR` all have real DeepSeek/Vitis evidence and the independent
`PATCH_INVALID` safety case passes the deterministic acceptance evaluator.

The runtime is self-contained and uses only Python 3.11+ standard-library
modules. It never imports the reference harness. The task loader reads only
`task.toml`, `description.md` when present, the configured kernel, configured
headers, and the configured public testbench. Paths entering `hidden/` or
`reference/` are rejected.

## V1 OpenAI-compatible repair

The API provider defaults to `deepseek-v4-pro`. DeepSeek V4 requests explicitly
disable its default thinking mode so the bounded completion budget is spent on
the final JSON patch. Provider-reported input/output token usage is persisted
for successful and failed requests. Configure the
endpoint and secret in the environment; the API key is never written to run
configuration, traces, prompts, action results, or provider fingerprints.

```bash
cd /home/ying/CompetitionTrackA/track-A/llm4hls_harness
export LLM4HLS_VITIS_HLS_ROOT=/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis
export OPENAI_BASE_URL=https://api.deepseek.com
export OPENAI_API_KEY=your-secret-value
export LLM4HLS_MODEL=deepseek-v4-pro

python3 -m llm4hls_agent repair examples/u55c_repair_task \
  --run-dir runs/v1-deepseek-v4-pro \
  --clock-ns 10 --minimum-frequency-mhz 100
```

`--allow-deterministic-fallback` is a debug-only path for the designated
vector-add fixture and is disabled by default. A fallback candidate passing
Vitis proves the validation loop, not LLM-based V1 acceptance. Real V1 evidence
requires an `openai-compatible` candidate with API-reported token usage that
passes Vitis csim, synth, and cosim.

For deterministic offline regression only:

```bash
python3 -m llm4hls_agent repair examples/u55c_repair_task \
  --run-dir runs/v1-static \
  --provider static --patch-file examples/u55c_repair.diff \
  --clock-ns 10 --minimum-frequency-mhz 100
```

The model receives only structured failure evidence, localized kernel lines,
public constraints, and a budget summary. It must return one strict JSON object
containing one unified diff. Only the configured kernel `.cpp` may change.
`CANDIDATE_VERIFIED` means the isolated candidate passed csim, synth, cosim,
and the minimum clock constraint. Provider, patch, validation, or budget failure
produces an explicit stop reason and leaves the baseline/best candidate intact.

Patch proposals are parsed, policy-checked, and dry-run against the immutable
source before a candidate ID is allocated. An invalid patch therefore creates
no candidate directory or registry record. A valid patch is atomically
materialized, reset to `NOT_RUN`, registered, and then bound to its own Vitis
actions.

## V1 three-error acceptance

| Case | Baseline boundary | Required result |
|---|---|---|
| `FUNCTIONAL_MISMATCH` | public csim mismatch | real DeepSeek candidate passes csim/synth/cosim/clock |
| `COMPILE_ERROR` | csim compile failure | real DeepSeek candidate passes csim/synth/cosim/clock |
| `SYNTHESIS_ERROR` | csim PASS, synth failure | real DeepSeek candidate passes csim/synth/cosim/clock |
| `PATCH_INVALID` | forbidden testbench patch | workflow fails safely before candidate allocation |

Preflight the new error boundaries without an LLM:

```bash
python3 -m llm4hls_agent run examples/u55c_compile_repair_task \
  --run-dir runs/v1-compile-preflight --clock-ns 10

python3 -m llm4hls_agent run examples/u55c_synthesis_repair_task \
  --run-dir runs/v1-synthesis-preflight --clock-ns 10
```

Run `repair` for all three HLS tasks with the API environment shown above. The
independent safety case makes no API call:

```bash
python3 -m llm4hls_agent repair examples/u55c_repair_task \
  --run-dir runs/v1-patch-invalid \
  --provider static --patch-file examples/u55c_patch_invalid.diff \
  --clock-ns 10 || test $? -eq 2
```

Finally evaluate the complete matrix. Evidence runs are read-only and the
result is written to a separate output directory:

```bash
python3 -m llm4hls_agent accept-v1 \
  --functional-run runs/v1-functional-final-2 \
  --compile-run runs/v1-compile-final \
  --synthesis-run runs/v1-synthesis-final-2 \
  --patch-invalid-run runs/v1-patch-invalid \
  --output-dir runs/v1-acceptance
```

Only canonical real evidence can produce `overall_status=PASS`. Unit/fake
evidence is labelled `TEST_PASS` and cannot complete V1. The command writes
machine-readable `acceptance_result.json`, English `acceptance_report.md`, and
Chinese `acceptance_report_CN.md`. Both report matrices link all four evidence
reports and Manifests and include the exact evaluation reproduction command.

For direct human review, aggregate the existing evidence offline without
rerunning the model or Vitis and without modifying any JSON, ledger, Trace,
Manifest, action, or Candidate artifact:

```bash
python3 -m llm4hls_agent review-v1 --runs-root runs
```

The command writes three flat files directly under `runs/`:

- `V1_ACCEPTANCE_REPORT.md` — complete English single-file review;
- `V1_ACCEPTANCE_REPORT_CN.md` — complete Chinese single-file review;
- `V1_ACCEPTANCE_DASHBOARD.html` — self-contained bilingual static dashboard.

They recompute and cross-check the recorded acceptance, Baseline failures,
model/fallback state, Patch scope and interface, final gates, Candidate
promotion/rollback, Tokens, tool calls, Credits, Ledger, Trace, action records,
and Manifest hashes. All raw evidence links are relative to `runs/`.

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
- `LLM4HLS_TOKEN_BUDGET` (zero in V0; V1 records provider-reported input,
  output, cached-input, and total tokens for successful and failed calls)
- `LLM4HLS_RUNTIME_LIMIT_S`, `LLM4HLS_MIN_FREQUENCY_MHZ`
- `OPENAI_BASE_URL`, `OPENAI_API_KEY`
- `LLM4HLS_MODEL` (default `deepseek-v4-pro`)
- `LLM4HLS_LLM_TIMEOUT_S`, `LLM4HLS_LLM_MAX_OUTPUT_TOKENS`,
  `LLM4HLS_LLM_TEMPERATURE`, `LLM4HLS_COST_LLM`

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
diagnostics/<candidate_id>.json
llm_actions/<action_id>/result.json
candidates/<candidate_id>/     read-only source, patch, and candidate metadata
v1_result.json                 V1 decision, validation, rollback, and budget result
experimental_report.md         human-readable Vitis, Token, credit, and metric report
artifact_manifest.json         sorted paths, sizes, SHA-256, producer/action bindings
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
