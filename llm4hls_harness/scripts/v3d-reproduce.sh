#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_root="${LLM4HLS_OUTPUT_DIR:-/outputs}"
corpus_root="${LLM4HLS_CORPUS_ROOT:-${project_root}/task_corpus/v3d-fast}"
official_corpus_root="${LLM4HLS_OFFICIAL_CORPUS_ROOT:-/official-corpus}"
source_root="${LLM4HLS_SOURCE_ROOT:-/workspace}"
python_bin="${PYTHON_BIN:-python3}"

usage() {
    printf '%s\n' \
        'Usage: scripts/v3d-reproduce.sh demo-smoke|official-smoke|quick-tests|real-preflight' \
        '' \
        '  demo-smoke      Run one deterministic V3-D orchestration fixture.' \
        '                  This is never real HLS/model evidence.' \
        '  official-smoke  Run deterministic fixture orchestration over the three' \
        '                  externally mounted official public tasks. This is never' \
        '                  real HLS/model evidence.' \
        '  quick-tests     Run all unittest quick tests from a read-only source mount.' \
        '                  This does not start Vitis or call an LLM.' \
        '  real-preflight  Verify an external Vitis 2025.2 runtime only.' \
        '                  This starts zero CSim/Synth/CoSim actions.'
}

ensure_writable_output() {
    mkdir -p "${output_root}"
    if [[ ! -w "${output_root}" ]]; then
        printf 'Output directory is not writable: %s\n' "${output_root}" >&2
        exit 3
    fi
}

case "${1:-}" in
    demo-smoke)
        ensure_writable_output
        exec timeout \
            --signal=TERM \
            --kill-after=15s \
            "${LLM4HLS_DEMO_SMOKE_TIMEOUT_S:-180}s" \
            "${python_bin}" -m llm4hls_agent.v3_batch_benchmark \
            --corpus "${corpus_root}" \
            --output-dir "${output_root}/v3d-demo-smoke" \
            --models deterministic-fixture-v1 \
            --backend demo \
            --max-tasks 1 \
            --max-runtime "${LLM4HLS_DEMO_BATCH_TIMEOUT_S:-120}" \
            --resume
        ;;
    official-smoke)
        ensure_writable_output
        if [[ ! -d "${official_corpus_root}" ]]; then
            printf 'Official public corpus mount is missing: %s\n' \
                "${official_corpus_root}" >&2
            exit 3
        fi
        for task_id in \
            projection_bugfix \
            dotProduct_optimize \
            residual_stream_deadlock
        do
            if [[ ! -f "${official_corpus_root}/${task_id}/task.toml" ]]; then
                printf 'Official public task is missing: %s\n' \
                    "${official_corpus_root}/${task_id}/task.toml" >&2
                exit 3
            fi
        done

        official_output="${output_root}/v3d-official-three-smoke"
        timeout \
            --signal=TERM \
            --kill-after=15s \
            "${LLM4HLS_OFFICIAL_SMOKE_TIMEOUT_S:-240}s" \
            "${python_bin}" -m llm4hls_agent.v3_batch_benchmark \
            --corpus "${official_corpus_root}" \
            --output-dir "${official_output}" \
            --models deterministic-fixture-v1 \
            --backend deterministic \
            --task projection_bugfix \
            --task dotProduct_optimize \
            --task residual_stream_deadlock \
            --max-tasks 3 \
            --max-runtime "${LLM4HLS_OFFICIAL_BATCH_TIMEOUT_S:-180}" \
            --resume

        "${python_bin}" - "${official_output}" <<'PY'
import json
import sys
from pathlib import Path

output = Path(sys.argv[1])
summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
records = [
    json.loads(line)
    for line in (output / "benchmark_results.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
expected = {
    "projection_bugfix",
    "dotProduct_optimize",
    "residual_stream_deadlock",
}
current_ids = {str(item.get("task_id")) for item in records if item.get("resumed") is not None}
deterministic = summary.get("by_evidence_class", {}).get("DETERMINISTIC", {})
real = summary.get("real_evidence_headline", {})
selection = summary.get("selection", {})
fixture_calls = deterministic.get("usage", {}).get("tool_calls", {})
if selection.get("tasks_selected") != 3 or selection.get("planned_runs") != 3:
    raise SystemExit("official smoke did not select exactly three tasks")
if deterministic.get("runs") != 3 or real.get("runs") != 0:
    raise SystemExit("official smoke evidence classes are not 3 DETERMINISTIC / 0 REAL")
if not expected.issubset(current_ids):
    raise SystemExit(f"official smoke task set mismatch: {sorted(current_ids)}")
if any(item.get("evidence_class") != "DETERMINISTIC" for item in records[-3:]):
    raise SystemExit("official smoke emitted a non-deterministic fixture row")
receipt = {
    "status": "PASS",
    "evidence": "DETERMINISTIC_FIXTURE_ONLY",
    "real_evidence_runs": 0,
    "tasks": sorted(expected),
    "records": 3,
    "hls_actions_started": 0,
    "llm_calls_started": 0,
    "synthetic_tool_call_records": {
        key: int(fixture_calls.get(key, 0))
        for key in ("csim", "synth", "cosim")
    },
    "summary_ref": "summary.json",
    "results_ref": "benchmark_results.jsonl",
}
(output / "official_smoke_receipt.json").write_text(
    json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
PY
        ;;
    quick-tests)
        if [[ ! -d "${source_root}/tests" ]]; then
            printf 'Read-only source mount with tests is missing: %s\n' \
                "${source_root}/tests" >&2
            exit 3
        fi
        cd "${source_root}"
        exec timeout \
            --signal=TERM \
            --kill-after=15s \
            "${LLM4HLS_QUICK_TEST_TIMEOUT_S:-900}s" \
            env PYTHONPATH="${source_root}" \
            "${python_bin}" -m unittest discover \
            -s "${source_root}/tests" \
            -t "${source_root}" \
            -p 'test_*.py' \
            -v
        ;;
    real-preflight)
        export LLM4HLS_OUTPUT_DIR="${output_root}"
        exec "${project_root}/scripts/vitis-2025.2-preflight.sh"
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage >&2
        exit 64
        ;;
esac
