# Track A RC1 freeze audit

Date: 2026-07-28

## Scope

This audit covers the pre-RC release snapshot, the latest validated combined
28-task evidence, and the binding boundary for the Full Agent manifest and the
A2/A3 admission artifacts. It does not modify or re-run any historical task.

## Snapshot and evidence checks

- Snapshot recovery drill: PASS. Applying `working_tree.patch` and the
  `source_overlay/` to base commit
  `d3f782887c5dc99d5e8b32b29d3d95f81008c680` reproduced all 249
  runtime-critical files recorded by the snapshot.
- Runtime aggregate:
  `693bdd90ea5be0ed10395c65bd0ef11eab7ef77c421c1a4cccf583f164973c98`.
- Executor implementation:
  `e54dabcbdfccf35dfb2aaa2cb4e2d522425a2d6a4964e08caa4aaf42e3943ee2`.
- Historical latest-run critical-tree aggregate:
  `a680bb5330394eaeae578c8c5128026a71f53ad59bb8dd852fad5d93e91a4d07`.
- 28 unique tasks and runs, 308 evidence files, and 84 independent-final-
  certification toolchain files were hash-validated.
- The pre-RC snapshot validator passed.
- No live API credential, token, password, or private key was found. The only
  credential-shaped value is a documented synthetic test fixture.
- `release_tools/` contributes zero entries to the Agent runtime fingerprint.
- DeepSeek calls: 0. Vitis actions: 0.

## Binding boundary

- A2 Gate and A2 Admission bind the exact Git commit.
- A3 Admission binds the exact Git commit.
- The frozen A3 Store and A3 Gate do not bind Git HEAD.
- The Full Agent manifest binds the artifact paths and hashes.

Because the admission files contain the commit that must already exist, they
cannot be committed inside that same commit without a self-reference cycle.
RC1 therefore uses a two-part immutable release:

1. an annotated Git tag over the final runtime/source commit; and
2. a separately hashed, read-only evidence bundle containing the post-commit
   HEAD-bound A2/A3 admissions and Full Agent manifest.

The evidence bundle must be supplied as the recorded runtime overlay. The tag
message binds its aggregate hash. This is a release packaging boundary only;
no A1/A2/A3 policy, Planner prompt, Executor, Corpus, budget, or validation
rule is changed.

## Evidence claims

- First-attempt strict success: 25/28.
- Latest validated combined coverage: 28/28.
- `COMBINED_COVERAGE_NOT_SINGLE_BATCH_28X1`.
- The historical 28-task evidence belongs to the recorded pre-RC fingerprint.
- It is no-harm evidence, not a same-fingerprint paired causal ablation.
