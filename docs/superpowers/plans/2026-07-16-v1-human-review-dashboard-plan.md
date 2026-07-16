# V1 Human Review Dashboard Implementation Plan

## Goal

Generate three flat, deterministic human-review files under `llm4hls_harness/runs/`
from the four existing real V1 runs without invoking an LLM/Vitis or modifying any
machine-readable evidence.

## Steps

1. Refactor acceptance evaluation into a pure compute function and retain the existing
   writing wrapper unchanged.
2. Add failing tests for flat output names, exact aggregate statistics, per-case evidence,
   Patch display limits, relative links, output determinism, and input immutability.
3. Implement a read-only `ReviewEvidence` collector that recomputes acceptance checks,
   compares them with the recorded `acceptance_result.json`, and aggregates diagnostics,
   model usage, budgets, candidate state, validation, Patch, Trace, and raw links.
4. Implement complete English and Chinese Markdown renderers from the same in-memory model.
5. Implement an escaped, self-contained bilingual static HTML renderer with no external
   assets.
6. Add a `review-v1` CLI command that writes only the three flat human-review files.
7. Update English/Chinese project documentation and the V1 readiness retrospective.
8. Run the full fast test suite, compileall, `git diff --check`, and one offline generation
   over the four real runs; verify exact totals, relative links, report consistency, and
   unchanged machine evidence.

## Verification Contract

- 4 cases: 3 HLS repair PASS and 1 safety rejection PASS.
- Real LLM calls: 3.
- Tokens: input 2700, output 676, cached input 384, total 3376.
- Tools: csim 7, synth 4, cosim 3, total 14.
- Credits: used 83, remaining 237 of 320.
- Final csim/synth/cosim/clock: 3/3 PASS.
- No generated human-review file contains an absolute path.
- Every JSON/JSONL/Manifest/ledger/Trace/registry/action/Candidate file retains its
  pre-generation hash, size, and modification time.
