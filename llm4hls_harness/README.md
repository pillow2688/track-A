# LLM4HLS Agent — Internal Milestones V0–V3-A1

English | [简体中文](README_CN.md)

V0–V4 are this project's internal engineering milestones, not official contest
stages. The contest-provided example is named **Reference Agent & Evaluation
Harness**. See the [Chinese comparison](../doc/materials/02_harness/2026-07-14-official-reference-vs-internal-v0.md).

V0 provides an immutable, budget-audited `csim -> synth -> cosim` baseline.
V1 adds deterministic failure diagnosis, compact repair context, one constrained
unified diff, an isolated candidate, real validation, promotion, and safe
rollback. The repair proposal may come from an OpenAI-compatible API or a
static patch fixture. V2 adds a durable Candidate tree, deterministic
verification/constraint/PPA/cost comparison, one optimization class per round,
best-candidate preservation, final revalidation, and safety rejection. V3-A1
now includes an independent deterministic multi-round LangGraph, versioned
Planner input/output/action contracts, loop-level synthesis evidence, complete
Planner provenance journals, and a hash-sealed terminal package. A rejected
Candidate can advance to another scripted proposal, while promotion,
rejection, and final selection remain recoverable. Autonomous LLM planning,
hidden grading, and reference-solution use are not included.

`runs/v1-deepseek-final` is historical evidence that DeepSeek repaired
`FUNCTIONAL_MISMATCH`, but it predates strict candidate/action binding and the
Artifact Manifest. It must be regenerated and is not the completion of V1.
V1 is complete only after `FUNCTIONAL_MISMATCH`, `COMPILE_ERROR`, and
`SYNTHESIS_ERROR` all have real DeepSeek/Vitis evidence and the independent
`PATCH_INVALID` safety case passes the deterministic acceptance evaluator.

The V0–V2 runtime is self-contained and uses only Python 3.11+ standard-library
modules; V3-A1 dependencies are isolated in the `.[v3]` optional extra. It
never imports the reference harness. The task loader reads only
`task.toml`, `description.md` when present, the configured kernel, configured
headers, and the configured public testbench. Paths entering `hidden/` or
`reference/` are rejected.

## V3-A1 LangGraph prototype

V3-A1 leaves the existing `run`, `repair`, and `optimize` (V2) commands
unchanged. Its independent command splits baseline CSim/Synth/CoSim, a scripted
Planner, Candidate materialization, Candidate CSim/Synth, the CoSim value gate,
promotion/rejection, round continuation/stop, final CSim/Synth/CoSim, and
reporting into checkpointed action nodes. Repeat `--patch-file` to provide an
ordered deterministic proposal sequence; one file preserves the original
single-round behavior. `--max-no-improvement-rounds` controls convergence;
`--enable-final-fallback` reserves at most one additional fresh final closure
when the configured credits and tool-call limits can afford it.

Every Planner round now persists a canonical input, STARTED journal, versioned
output, legacy report projection, and COMPLETED journal. Candidate metadata and
the final Manifest bind that complete chain by hash. Before packaging, every
score is recomputed from Ledger-bound tool results, while Candidate decision
prepared/committed pairs, operation IDs, revision order, and the terminal
Registry binding are checked semantically. Synthesis evidence records loop
`PipelineII`, trip count, loop latency, and scheduler violations when
`csynth.xml` exposes them; top-level transaction interval is explicitly kept
separate from loop II. Missing or mutated provenance fails closed.

The strict happy path costs `25 + 25 + 25 = 75` credits: one complete baseline
closure, one complete Candidate closure, and one fresh final closure. If a
Candidate round cannot preserve the final 25-credit reserve, the prototype
skips that round and finalizes the verified baseline instead.

```bash
PROJECT_ROOT=/absolute/path/to/track-A
cd "$PROJECT_ROOT"
python3 -m venv .venv
.venv/bin/python -m pip install -e 'llm4hls_harness[v3]'

# Graph, routing, ledger, Candidate, and report data-flow smoke only
.venv/bin/llm4hls-v3-prototype \
  --task-dir llm4hls_harness/examples/u55c_v2_optimize_task \
  --patch-file llm4hls_harness/examples/u55c_v3_prototype.diff \
  --run-dir runs/v3a1-prototype-demo --backend demo
```

For a real Vitis run, enter the local development Distrobox first (if it has
been created on this machine), then invoke the same CLI with explicit inputs:

```bash
distrobox enter vitis-2025-2
PROJECT_ROOT=/absolute/path/to/track-A
VITIS_ROOT=/absolute/path/to/AMD/2025.2/Vitis
cd "$PROJECT_ROOT"
.venv/bin/llm4hls-v3-prototype \
  --task-dir llm4hls_harness/examples/u55c_v2_optimize_task \
  --patch-file llm4hls_harness/examples/u55c_v3_prototype.diff \
  --run-dir runs/v3a1-prototype-vitis --backend vitis \
  --vitis-root "$VITIS_ROOT"
```

The `demo` backend is always labelled `ORCHESTRATION_SMOKE_ONLY`; it is not HLS
or model-quality evidence. Before starting the Graph, the `vitis` backend
requires `<vitis-root>/settings64.sh`, then invokes the real tools. A completed
real closure is labelled `REAL_VITIS_VALIDATED`; a started but failed one is
labelled `REAL_VITIS_ATTEMPT_FAILED`. The
`vitis-2025-2` Distrobox is only the current local development environment; it
is not the final competition Docker deliverable. The minimal agent-only
clean-room image and Vitis preflight are documented below; a complete
competition image and real containerized HLS acceptance remain V4 work. Durable
outputs are `v3_prototype_result.json`, the node-by-node `v3_team_report.md`,
`control/package_manifest.json`, Candidate decision journals, and
`graph_checkpoints.sqlite`. The result JSON is committed last; terminal
re-entry can rebuild a missing generated Markdown report without rerunning any
HLS tool, while the package manifest detects mutation of authoritative
artifacts. The next step is V3-B: replace the deterministic adapter with a
budget-charged, recoverable autonomous LLM Planner without giving the model
direct tool or filesystem authority.

## V3-D Docker / clean-room reproduction

[`Dockerfile`](Dockerfile) builds a linux/amd64 **agent-only** image from a
digest-pinned Python 3.12.11 base. It adds only pinned Debian `libX11`/locale
packages needed by the version preflight, not Vitis itself. Python dependencies are fully pinned with
artifact hashes in [`requirements-v3.lock`](requirements-v3.lock); regenerate
it only with the command recorded at the top of that file. The build context
excludes `.env*`, keys, run artifacts, official tasks, and every V3-D
`golden/`/`hidden_like/` directory. Copy [`.env.example`](.env.example) to a
local `.env` only when a real provider run needs configuration. The template
contains placeholders only, and the populated file remains ignored.

Build the image from this directory:

```bash
docker build --platform linux/amd64 --tag llm4hls-v3d:py3.12 .
```

There is one supported entrypoint, `scripts/v3d-reproduce.sh`, with four
explicit modes: `demo-smoke`, `official-smoke`, `quick-tests`, and
`real-preflight`. The canonical one-record demo smoke command is:

```bash
mkdir -p "$PWD/runs/container"
docker run --rm --platform linux/amd64 \
  --user "$(id -u):$(id -g)" \
  --volume "$PWD/runs/container:/outputs:Z" \
  llm4hls-v3d:py3.12 demo-smoke
```

It runs exactly one synthetic V3-D batch record and writes
`runs/container/v3d-demo-smoke/{benchmark_results.jsonl,summary.json,summary.csv,report.md}`
on the host. The output labels the population `DEMO`, keeps the real-evidence
headline at zero runs, and proves packaging/orchestration only. It is not CSim,
Synth, CoSim, PPA, model-quality, or competition evidence.

The official public sources are deliberately absent from the image. Mount a
separately obtained, read-only copy of the public official corpus to run all
three official task packages through deterministic fixture orchestration:

```bash
OFFICIAL_PUBLIC="$PWD/task_corpus/official/fpt26-harness-public"
docker run --rm --platform linux/amd64 \
  --user "$(id -u):$(id -g)" \
  --mount type=bind,src="$PWD/runs/container",dst=/outputs \
  --mount type=bind,src="$OFFICIAL_PUBLIC",dst=/official-corpus,readonly \
  llm4hls-v3d:py3.12 official-smoke
```

This requires and selects exactly `projection_bugfix`, `dotProduct_optimize`,
and `residual_stream_deadlock`. It writes
`runs/container/v3d-official-three-smoke/`, including the seven batch outputs
and `official_smoke_receipt.json`. All three rows are explicitly labelled
`DETERMINISTIC`; the receipt says `DETERMINISTIC_FIXTURE_ONLY`, zero real LLM
calls, zero real HLS actions, and zero real-evidence runs. The synthetic rows
report zero CSim/Synth/CoSim calls. It tests
task loading, selection, mode-labelled fixture orchestration, persistence, and
aggregation only.

To prove the locked dependencies can run the complete quick unittest suite in
a fresh container, mount the repository harness read-only. The tests and
official sources remain outside the built image:

```bash
docker run --rm --platform linux/amd64 \
  --user "$(id -u):$(id -g)" \
  --mount type=bind,src="$PWD",dst=/workspace,readonly \
  llm4hls-v3d:py3.12 quick-tests
```

`quick-tests` runs `unittest discover` with `/workspace` as the explicit source
root and a finite timeout. It does not start Vitis or call a model. This is the
clean dependency/runtime check; it is separate from the image's built-in
one-record demo and externally mounted official fixture smoke.

AMD Vitis 2025.2 and its license are proprietary and are **not downloaded,
copied, or redistributed by this image**. To check a host-supplied installation,
bind-mount the complete `2025.2` install directory read-only at the same
absolute path; vendor setup scripts may contain absolute references to sibling
`Vivado` and `Model_Composer` directories. The one canonical real-runtime
preflight command is:

```bash
VITIS_2025_2=/absolute/path/to/AMD/2025.2
docker run --rm --platform linux/amd64 \
  --user "$(id -u):$(id -g)" \
  --security-opt label=disable \
  --mount type=bind,src="$PWD/runs/container",dst=/outputs \
  --mount type=bind,src="$VITIS_2025_2",dst="$VITIS_2025_2",readonly \
  --env LLM4HLS_VITIS_HLS_ROOT="$VITIS_2025_2/Vitis" \
  llm4hls-v3d:py3.12 real-preflight
```

`:Z` gives only the disposable output directory a private SELinux label. The
preflight instead disables the container label so the large vendor tree stays
read-only and is never recursively relabelled; do not add `:Z` to the Vitis
mount.

The preflight requires `settings64.sh`, sources it, resolves `vitis-run` under
the configured root, enforces version `2025.2`, checks the output mount, and
runs only `vitis-run --version`. Its JSON is labelled
`VITIS_PREFLIGHT_ONLY` with `hls_actions_started=0`; it can never be cited as a
real Vitis PASS. The same JSON is saved as
`/outputs/vitis-2025.2-preflight.json`. Missing runtime, wrong version, incompatible libraries, or an
unwritable mount fail closed with exit code 3. A license variable is warned
about but is not required for this zero-HLS preflight.

If bind-mounted Vitis is incompatible with the agent image's Debian userspace,
run the same entrypoint in a vendor-supported host/Distrobox runtime instead.
That is an external-runtime topology, not a self-contained Docker image. Real
evidence still requires a later `--backend vitis` run whose structured terminal
artifacts say `REAL_VITIS_VALIDATED`; neither this image build nor the preflight
claims that result.

Timeouts are finite at every clean-room boundary:

| Boundary | Default | Override |
| --- | ---: | --- |
| demo batch budget | 120 s | `LLM4HLS_DEMO_BATCH_TIMEOUT_S` |
| demo outer kill | 180 s + 15 s TERM grace | `LLM4HLS_DEMO_SMOKE_TIMEOUT_S` |
| official fixture batch budget | 180 s | `LLM4HLS_OFFICIAL_BATCH_TIMEOUT_S` |
| official fixture outer kill | 240 s + 15 s TERM grace | `LLM4HLS_OFFICIAL_SMOKE_TIMEOUT_S` |
| mounted-source quick tests | 900 s + 15 s TERM grace | `LLM4HLS_QUICK_TEST_TIMEOUT_S` |
| Vitis version preflight | 60 s + 5 s TERM grace | `LLM4HLS_PREFLIGHT_TIMEOUT_S` |
| real V3 per-run wall time | 7200 s | `--runtime-limit` |
| real CSim / Synth / CoSim | 300 / 1800 / 1800 s | `--csim-timeout`, `--synth-timeout`, `--cosim-timeout` |
| V3-D batch wall time | required for bounded overnight runs | `--max-runtime` |

The output bind mount is mandatory for durable evidence: deleting a container
must not delete the Ledger, Trace, Candidate records, reports, or benchmark
summary. Do not pass secrets as Docker build arguments or bake `.env` into an
image; pass provider/license variables only at runtime when the corresponding
real action actually needs them.

### P8 verification record (2026-07-20)

The first linux/amd64 Docker pass built the predecessor of the current source
as image
`sha256:7b3a0916f279d921fe8bab2b3a51051f07840d9f65540d65e6ce92b2ebdc9734`
with Python 3.12.11. Its hash-locked install, in-image imports, `pip check`,
one-record `demo-smoke`, and external Vitis `real-preflight` passed. The demo
reported one `DEMO` row and zero real-evidence runs; preflight reported
`vitis-run v2025.2`, `VITIS_PREFLIGHT_ONLY`, and `hls_actions_started=0`.

After adding `official-smoke` and `quick-tests`, their entrypoints and output
contracts passed the six reproduction-asset tests. `official-smoke` was also
executed directly on the host against the external public corpus and produced
exactly three `DETERMINISTIC` rows, zero real LLM/HLS processes, and zero
real-evidence runs under `runs/container/p8-host-20260720-r03/`. Its synthetic
summary reports zero real tool calls. This is host-entrypoint
fixture evidence, not a clean-container result.

The current session could not rebuild or run the updated image because access
to `/var/run/docker.sock` was denied, including after sandbox escalation; sudo
required an unavailable password. Therefore the digest above is retained only
as the last successful predecessor build and must not be cited as the digest of
the updated four-mode entrypoint. Re-run the documented build plus
`official-smoke` and `quick-tests` commands when Docker daemon access is
restored. No CSim, Synth, or CoSim was started by any P8 smoke or preflight, so
none of these records claims real Vitis validation.

## V1 OpenAI-compatible repair

The API provider defaults to `deepseek-v4-pro`. DeepSeek V4 requests explicitly
disable its default thinking mode so the bounded completion budget is spent on
the final JSON patch. Provider-reported input/output token usage is persisted
for successful and failed requests. Configure the
endpoint and secret in the environment; the API key is never written to run
configuration, traces, prompts, action results, or provider fingerprints.

```bash
PROJECT_ROOT=/absolute/path/to/track-A
VITIS_ROOT=/absolute/path/to/AMD/2025.2/Vitis
cd "$PROJECT_ROOT/llm4hls_harness"
export LLM4HLS_VITIS_HLS_ROOT="$VITIS_ROOT"
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

The command writes two flat files directly under `runs/`:

- `V1_ACCEPTANCE_REPORT.md` — complete English single-file review;
- `V1_ACCEPTANCE_REPORT_CN.md` — complete Chinese single-file review.

They recompute and cross-check the recorded acceptance, Baseline failures,
model/fallback state, Patch scope and interface, final gates, Candidate
promotion/rollback, Tokens, tool calls, Credits, Ledger, Trace, action records,
and Manifest hashes. All raw evidence links are relative to `runs/`.

## V2 Candidate and PPA loop

V2 uses the self-contained 256-element U55C `vector_add` fixture. Its baseline
is functionally correct and intentionally conservative (`PIPELINE II=16`). At
most six Candidate attempts are allowed, each round permits one optimization
class, and exploration stops after two consecutive no-improvement rounds. Each
Candidate runs CSim and synthesis first. Only a strict Synth-PPA improvement
over the current best is allowed to spend exploration CoSim credits. A gated
Candidate can become best only after CoSim, clock, and resource constraints
also pass. The final best is independently validated with complete CSim,
synthesis, and CoSim. The comparator order remains verification tier, hard
constraints, PPA cost (latency/II first), then Token/Credit cost and stable ID.

The accepted V2 evidence in `runs/v2-optimize-final` is frozen and must not be
overwritten. Use a new run directory for this gated policy.

The final V2 release record is
[`releases/v2-ppa-gated-final.md`](releases/v2-ppa-gated-final.md), with machine-readable
metadata in [`releases/v2-ppa-gated-final.json`](releases/v2-ppa-gated-final.json). The
global champion is fixed as `v2-optimize-final/candidate_004`; later real gated-regression
Candidates are cost-policy evidence and cannot replace it. Development proceeds to V3
LangGraph orchestration after this release.

With the API and Vitis environment configured above, run the real optimization:

```bash
python3 -m llm4hls_agent optimize examples/u55c_v2_optimize_task \
  --run-dir runs/next-ppa-gated \
  --vitis-root "$LLM4HLS_VITIS_HLS_ROOT" \
  --clock-ns 10 --minimum-frequency-mhz 100 \
  --credit-limit 160 --max-optimization-rounds 6 \
  --max-no-improvement-rounds 2 --max-final-attempts 2 \
  --max-csim-calls 8 --max-synth-calls 8 --max-cosim-calls 8 \
  --final-reserve-credits 25 \
  --csim-timeout 180 --synth-timeout 900 --cosim-timeout 900
```

Run the separate deterministic semantic-regression safety case. It makes no
LLM call, fully verifies the baseline, materializes the policy-valid regression
only after Patch validation, runs CSim on it, and must reject it without
polluting best/final:

```bash
python3 -m llm4hls_agent reject-v2 examples/u55c_v2_optimize_task \
  --run-dir runs/v2-safety-rejection-final \
  --patch-file examples/u55c_v2_regression.diff \
  --vitis-root "$LLM4HLS_VITIS_HLS_ROOT" \
  --clock-ns 10 --minimum-frequency-mhz 100 \
  --credit-limit 160 --csim-timeout 180 \
  --synth-timeout 900 --cosim-timeout 900
```

Machine acceptance recomputes the Candidate tree, every score/comparison,
best/final choice, provider/model/Token evidence, public input and kernel-only
Patch binding, safety invariants, and Ledger/Trace/action/budget consistency:

```bash
python3 -m llm4hls_agent accept-v2 \
  --optimization-run runs/v2-optimize-final \
  --rejection-run runs/v2-safety-rejection-final \
  --output-dir runs/v2-acceptance

python3 -m llm4hls_agent review-v2 --runs-root runs
```

Only real Vitis/DeepSeek evidence can produce `PASS`; fake evidence is labelled
`TEST_PASS`. `review-v2` does not call the model or Vitis and writes only two
flat Markdown files, `runs/V2_ACCEPTANCE_REPORT.md` and
`runs/V2_ACCEPTANCE_REPORT_CN.md`. No HTML dashboard is generated.

## How the official reference maps to our milestones

The official example does not fit one internal milestone; its feature breadth
and engineering depth need separate assessment:

| Internal milestone | Official coverage | Assessment |
| --- | --- | --- |
| V0 deterministic harness | Core flow only | It has task loading, csim/synth/cosim, structured results, and credit charging, but lacks an immutable baseline, durable ledger and trace, candidate registry, idempotent charging, and recovery |
| V1 minimal repair loop | Prototype | It has an LLM repair loop, but returns a complete `.cpp` without constrained patches, candidate isolation, structured diagnosis, or safe rollback |
| V2 candidate/PPA loop | Partial | It synthesizes candidates and retains lower-latency code, but has no candidate tree, verification tiers, constraint-first ranking, or multi-objective PPA policy |
| V3 budget-aware LangGraph | Essentially absent | It has no LangGraph, checkpointer, durable State, multidimensional budget, or recoverable side-effect nodes |
| V4 competition hardening | Example material plus local minimum | The local agent-only Docker smoke and Vitis 2025.2 preflight now exist, but real containerized HLS acceptance, hidden grading, submission packaging, and final hardening remain incomplete |

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
optimization_config.json       V2 scoring, limits, Patch policy, and provider fingerprint
optimization_rounds/*.json     durable Selector/proposal/Candidate/decision records
scores/*.json                  recomputable Candidate PPA and cost scores
comparisons/*.json             lexicographic comparison evidence
v2_result.json                 V2 best/final, rounds, final validation, and budget
v2_rejection_result.json       deterministic safety rejection and invariants
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
PYTHONPATH=llm4hls_harness python3 -m unittest discover \
  -s llm4hls_harness/tests -t llm4hls_harness -v
```

The tests use deterministic fake process/tool backends and do not require
Vitis. Real Vitis evidence must be produced separately with the commands above.
