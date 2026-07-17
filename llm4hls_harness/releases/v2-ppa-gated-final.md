[简体中文](v2-ppa-gated-final_CN.md)

# V2 PPA-Gated Final Release Record

Release date: 2026-07-17

Release ID: `v2-ppa-gated-final`

Overall status: `PASS / FINAL`

## Release decision

The V2 global champion is frozen as `runs/v2-optimize-final/candidate_004` with
release status `FROZEN_GLOBAL_CHAMPION`. The gated-regression Candidate at
`runs/next-ppa-gated-real/candidate_003` is retained as real DeepSeek/Vitis evidence with
status `VALID_BUT_NOT_GLOBAL_BEST`; it does not replace the global champion.

## Global champion

| Field | Result |
|---|---:|
| Candidate | `candidate_004` |
| Worst latency | 128 |
| Maximum II | 129 |
| Estimated clock | 0.880 ns |
| LUT / FF | 4829 / 129 |
| PPA cost (lower is better) | 0.4006831529 |
| CSim / Synth / CoSim / Clock | PASS / PASS / PASS / PASS |
| Status | `FROZEN_GLOBAL_CHAMPION` |

Champion source SHA-256:
`ee5a0fc7b61f578159487877bde799f14dc53a51ddd40d7013bc9a7988aec65e`.
Champion Patch SHA-256:
`768fa8cc6f402a8a1666d7d2adb7d6602ef6048bcaab14d032a0c08f45e1e1e0`.
Champion Artifact Manifest SHA-256:
`aee4310f17753a359264153a6e1a77f4e8f5f007030abc7edbed5a5b12740419`.

## Frozen optimization policy

```text
CSim -> Synth -> strict PPA gate
  -> run exploration CoSim only when strictly better than the current best
  -> promote only after CoSim, clock, and resource constraints pass
  -> at most 6 optimization Candidates
  -> stop after 2 consecutive no-improvement attempts
  -> independently run complete CSim, Synth, CoSim, and clock checks on final best
```

Correctness and hard constraints always outrank PPA. A Candidate that does not pass the
gate records `CoSim=NOT_RUN` and cannot become best.

## Gated regression

The regression made five real `deepseek-v4-pro` calls without fallback. Its
input/output/cached/total Token counts are 9481/2436/2432/11917. Final
`candidate_003` passed CSim, Synth, CoSim, and clock, but its PPA cost of
0.6005631669 is worse than the champion's 0.4006831529, so it was not promoted to the
global champion.

| Metric | Previous formal run | Gated regression | Difference |
|---|---:|---:|---:|
| CSim calls | 6 | 7 | +1 |
| Synth calls | 6 | 7 | +1 |
| CoSim calls | 6 | 4 | -2 |
| Credits | 150 | 115 | **-35** |

Two fewer CoSim calls save 40 gross credits; one additional CSim and Synth pair costs 5
credits, for a net saving of 35 credits. The real regression therefore demonstrates that
CoSim gating reduces cost while retaining complete final validation.

## Acceptance and integrity

- Global-champion machine acceptance: evaluator `v2.1`, `REAL / PASS`, including
  `exploration_cosim_gated=true`, result SHA-256
  `ff585393de42eb2232eb08a14bc724c7e764c3fa23f99699ef1f3fcbdc9f2196`.
- Gated-regression machine acceptance: evaluator `v2.1`, `REAL / PASS`, including
  `exploration_cosim_gated=true`, result SHA-256
  `83c3dac575832e6f0fdcbdd2ac36751e58d7b67e4c234916cc3952fbe3b35302`.
- Safety-rejection Manifest SHA-256:
  `4000b1bda7763273d39f641c2ef3719c451ff77f5fc07cc0c5fb05e12ac2d815`.
- Both machine acceptances have no reason codes and pass Ledger, Trace, action,
  Candidate-tree, and Manifest consistency checks.
- All 140 fast unit tests, Python compileall, release Hash bindings, and
  `git diff --check` passed before release.

See [`v2-ppa-gated-final.json`](v2-ppa-gated-final.json) for the complete machine-readable
release metadata. Raw evidence:

- [Global-champion run](../runs/v2-optimize-final/experimental_report.md)
- [Global-champion release acceptance](../runs/v2-release-acceptance/acceptance_result.json)
- [Gated-regression run](../runs/next-ppa-gated-real/experimental_report.md)
- [Gated-regression machine acceptance](../runs/v2-gated-regression-acceptance/acceptance_result.json)
- [Safety-rejection run](../runs/v2-safety-rejection-final/v2_rejection_result.json)

V2 is now closed. Development proceeds to `V3_LANGGRAPH_ORCHESTRATION`; gated-regression
`candidate_003` must not replace the frozen champion.
