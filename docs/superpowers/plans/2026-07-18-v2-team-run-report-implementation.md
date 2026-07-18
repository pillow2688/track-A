# V2 Team Run Report Implementation Plan

> Execution skill: `executing-plans`. The optional `writing-plans`,
> `using-git-worktrees`, and `subagent-driven-development` skills are unavailable in this
> session, so this checked-in plan is the local equivalent. Work is isolated on
> `feature/v2-team-report-real-smoke`; pre-existing dirty changes must be preserved.

**Goal:** Replace the V2 optimize run-local summary with a deterministic Chinese-first
team retrospective that explains every LLM attempt, Patch, Harness tool decision, CoSim
gate, Candidate branch, Token/Credit cost, best transition, final closure, and next action;
then exercise it on two real Vitis runs.

**Design:** Follow
`docs/superpowers/specs/2026-07-18-v2-team-run-report-design.md`. One collector builds a
normalized evidence model, one analyzer creates deterministic findings, and one renderer
serves automatic and external offline modes. The LLM never gets credited with Harness
tool calls. Automatic mode writes before Manifest; offline mode verifies a Manifest
whitelist and writes outside the immutable run.

**Baseline:** `python -m unittest discover -s tests -v` passed 157 tests before report
implementation.

## Task 1: Persist selector branch context and finalize baseline failures

**Files:**

- Modify `llm4hls_harness/llm4hls_agent/optimization.py`
- Modify `llm4hls_harness/tests/test_optimization.py`

- [ ] Add a pure helper that computes metrics digest, same-metrics attempted/failed
  classes, and available classes without changing selector priority.
- [ ] Make `select_optimization()` consume that helper.
- [ ] Persist `selection_context` into every new durable round.
- [ ] On resume, recompute and verify `selection_context` when present; keep old rounds
  without it compatible.
- [ ] Write `optimization_config.json` before V0 starts.
- [ ] Expand `BASELINE_NOT_VERIFIED` into a complete V2 terminal result and pass it through
  the common report/Manifest finalizer.
- [ ] Add tests for selection context, resume binding, and baseline-failure report/Manifest.

Verification:

```bash
python -m unittest tests.test_optimization -v
```

## Task 2: Build the normalized report collector and analyzer

**Files:**

- Create `llm4hls_harness/llm4hls_agent/v2_team_report.py`
- Create `llm4hls_harness/tests/test_v2_team_report.py`

- [ ] Add `V2TeamReportError`, immutable top-level/round data records, safe JSON/JSONL
  readers, relative-ref guards, and atomic text writing.
- [ ] In automatic mode, validate declared refs directly and set integrity state
  `GENERATED_PRE_MANIFEST`.
- [ ] In offline mode, verify Manifest, build its path whitelist, and read only covered
  refs. Reject unsealed legacy runs and broken ref closure.
- [ ] Detect `legacy_ungated`, `ppa_gate_v1`, and `official_proxy_gate_v1` from persisted
  fields without applying current defaults.
- [ ] Reconstruct per-round best-before/best-after, attempt stubs, Candidate status,
  selector context, Provider request/result, actual Patch observations, tool chain,
  CoSim state, score/comparison, and next transition.
- [ ] Pair Ledger STARTED/terminal events by action ID. Keep cached input as a subset of
  input tokens and enforce Credit conservation.
- [ ] Generate deterministic fact levels, prediction-vs-measurement findings, and next
  actions. Never treat model hypothesis or selector bottleneck as measured fact.
- [ ] Redact secret-key fields and avoid rendering absolute paths or raw hidden/reference
  content.

Tests must cover at minimum:

- Provider/Patch rejection without Candidate;
- CSim/Synth/clock/CoSim failure and explicit not-reached states;
- gate skip/run, promotion, not-better, final/fallback;
- Token/Credit conservation and cached-token semantics;
- old ungated and PPA-gated runs;
- broken Manifest/ref closure;
- repeated collection determinism.

Verification:

```bash
python -m unittest tests.test_v2_team_report -v
```

## Task 3: Render the detailed Markdown report

**Files:**

- Modify `llm4hls_harness/llm4hls_agent/v2_team_report.py`
- Modify `llm4hls_harness/tests/test_v2_team_report.py`

- [ ] Render the 30-second outcome inside the first 40 lines.
- [ ] Render the fixed small Mermaid plus plain-text fallback.
- [ ] Render baseline, global timeline, detailed round cards, Candidate/attempt tree,
  CoSim review, accounting, prediction-vs-measurement, next actions, official differences,
  and audit appendix.
- [ ] Explain every tool invocation/skip as a Harness decision, not an LLM tool call.
- [ ] Put complete redacted Prompt and applied diff in the appendix and use relative
  evidence links.
- [ ] Prove byte stability for the same mode/output/evidence and prove there are no HTML
  Mermaid labels, absolute paths, secrets, or fake final scores.

Verification:

```bash
python -m unittest tests.test_v2_team_report -v
```

## Task 4: Integrate automatic and offline report entry points

**Files:**

- Modify `llm4hls_harness/llm4hls_agent/optimization.py`
- Modify `llm4hls_harness/llm4hls_agent/cli.py`
- Modify `llm4hls_harness/tests/test_cli.py`
- Modify `llm4hls_harness/tests/test_optimization.py`

- [ ] Replace the old inline V2 renderer with the common writer.
- [ ] Keep automatic order `machine result -> report -> Manifest`.
- [ ] Add `report-v2-run --run-dir ... --output ...`; reject output inside the run.
- [ ] Ensure offline verify/collect/render failure leaves an existing output unchanged.
- [ ] Emit a machine-readable CLI summary with report path and verified Manifest digest.
- [ ] Inject Manifest finalization failure and prove machine JSON remains authoritative
  while the package is reported unsealed.

Verification:

```bash
python -m unittest tests.test_cli tests.test_optimization tests.test_v2_team_report -v
python -m unittest discover -s tests -v
```

## Task 5: Generate controlled reports before spending real tool time

- [ ] Run the current fake Provider/Backend workflow through promotion, rejection, gate
  skip, and final closure.
- [ ] Inspect the generated Markdown manually for branch and accounting clarity.
- [ ] Offline-render one `legacy_ungated` and one `ppa_gate_v1` historical real run to
  confirm truthful compatibility without mutating either run.
- [ ] Record report paths and hashes.

## Task 6: Run two real Vitis experiments

**Run A:** official public `dotProduct_optimize`, U55C, 5 ns.

**Run B:** internal self-contained `examples/u55c_v2_optimize_task`, U55C, 10 ns.

- [ ] Use fresh run directories; never overwrite historical evidence.
- [ ] Use `/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis` and verify the backend
  fingerprint records real Vitis.
- [ ] For Run A, explicitly set a local Credit limit of at least 75 and render
  `LOCAL_STRICT_BUDGET_OVERRIDE`; never claim official 40-Credit compliance.
- [ ] Use a real OpenAI-compatible Provider only if endpoint/key are configured. If not,
  either obtain user configuration or run a clearly labelled deterministic/replay
  Provider for real-Vitis-only validation; never report it as a real LLM run.
- [ ] Verify CSim/Synth/CoSim evidence, report, Manifest, Candidate tree, Token/Credit
  accounting, and final status for both runs.
- [ ] Present both report links and a concise comparison.

## Task 7: Finish safely

- [ ] Run `git diff --check` and the full 157+ test suite.
- [ ] Review all changes against the design spec and preserve unrelated dirty files.
- [ ] Invoke `finishing-a-development-branch` and present completion options without
  merging or deleting work unless the user chooses.
