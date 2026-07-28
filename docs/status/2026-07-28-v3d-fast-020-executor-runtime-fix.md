# v3d_fast_020 Executor Runtime Fix

Date: 2026-07-28

## Scope

This change only addresses the executor deadline, process-tree cleanup, and
partial-artifact reporting failure exposed by `v3d_fast_020`.

It does not change A1 evidence behavior, A2 continuation decisions, A3
ranking/guidance, Planner Prompt content, Gate, Admission, Experience Store,
Full Agent Manifest, Corpus tasks, scoring, or task budgets.

No DeepSeek request or Vitis action was executed during implementation or
verification.

## Implemented runtime contract

- One `run_deadline_monotonic` is created at single-task start.
- Batch and CLI pass the same absolute monotonic deadline into Planner,
  CSim, Synth, CoSim, search closeout, and Independent Final Certification.
- Every new action uses:

  ```text
  effective_timeout =
      min(configured_timeout,
          run_deadline_monotonic - monotonic_now - cleanup_reserve_seconds)
  ```

- `cleanup_reserve_seconds` defaults to 30 seconds.
- CoSim requires an effective execution window of at least 60 seconds by
  default, capped by its configured timeout.
- Runtime gates execute before Ledger reservation and before the backend or
  provider starts.
- A cached completed action may still be read without starting a new action.
- Stable not-started reasons are:
  - `TOOL_NOT_STARTED_INSUFFICIENT_RUNTIME`
  - `COSIM_NOT_STARTED_INSUFFICIENT_RUNTIME`
  - `FINAL_CLOSURE_UNAFFORDABLE_RUNTIME`
- A not-started action is not recorded as PASS, ordinary tool FAIL, or a
  charged Ledger action.

## Executor process ownership and cleanup

- Each single-task CLI is launched in an independent POSIX session/process
  group.
- A hard outer timeout targets only that run-owned PGID:
  1. send `SIGTERM`;
  2. wait a bounded grace interval;
  3. send `SIGKILL` when any group member remains;
  4. reap the direct child and Linux subreaper-owned descendants;
  5. confirm the target PGID is empty before producing the timeout report.
- Cleanup never uses process names, `pkill`, or `killall`.
- The report records PID, PGID, TERM/KILL decisions, reaped PIDs, remaining
  members, return code, and `process_tree_cleaned`.
- Controlled tests prove that an unrelated independent session is not killed.

## Partial-artifact reporting

`recover_partial_run_report()` is read-only and uses the append-only Ledger as
the accounting authority. It then fail-closed validates:

- Ledger sequence and action lifecycle;
- completed ToolResult identity, result digest, and attached artifact hashes;
- completed Planner STARTED/COMPLETED journals;
- Planner input, request, outcome/rejection, deterministic identity, hashes,
  and usage agreement;
- pending actions;
- trace node/tool position;
- Candidate Registry state and final freeze state.

Completed usage and pending reservations are reported separately. If the
artifacts cannot prove a value, the report writes `UNKNOWN`, never numeric
zero. An executor timeout row uses:

```text
status = EXECUTOR_TIMEOUT
stop_reason = EXECUTOR_TIMEOUT_AND_INCOMPLETE_ARTIFACT
```

It remains ineligible for success or REAL terminal evidence.

## Historical v3d_fast_020 read-only recovery

Source run:

```text
llm4hls_harness/runs/full-agent-v3d-fast-targeted-rerun-20260728-a01/
v3d_fast_020/runs/
v3d_fast_020--deepseek-v4-pro--r001--3cd15b547f31
```

Recovered facts:

- Baseline CSim: PASS
- Baseline Synth: PASS
- Baseline CoSim: TIMEOUT
- Planner calls: 1
- Tokens: 2594
  - input: 2120
  - output: 474
  - cached input: 256
- Completed Agent Credits: 26
- Pending Candidate CoSim reserve: 20
- Conservative accounted Credits: 46
- `candidate_001`: created; CSim PASS
- Last completed Graph node: `candidate_csim`
- Last started tool: Candidate CoSim
- Unfinished action:
  `c3ba394acb3c22fa91439c9c5042d700e38b2daaffa7dd20fce61aa1790a047b`
- Candidate frozen: false
- Agent terminal present: false

The source Ledger remained byte-identical:

```text
8beda8d191437a66257e7e37d67d68441734a761eef170ea25cbf261fc21c372
```

Machine report:

```text
docs/status/2026-07-28-v3d-fast-020-partial-recovery.json
```

The incomplete run is not successful and is not authorized for direct
Resume. A later retry must use a fresh run directory.

## Tests

Focused current-code regression:

```text
python -m unittest \
  tests.test_v3_batch_benchmark \
  tests.test_v3_prototype \
  tests.test_executor_runtime \
  tests.test_final_certification

110 tests: PASS
```

Planner runtime forwarding and executor runtime tests:

```text
python -m unittest \
  tests.test_v3_planner_action \
  tests.test_executor_runtime

28 tests: PASS
```

Complete suite:

```text
python -m unittest discover -s tests -t .

725 tests: PASS
```

Final checks:

- `git diff --check`: PASS
- Vitis/XSim process scan: no matching live process
- HEAD: `d3f782887c5dc99d5e8b32b29d3d95f81008c680`
- Full Agent Manifest SHA-256:
  `3bff613e626a1e84608d789109d62ec1a60a8e6cd90fb8de2146d0d4e6f73a46`

The worktree was already dirty from earlier authorized work. No cleanup,
commit, merge, or push was performed.

## Files touched in this fix

- `llm4hls_harness/llm4hls_agent/runtime_control.py`
- `llm4hls_harness/llm4hls_agent/tools.py`
- `llm4hls_harness/llm4hls_agent/workflow.py`
- `llm4hls_harness/llm4hls_agent/v3_prototype.py`
- `llm4hls_harness/llm4hls_agent/v3_prototype_cli.py`
- `llm4hls_harness/llm4hls_agent/v3_planner_action.py`
- `llm4hls_harness/llm4hls_agent/v3_openai_planner.py`
- `llm4hls_harness/llm4hls_agent/openai_provider.py`
- `llm4hls_harness/llm4hls_agent/final_certification.py`
- `llm4hls_harness/llm4hls_agent/v3_batch_benchmark.py`
- `llm4hls_harness/tests/test_executor_runtime.py`
- `llm4hls_harness/tests/test_v3_planner_action.py`
- `docs/status/2026-07-28-v3d-fast-020-partial-recovery.json`
- `docs/status/2026-07-28-v3d-fast-020-executor-runtime-fix.md`
