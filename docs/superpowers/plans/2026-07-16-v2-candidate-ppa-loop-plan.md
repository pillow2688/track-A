# V2 Candidate Tree and PPA Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify a real DeepSeek/Vitis V2 loop that maintains a Candidate tree, selects one optimization class per round, compares fully verified candidates lexicographically, preserves the best candidate, and produces deterministic machine and bilingual Markdown acceptance evidence.

**Architecture:** Keep V2 as a modular imperative Python workflow so Candidate management, validation, scoring, selection, acceptance, and reporting remain independently testable and can later become V3 graph nodes. Reuse the V1 Patch and audited tool/provider boundaries, extract only the shared Candidate/validation behavior, and keep V1 schemas and default action identities compatible.

**Tech Stack:** Python 3.11+ standard library, `unittest`, OpenAI-compatible DeepSeek API, AMD Vitis 2025.2 HLS, JSON/JSONL audit artifacts, JSON-compatible YAML 1.2 configuration.

---

## File Map

Create:

- `llm4hls_harness/llm4hls_agent/candidate.py` — Candidate Registry, atomic materialization, lineage, and immutable source/Patch records.
- `llm4hls_harness/llm4hls_agent/validation.py` — shared staged Candidate validation with exploration/final action scopes.
- `llm4hls_harness/llm4hls_agent/scoring.py` — scoring config, metric validation, hard constraints, PPA normalization, and lexicographic comparator.
- `llm4hls_harness/llm4hls_agent/optimization.py` — optimization context, deterministic Selector, durable rounds, budget gates, promotion, final verification, and `run_v2`.
- `llm4hls_harness/llm4hls_agent/v2_acceptance.py` — deterministic V2 machine acceptance and base bilingual reports.
- `llm4hls_harness/llm4hls_agent/v2_review.py` — flat read-only English/Chinese V2 human-review reports.
- `llm4hls_harness/llm4hls_agent/config/v2_scoring.yaml` — JSON-compatible YAML PPA weights and hard resource caps.
- `llm4hls_harness/llm4hls_agent/config/v2_acceptance.json` — V2 evidence roots and fail-closed checks.
- `llm4hls_harness/examples/u55c_v2_optimize_task/*` — self-contained optimize fixture.
- `llm4hls_harness/examples/u55c_v2_regression.diff` — deterministic semantic-regression safety Patch.
- `llm4hls_harness/tests/test_candidate.py`
- `llm4hls_harness/tests/test_validation.py`
- `llm4hls_harness/tests/test_scoring.py`
- `llm4hls_harness/tests/test_optimization.py`
- `llm4hls_harness/tests/test_v2_acceptance.py`
- `llm4hls_harness/tests/test_v2_review.py`

Modify:

- `llm4hls_harness/llm4hls_agent/repair.py` — delegate Candidate materialization/validation without changing V1 output.
- `llm4hls_harness/llm4hls_agent/tools.py` — add backward-compatible validation scope to action identity/result binding.
- `llm4hls_harness/llm4hls_agent/openai_provider.py` — add strict optimization Prompt/response mode using the existing transport and usage parsing.
- `llm4hls_harness/llm4hls_agent/cli.py` — add `optimize`, `accept-v2`, and `review-v2`.
- `llm4hls_harness/llm4hls_agent/artifacts.py` — recognize V2 core artifacts without weakening V1.
- `llm4hls_harness/tests/test_repair.py`, `test_toolserver.py`, `test_openai_provider.py`, `test_cli.py`, `test_artifacts.py` — compatibility and V2 coverage.
- `llm4hls_harness/README.md`, `README_CN.md` — synchronized V2 commands and evidence contract.
- `doc/materials/07_experiments/2026-07-15-v1-errors-and-v2-readiness.md` — append V2 incidents, resolutions, and final evidence.

## Task 1: Extract a Backward-Compatible CandidateManager

**Files:**

- Create: `llm4hls_harness/llm4hls_agent/candidate.py`
- Create: `llm4hls_harness/tests/test_candidate.py`
- Modify: `llm4hls_harness/llm4hls_agent/repair.py`
- Modify: `llm4hls_harness/tests/test_repair.py`

- [ ] **Step 1: Add characterization assertions for the existing V1 Candidate record**

Add to the existing successful V1 test:

```python
candidate = registry["candidates"]["candidate_001"]
self.assertEqual(candidate["parent_id"], "candidate_000")
self.assertEqual(candidate["status"], "VERIFIED")
self.assertTrue(candidate["immutable"])
self.assertEqual(candidate["code_hash"], result["patch"]["patched_sha256"])
self.assertEqual(candidate["patch_sha256"], sha256(
    result["patch"]["applied_patch"].encode("utf-8")
).hexdigest())
```

- [ ] **Step 2: Run the V1 characterization test and record the green baseline**

Run: `python3 -m unittest tests.test_repair -q`

Expected: existing V1 suite passes before extraction.

- [ ] **Step 3: Write failing CandidateManager tests**

```python
class CandidateManagerTests(unittest.TestCase):
    def test_child_uses_explicit_best_parent_and_allocates_monotonic_id(self) -> None:
        manager = CandidateManager(self.run_root, self.task)
        registry = self.make_registry(best="candidate_002")
        materialized = manager.materialize(
            registry,
            parent_id="candidate_002",
            patch_text=self.patch,
            application=self.application,
            kind="optimization",
            metadata={"round": 3, "optimization_class": "LOOP_UNROLL"},
        )
        self.assertEqual(materialized.candidate_id, "candidate_003")
        self.assertEqual(materialized.record["parent_id"], "candidate_002")
        self.assertEqual(
            materialized.record["validation"]["csim"]["status"], "NOT_RUN"
        )

    def test_materialization_is_idempotent_by_patch_and_parent(self) -> None:
        first = self.materialize(parent_id="candidate_000")
        second = self.materialize(parent_id="candidate_000")
        self.assertEqual(second.candidate_id, first.candidate_id)

    def test_same_patch_on_different_parent_is_a_distinct_candidate(self) -> None:
        first = self.materialize(parent_id="candidate_000")
        second = self.materialize(parent_id="candidate_001")
        self.assertNotEqual(second.candidate_id, first.candidate_id)
```

- [ ] **Step 4: Run the new test and verify RED**

Run: `python3 -m unittest tests.test_candidate -q`

Expected: FAIL with `ModuleNotFoundError: llm4hls_agent.candidate`.

- [ ] **Step 5: Implement the focused CandidateManager API**

```python
@dataclass(frozen=True)
class CandidateMaterialization:
    candidate_id: str
    record: dict[str, object]

class CandidateManager:
    def __init__(self, run_root: Path, task: PublicTask) -> None:
        self.run_root = run_root.resolve()
        self.task = task
```

Implement `load_registry()`, `next_id(registry)`, `materialize(registry, *, parent_id,
patch_text, application, kind, metadata)`, and `save_registry(registry)`. Copy the existing
staging/`os.replace`/read-only/orphan recovery behavior exactly. Include `parent_id` in
idempotency matching, reject a missing parent, and never allocate an ID before
`PatchApplication` exists. `materialize()` returns the complete `CandidateMaterialization`
dataclass above rather than a tuple.

- [ ] **Step 6: Make V1 delegate to CandidateManager**

Keep `_materialize_candidate` as a compatibility wrapper that constructs the existing V1 metadata and passes `parent_id="candidate_000"`. Do not change `v1_result.json`, Candidate field names, Patch refs, or V1 action IDs.

- [ ] **Step 7: Run Candidate and V1 tests**

Run: `python3 -m unittest tests.test_candidate tests.test_repair tests.test_acceptance tests.test_review -q`

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add llm4hls_harness/llm4hls_agent/candidate.py \
  llm4hls_harness/llm4hls_agent/repair.py \
  llm4hls_harness/tests/test_candidate.py \
  llm4hls_harness/tests/test_repair.py
git commit -m "refactor: extract reusable Candidate manager"
```

## Task 2: Add Scoped Validation Without Breaking V1 Cache Identity

**Files:**

- Create: `llm4hls_harness/llm4hls_agent/validation.py`
- Create: `llm4hls_harness/tests/test_validation.py`
- Modify: `llm4hls_harness/llm4hls_agent/tools.py`
- Modify: `llm4hls_harness/llm4hls_agent/repair.py`
- Modify: `llm4hls_harness/tests/test_toolserver.py`

- [ ] **Step 1: Write failing ToolServer scope tests**

```python
def test_final_scope_has_distinct_action_and_charge(self) -> None:
    exploration = self.server.csim(self.kernel, candidate_id="candidate_001")
    final = self.server.csim(
        self.kernel, candidate_id="candidate_001", validation_scope="final"
    )
    self.assertNotEqual(exploration.action_id, final.action_id)
    self.assertEqual(final.validation_scope, "final")
    self.assertEqual(self.ledger.snapshot()["tool_used"]["csim"], 2)

def test_default_scope_preserves_legacy_action_payload(self) -> None:
    result = self.server.csim(self.kernel, candidate_id="candidate_000")
    self.assertEqual(result.validation_scope, "exploration")
    self.assertEqual(result.action_id, self.expected_legacy_action_id())
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_toolserver -q`

Expected: FAIL because `validation_scope` is not accepted.

- [ ] **Step 3: Implement backward-compatible validation scope**

Add `validation_scope: str = "exploration"` to `ToolResult`. Let `from_dict()` default missing legacy values to `exploration`. Accept `validation_scope` in `csim/synth/cosim/_invoke` and validate it against `{"exploration", "final"}`.

For action identity, preserve the exact legacy payload for exploration and add the field only for final:

```python
action_payload = {
    "kind": kind,
    "candidate_id": candidate_id,
    "code_hash": code_hash,
    "tool_config_hash": tool_config_hash,
    "backend_fingerprint": self.backend_fingerprint,
    "task_fingerprint": self.task_fingerprint,
}
if validation_scope != "exploration":
    action_payload["validation_scope"] = validation_scope
```

Bind the scope in new result files and `_load_result()`.

- [ ] **Step 4: Write failing shared validation tests**

```python
def test_requires_cosim_runs_all_stages_and_returns_metrics(self) -> None:
    result = validate_candidate(
        self.task, self.kernel, "candidate_001", self.run_root,
        self.config, backend=self.backend, validation_scope="exploration",
    )
    self.assertEqual(result.status, "DONE")
    self.assertEqual(result.validation["cosim"]["status"], "PASS")
    self.assertIsNotNone(result.metrics_ref)

def test_final_scope_is_propagated_to_every_stage(self) -> None:
    result = validate_candidate(
        self.task, self.kernel, "candidate_001", self.run_root,
        self.config, backend=self.backend, validation_scope="final",
    )
    self.assertEqual(
        [result.validation[stage]["validation_scope"]
         for stage in ("csim", "synth", "cosim")],
        ["final", "final", "final"],
    )
```

- [ ] **Step 5: Move `_validate_candidate` into `validation.py`**

Expose:

```python
@dataclass(frozen=True)
class CandidateValidation:
    status: str
    stop_reason: str
    validation: dict[str, dict[str, object]]
    clock_constraint: dict[str, object]
    metrics_ref: str | None
    budget: dict[str, object]

```

Implement `validate_candidate(task, kernel_bytes, candidate_id, run_root, config, *,
backend, validation_scope="exploration") -> CandidateValidation` by moving the existing
CSim → Synth → Clock → CoSim logic without changing stage order or stop reasons. Pass
`validation_scope` into every ToolServer stage. V1 retains a wrapper that converts the
dataclass to its current dictionary shape.

- [ ] **Step 6: Run focused and full V1 tests**

Run: `python3 -m unittest tests.test_validation tests.test_toolserver tests.test_repair tests.test_workflow -q`

Expected: PASS, including legacy cache/idempotency tests.

- [ ] **Step 7: Commit**

```bash
git add llm4hls_harness/llm4hls_agent/validation.py \
  llm4hls_harness/llm4hls_agent/tools.py \
  llm4hls_harness/llm4hls_agent/repair.py \
  llm4hls_harness/tests/test_validation.py \
  llm4hls_harness/tests/test_toolserver.py
git commit -m "feat: add scoped Candidate validation"
```

## Task 3: Implement PPA Scoring and Lexicographic Comparison

**Files:**

- Create: `llm4hls_harness/llm4hls_agent/scoring.py`
- Create: `llm4hls_harness/llm4hls_agent/config/v2_scoring.yaml`
- Create: `llm4hls_harness/tests/test_scoring.py`

- [ ] **Step 1: Write config and zero-baseline RED tests**

```python
def test_loads_json_compatible_yaml_and_requires_weights_sum_to_one(self) -> None:
    config = load_scoring_config(self.config_path)
    self.assertEqual(config.weights["latency"], 0.45)
    self.assertAlmostEqual(sum(config.weights.values()), 1.0)

def test_zero_baseline_resource_is_finite(self) -> None:
    score = score_candidate(
        baseline=self.metrics(lut=100, dsp=0),
        candidate=self.metrics(lut=90, dsp=1),
        config=self.config,
    )
    self.assertTrue(math.isfinite(score.ppa_cost))
    self.assertGreater(score.components["dsp"], 1.0)
```

- [ ] **Step 2: Write comparator priority RED tests**

```python
def test_verification_outranks_better_ppa(self) -> None:
    low_tier = self.score(tier=3, ppa=0.1, hard=True)
    high_tier = self.score(tier=4, ppa=1.2, hard=True)
    self.assertEqual(compare_scores(high_tier, low_tier).winner, "candidate")

def test_hard_constraint_outranks_better_ppa(self) -> None:
    violating = self.score(tier=4, ppa=0.1, hard=False)
    eligible = self.score(tier=4, ppa=1.0, hard=True)
    self.assertEqual(compare_scores(eligible, violating).winner, "candidate")

def test_cost_breaks_only_equal_ppa(self) -> None:
    cheap = self.score(tier=4, ppa=0.8, hard=True, tokens=100, credits=25)
    costly = self.score(tier=4, ppa=0.8, hard=True, tokens=200, credits=25)
    self.assertEqual(compare_scores(cheap, costly).winner, "candidate")
```

- [ ] **Step 3: Run and verify RED**

Run: `python3 -m unittest tests.test_scoring -q`

Expected: FAIL because `llm4hls_agent.scoring` does not exist.

- [ ] **Step 4: Implement strict config and score dataclasses**

```python
@dataclass(frozen=True)
class ScoringConfig:
    weights: Mapping[str, float]
    max_resource_percent: Mapping[str, float]
    official_score_enabled: bool = False

@dataclass(frozen=True)
class CandidateScore:
    candidate_id: str
    verification_tier: int
    hard_constraints_passed: bool
    hard_failures: tuple[str, ...]
    ppa_cost: float | None
    components: Mapping[str, float]
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    credits_used: int

@dataclass(frozen=True)
class Comparison:
    winner: str
    strictly_better: bool
    reason: str
    candidate_key: tuple[object, ...]
    incumbent_key: tuple[object, ...]
```

Parse `v2_scoring.yaml` with `json.loads`, reject unknown keys, require all seven weights,
finite non-negative values, and require
`math.isclose(sum(weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-9)`.

- [ ] **Step 5: Implement metrics and comparator functions**

Implement `score_candidate(*, candidate_id, baseline, candidate, validation, clock,
config, input_tokens, output_tokens, cached_input_tokens, credits_used) -> CandidateScore`
and `compare_scores(candidate, incumbent) -> Comparison`. Use worst latency, maximum
interval, and available-resource fractions exactly as specified. Treat bool, missing,
negative, NaN, Infinity, or non-positive available resources as hard failures. Stable
comparison key minimizes `(-tier, not hard, ppa, tokens, credits, candidate_id)`.

- [ ] **Step 6: Run tests**

Run: `python3 -m unittest tests.test_scoring -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add llm4hls_harness/llm4hls_agent/scoring.py \
  llm4hls_harness/llm4hls_agent/config/v2_scoring.yaml \
  llm4hls_harness/tests/test_scoring.py
git commit -m "feat: add deterministic Candidate comparator"
```

## Task 4: Implement the One-Class OptimizationSelector

**Files:**

- Create: `llm4hls_harness/llm4hls_agent/optimization.py`
- Create: `llm4hls_harness/tests/test_optimization.py`

- [ ] **Step 1: Write RED tests for deterministic class selection**

```python
def test_high_interval_selects_pipeline(self) -> None:
    decision = select_optimization(
        self.metrics(interval=16), source=self.unpipelined_source,
        attempted=(), failures=(),
    )
    self.assertEqual(decision.optimization_class, "LOOP_PIPELINE")

def test_static_trip_count_after_pipeline_selects_unroll(self) -> None:
    decision = select_optimization(
        self.metrics(interval=1, latency=260), source=self.pipelined_source,
        attempted=("LOOP_PIPELINE",), failures=(),
    )
    self.assertEqual(decision.optimization_class, "LOOP_UNROLL")

def test_no_new_evidence_does_not_repeat_failed_class(self) -> None:
    decision = select_optimization(
        self.metrics(interval=16), source=self.unpipelined_source,
        attempted=("LOOP_PIPELINE",),
        failures=(("LOOP_PIPELINE", self.metrics_digest),),
    )
    self.assertNotEqual(decision.optimization_class, "LOOP_PIPELINE")
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_optimization.OptimizationSelectorTests -q`

Expected: FAIL because Selector symbols are missing.

- [ ] **Step 3: Implement Selector types and pure function**

```python
ALLOWED_OPTIMIZATIONS = (
    "LOOP_PIPELINE", "LOOP_UNROLL", "MEMORY_LAYOUT", "LOOP_RESTRUCTURE"
)

@dataclass(frozen=True)
class OptimizationDecision:
    optimization_class: str | None
    bottleneck: str
    evidence: tuple[str, ...]
    metrics_digest: str
    stop_reason: str | None = None

```

Implement `select_optimization(metrics, *, source, attempted, failures) ->
OptimizationDecision` as a pure function. It performs no I/O and returns
`NO_DISTINCT_OPTIMIZATION` when the class whitelist is exhausted.

- [ ] **Step 4: Run Selector tests**

Run: `python3 -m unittest tests.test_optimization.OptimizationSelectorTests -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add llm4hls_harness/llm4hls_agent/optimization.py \
  llm4hls_harness/tests/test_optimization.py
git commit -m "feat: select one PPA optimization class per round"
```

## Task 5: Add the Strict DeepSeek Optimization Provider Contract

**Files:**

- Modify: `llm4hls_harness/llm4hls_agent/optimization.py`
- Modify: `llm4hls_harness/llm4hls_agent/openai_provider.py`
- Modify: `llm4hls_harness/tests/test_openai_provider.py`
- Modify: `llm4hls_harness/tests/test_optimization.py`

- [ ] **Step 1: Write optimization Prompt tests**

```python
def test_optimization_prompt_contains_one_allowed_class_and_no_secret(self) -> None:
    prompt = build_optimization_prompt(self.context)
    self.assertIn('"allowed_optimization_class": "LOOP_PIPELINE"', prompt)
    self.assertIn('"final_reserve_credits": 25', prompt)
    self.assertNotIn(self.api_key, prompt)
    self.assertNotIn("kernel_tb.cpp", prompt)
```

- [ ] **Step 2: Write response-class enforcement tests**

```python
def test_provider_rejects_a_different_optimization_class(self) -> None:
    provider = self.provider_with_response({
        "hypothesis": "x", "optimization_class": "LOOP_UNROLL",
        "expected_effect": "x", "risk": "low",
        "required_validation": ["csim", "synth", "cosim"],
        "patch": self.patch,
    })
    with self.assertRaisesRegex(RepairProviderError, "optimization class"):
        provider.propose_optimization(self.context)
```

- [ ] **Step 3: Run and verify RED**

Run: `python3 -m unittest tests.test_openai_provider tests.test_optimization -q`

Expected: FAIL because optimization context/provider APIs are missing.

- [ ] **Step 4: Define the optimization boundary**

```python
@dataclass(frozen=True)
class OptimizationContext:
    task_id: str
    parent_candidate_id: str
    round_index: int
    allowed_optimization_class: str
    bottleneck: str
    evidence: tuple[str, ...]
    baseline_metrics: Mapping[str, object]
    current_metrics: Mapping[str, object]
    source_excerpt: str
    failed_actions: tuple[Mapping[str, object], ...]
    remaining_tokens: int
    remaining_credits: int | None
    final_reserve_credits: int
    top: str
    kernel_name: str
    part: str
    clock_ns: float
```

Use the existing `PatchProposal` with `change_class` equal to the required optimization class. Add `OptimizationProvider(Protocol)` with `propose_optimization(context)`.

- [ ] **Step 5: Implement shared OpenAI request parsing and optimization mode**

Refactor only the HTTP/usage extraction shared by repair and optimization. Keep repair Prompt bytes and successful V1 behavior stable. Optimization response expects `optimization_class`; convert it to `PatchProposal.change_class` after exact equality with the Context class. Require all three validation stages for this fixture even if the model requests fewer.

- [ ] **Step 6: Run Provider tests**

Run: `python3 -m unittest tests.test_openai_provider tests.test_repair tests.test_optimization -q`

Expected: PASS, including DeepSeek `reasoning_content`, segmented content, usage, and disabled-thinking tests.

- [ ] **Step 7: Commit**

```bash
git add llm4hls_harness/llm4hls_agent/optimization.py \
  llm4hls_harness/llm4hls_agent/openai_provider.py \
  llm4hls_harness/tests/test_openai_provider.py \
  llm4hls_harness/tests/test_optimization.py
git commit -m "feat: add strict DeepSeek PPA proposal contract"
```

## Task 6: Build the Durable Four-Round V2 Workflow and CLI

**Files:**

- Modify: `llm4hls_harness/llm4hls_agent/optimization.py`
- Modify: `llm4hls_harness/llm4hls_agent/cli.py`
- Modify: `llm4hls_harness/tests/test_optimization.py`
- Modify: `llm4hls_harness/tests/test_cli.py`

- [ ] **Step 1: Write the fake-backend end-to-end RED test**

```python
def test_four_round_loop_preserves_best_and_finally_revalidates(self) -> None:
    result = run_v2(
        self.task, self.run_root, self.run_config,
        self.optimization_config, self.provider,
        backend=self.backend,
    )
    registry = json.loads((self.run_root / "candidate_registry.json").read_text())
    self.assertEqual(result["status"], "DONE")
    self.assertEqual(result["stop_reason"], "NO_IMPROVEMENT_LIMIT")
    self.assertEqual(registry["best_candidate_id"], "candidate_002")
    self.assertEqual(registry["final_candidate_id"], "candidate_002")
    self.assertEqual(registry["candidates"]["candidate_003"]["status"], "REJECTED_VALIDATION")
    self.assertEqual(registry["candidates"]["candidate_004"]["status"], "REJECTED_NOT_BETTER")
    for stage in ("csim", "synth", "cosim"):
        ref = result["final_validation"][stage]["result_ref"]
        action = json.loads((self.run_root / ref).read_text())
        self.assertEqual(action["validation_scope"], "final")
```

Configure fake reports so Candidate 1 improves, Candidate 2 improves again, Candidate 3 fails CSim, Candidate 4 passes but has worse PPA.

- [ ] **Step 2: Write final-reserve and resume RED tests**

```python
def test_exploration_stops_before_spending_final_reserve(self) -> None:
    result = self.run_with_credit_limit(50)
    self.assertEqual(result["exploration_stop_reason"], "FINAL_RESERVE_REACHED")
    self.assertEqual(result["final_validation"]["cosim"]["status"], "PASS")

def test_restart_reuses_completed_round_without_duplicate_charge(self) -> None:
    first = self.run_until_injected_crash_after_round(1)
    before = BudgetLedger(self.ledger, self.budget).snapshot()
    resumed = self.run_normally()
    after = BudgetLedger(self.ledger, self.budget).snapshot()
    self.assertEqual(resumed["rounds"][0]["action_id"], first["round_action_id"])
    self.assertEqual(after["tool_used"]["llm"], before["tool_used"]["llm"] + 1)
```

- [ ] **Step 3: Run and verify RED**

Run: `python3 -m unittest tests.test_optimization -q`

Expected: FAIL because `run_v2` and durable round state are missing.

- [ ] **Step 4: Implement V2 config and durable round records**

```python
@dataclass(frozen=True)
class OptimizationConfig:
    scoring: ScoringConfig
    max_rounds: int = 4
    max_no_improvement_rounds: int = 2
    max_llm_calls: int = 6
    final_reserve_credits: int = 25
    patch_limits: PatchLimits = PatchLimits(max_changed_lines=30, max_hunks=4)

```

Implement `run_v2(task, run_dir, run_config, optimization_config, provider, *,
backend=None) -> dict[str, object]`. Persist each primary proposal as
`optimization_rounds/round_NNN.json` with stable Context digest, provider ref, selected
class, Candidate ID, validation ref, score ref, comparison, decision, and completion
state. On restart, verify and reuse completed rounds; never infer completion from Trace
alone.

- [ ] **Step 5: Implement budget gate, promotion, stopping, final and fallback**

Before each LLM call require the configured stage costs plus final reserve and available call limits. A primary proposal consumes one round; one format repair consumes LLM call/Token budget but not another round. Increment no-improvement for provider/Patch failure, validation failure, or non-better Candidate.

Perform final validation with `validation_scope="final"`. If it fails, rank earlier fully verified candidates and retry only when the complete final closure is affordable.

- [ ] **Step 6: Add CLI parser and summary**

Add `optimize` arguments from the spec, resolve `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `LLM4HLS_MODEL` exactly as V1 does, and print:

```json
{
  "status": "DONE",
  "stop_reason": "NO_IMPROVEMENT_LIMIT",
  "best_candidate_id": "candidate_002",
  "final_candidate_id": "candidate_002",
  "rounds_completed": 4,
  "credits_used": 150,
  "tokens_used": 1234,
  "result_ref": "v2_result.json"
}
```

The numeric values come from evidence; tests use fixture values, production never hardcodes them.

Also add `reject-v2 TASK --run-dir RUN --patch-file PATCH` as an explicitly deterministic
safety command. It runs and verifies baseline, applies the policy-valid Patch, materializes
one `kind="safety_regression"` child, runs CSim only, requires that CSim fail, and records
`REJECTED_VALIDATION` while leaving best/final at baseline. It exits 0 only when all safety
invariants hold; it is never a source of a formal optimized best.

- [ ] **Step 7: Run workflow and CLI tests**

Run: `python3 -m unittest tests.test_optimization tests.test_cli tests.test_repair tests.test_workflow -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add llm4hls_harness/llm4hls_agent/optimization.py \
  llm4hls_harness/llm4hls_agent/cli.py \
  llm4hls_harness/tests/test_optimization.py \
  llm4hls_harness/tests/test_cli.py
git commit -m "feat: add durable V2 Candidate PPA loop"
```

## Task 7: Add the Optimize Fixture and Deterministic Rejection Case

**Files:**

- Create: `llm4hls_harness/examples/u55c_v2_optimize_task/task.toml`
- Create: `llm4hls_harness/examples/u55c_v2_optimize_task/description.md`
- Create: `llm4hls_harness/examples/u55c_v2_optimize_task/kernel.cpp`
- Create: `llm4hls_harness/examples/u55c_v2_optimize_task/kernel.h`
- Create: `llm4hls_harness/examples/u55c_v2_optimize_task/kernel_tb.cpp`
- Create: `llm4hls_harness/examples/u55c_v2_regression.diff`
- Modify: `llm4hls_harness/tests/test_task.py`
- Modify: `llm4hls_harness/tests/test_optimization.py`

- [ ] **Step 1: Write fixture contract RED tests**

```python
def test_v2_fixture_is_self_contained_optimize_task(self) -> None:
    task = load_public_task(self.examples / "u55c_v2_optimize_task")
    self.assertEqual(task.task_type, "optimize")
    self.assertEqual(task.top, "vector_add")
    self.assertTrue(task.requires_cosim)
    self.assertEqual(task.part, "xcu55c-fsvh2892-2L-e")
    self.assertEqual(task.clock_ns, 10.0)
    self.assertNotIn("reference", task.public_file_hashes)
    self.assertNotIn("hidden", task.public_file_hashes)
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_task.V2FixtureTests -q`

Expected: FAIL because the fixture does not exist.

- [ ] **Step 3: Create the fixture**

Use exactly 256 elements and an intentionally conservative `PIPELINE II=16` baseline so
Vitis exposes a deterministic first bottleneck without changing behavior:

```cpp
// kernel.h
#pragma once
constexpr int VECTOR_SIZE = 256;
void vector_add(
    const int a[VECTOR_SIZE],
    const int b[VECTOR_SIZE],
    int c[VECTOR_SIZE]);
```

```cpp
// kernel.cpp
#include "kernel.h"

void vector_add(
    const int a[VECTOR_SIZE],
    const int b[VECTOR_SIZE],
    int c[VECTOR_SIZE]) {
#pragma HLS INTERFACE ap_memory port=a
#pragma HLS INTERFACE ap_memory port=b
#pragma HLS INTERFACE ap_memory port=c
vector_add_loop:
    for (int i = 0; i < VECTOR_SIZE; ++i) {
#pragma HLS PIPELINE II=16
        c[i] = a[i] + b[i];
    }
}
```

The public testbench runs four deterministic cases. Use values `(i, 2*i)`, `(-i, i/2)`,
`(0, 0)`, and boundary-safe pairs where `a[0]=INT_MAX,b[0]=0` and
`a[1]=INT_MIN,b[1]=0`; compute `expected=a[i]+b[i]` only for pairs chosen not to overflow.
Return a distinct non-zero code containing case and index information on mismatch.

`task.toml` must set budget 160 and describe the public objective without revealing a preferred Patch.

- [ ] **Step 4: Add the deterministic regression Patch**

The Patch modifies only `kernel.cpp`, applies cleanly, preserves the interface, and changes one output expression so CSim fails. Add a test that `apply_unified_diff` accepts it and real/fake validation rejects the materialized Candidate while `best_candidate_id` remains unchanged.

- [ ] **Step 5: Run fixture and safety tests**

Run: `python3 -m unittest tests.test_task tests.test_optimization -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add llm4hls_harness/examples/u55c_v2_optimize_task \
  llm4hls_harness/examples/u55c_v2_regression.diff \
  llm4hls_harness/tests/test_task.py \
  llm4hls_harness/tests/test_optimization.py
git commit -m "test: add U55C V2 optimization fixtures"
```

## Task 8: Add Fail-Closed V2 Acceptance and Artifact Coverage

**Files:**

- Create: `llm4hls_harness/llm4hls_agent/v2_acceptance.py`
- Create: `llm4hls_harness/llm4hls_agent/config/v2_acceptance.json`
- Create: `llm4hls_harness/tests/test_v2_acceptance.py`
- Modify: `llm4hls_harness/llm4hls_agent/artifacts.py`
- Modify: `llm4hls_harness/llm4hls_agent/cli.py`
- Modify: `llm4hls_harness/tests/test_artifacts.py`
- Modify: `llm4hls_harness/tests/test_cli.py`

- [ ] **Step 1: Write machine-acceptance RED tests**

```python
def test_acceptance_recomputes_tree_score_and_accounting(self) -> None:
    result = compute_v2_acceptance(
        self.spec, self.optimization_run, self.rejection_run
    )
    self.assertEqual(result["overall_status"], "PASS")
    self.assertTrue(result["checks"]["candidate_tree_valid"])
    self.assertTrue(result["checks"]["two_real_llm_candidates"])
    self.assertTrue(result["checks"]["best_matches_recomputed_comparator"])
    self.assertTrue(result["checks"]["rejected_candidate_did_not_pollute_best"])

def test_tampered_score_fails_closed(self) -> None:
    self.tamper_score_without_updating_manifest()
    result = compute_v2_acceptance(
        self.spec, self.optimization_run, self.rejection_run
    )
    self.assertEqual(result["overall_status"], "FAIL")
    self.assertIn("MANIFEST_INVALID", result["reason_codes"])
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_v2_acceptance -q`

Expected: FAIL because the evaluator does not exist.

- [ ] **Step 3: Implement pure compute and separate writer**

Implement pure `compute_v2_acceptance(spec_path, optimization_run, rejection_run) ->
dict[str, object]` and writing wrapper `evaluate_v2_acceptance(spec_path,
optimization_run, rejection_run, output_dir) -> dict[str, object]`. Verify Manifest first,
then parse only Manifest-covered refs. Recompute lineage, provider/model/fallback, token
totals, tool calls/credits, metric validity, every Candidate score, promotion decisions,
current best, final validation, safety rejection, and public-file hashes.

- [ ] **Step 4: Extend Manifest core-artifact checks**

Require `v2_result.json`, scoring config snapshot, round records, Candidate metadata, score/comparison records, Registry, Ledger, Trace, all actions, final actions, reports, and V2 result refs. Do not weaken existing V0/V1 artifact rules.

- [ ] **Step 5: Add `accept-v2` CLI**

Print `overall_status`, `acceptance_result.json`, English/Chinese base report refs, and output dir. Exit 0 only for REAL/PASS; exit 2 for evaluated FAIL; exit 3 for invalid invocation/artifacts that prevent evaluation.

- [ ] **Step 6: Run acceptance/artifact/CLI tests**

Run: `python3 -m unittest tests.test_v2_acceptance tests.test_artifacts tests.test_cli -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add llm4hls_harness/llm4hls_agent/v2_acceptance.py \
  llm4hls_harness/llm4hls_agent/config/v2_acceptance.json \
  llm4hls_harness/llm4hls_agent/artifacts.py \
  llm4hls_harness/llm4hls_agent/cli.py \
  llm4hls_harness/tests/test_v2_acceptance.py \
  llm4hls_harness/tests/test_artifacts.py \
  llm4hls_harness/tests/test_cli.py
git commit -m "feat: add deterministic V2 acceptance"
```

## Task 9: Generate Flat Bilingual V2 Human-Review Reports

**Files:**

- Create: `llm4hls_harness/llm4hls_agent/v2_review.py`
- Create: `llm4hls_harness/tests/test_v2_review.py`
- Modify: `llm4hls_harness/llm4hls_agent/cli.py`
- Modify: `llm4hls_harness/tests/test_cli.py`

- [ ] **Step 1: Write report-content RED tests**

```python
def test_reports_contain_candidate_tree_ppa_and_accounting(self) -> None:
    review = build_v2_review_evidence(
        self.spec, self.optimization_run, self.rejection_run,
        self.acceptance_result, self.runs_root,
    )
    english = render_v2_markdown(review, "en")
    chinese = render_v2_markdown(review, "zh")
    for output in (english, chinese):
        self.assertIn("candidate_000", output)
        self.assertIn("candidate_002", output)
        self.assertIn("LOOP_PIPELINE", output)
        self.assertIn("PPA", output)
        self.assertIn("Credits", output)
        self.assertNotIn("/home/", output)
        self.assertNotIn("Dashboard", output)
```

- [ ] **Step 2: Write immutability RED test**

```python
def test_generation_does_not_modify_machine_evidence(self) -> None:
    before = machine_snapshot(self.evidence_roots)
    generate_v2_review_reports(
        self.spec,
        self.optimization_run,
        self.rejection_run,
        self.acceptance_result,
        self.runs_root,
    )
    after = machine_snapshot(self.evidence_roots)
    self.assertEqual(after, before)
```

- [ ] **Step 3: Run and verify RED**

Run: `python3 -m unittest tests.test_v2_review -q`

Expected: FAIL because V2 review module is missing.

- [ ] **Step 4: Implement canonical ReviewEvidence and two renderers**

The collector calls pure acceptance compute, compares recorded/recomputed status, and aggregates Candidate tree, every round, patches, provider usage, validation, PPA components, comparator keys, budgets, final/fallback, safety rejection, and raw relative refs. Render two files only:

```text
runs/V2_ACCEPTANCE_REPORT.md
runs/V2_ACCEPTANCE_REPORT_CN.md
```

Display a complete Patch at ≤30 lines and the first 30 lines plus summary above 30. Never embed full stdout/stderr/XML/JSONL.

- [ ] **Step 5: Add `review-v2` CLI and run tests**

Run: `python3 -m unittest tests.test_v2_review tests.test_cli -q`

Expected: PASS and CLI summary contains no HTML ref.

- [ ] **Step 6: Commit**

```bash
git add llm4hls_harness/llm4hls_agent/v2_review.py \
  llm4hls_harness/llm4hls_agent/cli.py \
  llm4hls_harness/tests/test_v2_review.py \
  llm4hls_harness/tests/test_cli.py
git commit -m "feat: add flat V2 human-review reports"
```

## Task 10: Documentation, Full Fast Verification, and Real Vitis/DeepSeek Evidence

**Files:**

- Modify: `llm4hls_harness/README.md`
- Modify: `llm4hls_harness/README_CN.md`
- Modify: `doc/materials/07_experiments/2026-07-15-v1-errors-and-v2-readiness.md`
- Generated/ignored: `llm4hls_harness/runs/v2-vector-add-final/*`
- Generated/ignored: `llm4hls_harness/runs/v2-rejection-final/*`
- Generated/ignored: `llm4hls_harness/runs/v2-acceptance/*`
- Generated/ignored: `llm4hls_harness/runs/V2_ACCEPTANCE_REPORT.md`
- Generated/ignored: `llm4hls_harness/runs/V2_ACCEPTANCE_REPORT_CN.md`

- [ ] **Step 1: Update synchronized English/Chinese usage docs**

Document environment variables, `optimize`, deterministic rejection, `accept-v2`, `review-v2`, budgets, exit codes, generated artifacts, and the distinction between safe baseline return and V2 milestone PASS. Add reciprocal language links.

- [ ] **Step 2: Run the complete fast suite before expensive tools**

Run:

```bash
cd /home/ying/CompetitionTrackA/track-A/llm4hls_harness
python3 -m unittest discover -s tests -q
python3 -m compileall -q llm4hls_agent tests
git diff --check
```

Expected: all tests PASS, compileall exits 0, diff check has no output.

- [ ] **Step 3: Run real baseline preflight**

```bash
python3 -m llm4hls_agent run examples/u55c_v2_optimize_task \
  --run-dir runs/v2-vector-add-preflight \
  --vitis-root /home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis \
  --clock-ns 10 --minimum-frequency-mhz 100 \
  --credit-limit 30 \
  --csim-timeout 180 --synth-timeout 900 --cosim-timeout 900
```

Expected: real Vitis 2025.2 CSim/Synth/CoSim/Clock PASS with non-empty latency/II/resource metrics. If it fails or has no optimization headroom, preserve the run, update readiness with the observed cause, adjust the fixture through a new tested commit, and rerun in a new directory.

- [ ] **Step 4: Run the real DeepSeek optimization**

Load the user-managed external environment without printing the key, then run:

```bash
python3 -m llm4hls_agent optimize examples/u55c_v2_optimize_task \
  --run-dir runs/v2-vector-add-final \
  --vitis-root /home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis \
  --clock-ns 10 --minimum-frequency-mhz 100 \
  --model deepseek-v4-pro \
  --scoring-config llm4hls_agent/config/v2_scoring.yaml \
  --max-optimization-rounds 4 \
  --max-no-improvement-rounds 2 \
  --credit-limit 160 \
  --token-budget 32768 \
  --csim-timeout 180 --synth-timeout 900 --cosim-timeout 900
```

Expected milestone evidence: at least two real DeepSeek Candidates fully pass, at least one is promoted over baseline, final best passes independently, no fallback, complete usage. A safe baseline result is retained but does not satisfy acceptance.

- [ ] **Step 5: Run the real deterministic rejection case**

```bash
python3 -m llm4hls_agent reject-v2 examples/u55c_v2_optimize_task \
  --run-dir runs/v2-rejection-final \
  --patch-file examples/u55c_v2_regression.diff \
  --vitis-root /home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis \
  --clock-ns 10 --minimum-frequency-mhz 100 \
  --credit-limit 30 \
  --csim-timeout 180 --synth-timeout 900 --cosim-timeout 900
```

Expected: Patch materializes, real CSim fails, Candidate is rejected, best remains
unchanged, and later Candidate Synth/CoSim stages are not charged.

- [ ] **Step 6: Generate machine acceptance and flat reports**

```bash
python3 -m llm4hls_agent accept-v2 \
  --optimization-run runs/v2-vector-add-final \
  --rejection-run runs/v2-rejection-final \
  --output-dir runs/v2-acceptance
python3 -m llm4hls_agent review-v2 --runs-root runs
```

Expected: `acceptance_result.json` reports `evidence_tier=REAL` and `overall_status=PASS`; exactly two flat Markdown reports exist at `runs/` root.

- [ ] **Step 7: Prove report generation did not mutate evidence**

Hash every non-lock file under the two evidence runs and `v2-acceptance`, regenerate reports, hash again, and require exact equality. Scan both reports for `/home/`, `file://`, HTTP URLs, and HTML/Dashboard refs; expect no match.

- [ ] **Step 8: Update readiness with actual evidence and every encountered issue**

Record exact model/provider, Vitis version, Candidate tree, PPA before/after, Token input/output/cached/total, per-tool calls, credits, stop reason, rejected Candidate, final reserve, evidence paths, failure runs, root causes, and prevention rules. Never replace observed values with design-time estimates.

- [ ] **Step 9: Run final completion verification**

```bash
python3 -m unittest discover -s tests -q
python3 -m compileall -q llm4hls_agent tests
git diff --check
git status --short
```

Additionally run `accept-v2` and `review-v2` once more from the recorded evidence. Completion requires fresh command output for every real Vitis gate and the deterministic acceptance result.

- [ ] **Step 10: Commit implementation/docs, merge only after verified PASS**

```bash
git add llm4hls_harness/README.md llm4hls_harness/README_CN.md \
  doc/materials/07_experiments/2026-07-15-v1-errors-and-v2-readiness.md
git commit -m "docs: record verified V2 Candidate PPA evidence"
```

Do not commit ignored run artifacts or secrets. Merge the feature branch to `main` only after all machine and human acceptance checks pass.

## Completion Audit Checklist

- [ ] Candidate tree has valid parent-child lineage.
- [ ] At least two real DeepSeek Candidates fully pass CSim/Synth/CoSim/Clock.
- [ ] At least one real DeepSeek Candidate strictly improves PPA and is promoted.
- [ ] At least one materialized regression Candidate is rejected without changing best.
- [ ] Every round contains exactly one optimization class.
- [ ] Recomputed Comparator winner equals Registry best/final.
- [ ] Independent final actions are distinct from exploration actions.
- [ ] Token, tool, credit, Trace, Ledger, Registry, action, and Manifest data reconcile.
- [ ] `acceptance_result.json` is REAL/PASS.
- [ ] English/Chinese Markdown reports are flat, consistent, relative-link-only, and HTML-free.
- [ ] Machine evidence is unchanged by review generation.
- [ ] Full fast tests and fresh real Vitis 2025.2 evidence pass.
