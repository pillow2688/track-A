#!/usr/bin/env bash
# Run the public v3d-fast Full Agent campaign serially from a VSCode terminal.
# This is campaign orchestration only: it does not change Agent product logic.

set -euo pipefail

HARNESS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_ROOT="$(cd "${HARNESS_ROOT}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_ROOT}/../track-A/.venv/bin/python3}"
ENV_FILE="${FULL_AGENT_ENV_FILE:-${PROJECT_ROOT}/../track-A/llm4hls_harness/.env}"
CAMPAIGN_DIR="${CAMPAIGN_DIR:-${HARNESS_ROOT}/runs/full-agent-v09-vscode-$(date -u +%Y%m%dT%H%M%SZ)}"
START_INDEX="${START_INDEX:-2}"
END_INDEX="${END_INDEX:-28}"
PROGRESS_INTERVAL_SECONDS="${PROGRESS_INTERVAL_SECONDS:-15}"
TASK_MAX_RUNTIME_SECONDS="${TASK_MAX_RUNTIME_SECONDS:-2400}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "HARD_STOP: Python runtime not executable: ${PYTHON_BIN}" >&2
  exit 2
fi
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "HARD_STOP: Provider env file is missing: ${ENV_FILE}" >&2
  exit 2
fi
if [[ ! "${START_INDEX}" =~ ^[0-9]+$ ]] || [[ ! "${END_INDEX}" =~ ^[0-9]+$ ]] \
  || (( START_INDEX < 1 || END_INDEX > 28 || START_INDEX > END_INDEX )); then
  echo "HARD_STOP: START_INDEX and END_INDEX must satisfy 1 <= start <= end <= 28" >&2
  exit 2
fi
if [[ ! "${PROGRESS_INTERVAL_SECONDS}" =~ ^[0-9]+$ ]] \
  || (( PROGRESS_INTERVAL_SECONDS < 5 )); then
  echo "HARD_STOP: PROGRESS_INTERVAL_SECONDS must be an integer of at least 5" >&2
  exit 2
fi
if [[ ! "${TASK_MAX_RUNTIME_SECONDS}" =~ ^[0-9]+([.][0-9]+)?$ ]] \
  || ! awk -v value="${TASK_MAX_RUNTIME_SECONDS}" 'BEGIN { exit !(value + 0 > 0) }'; then
  echo "HARD_STOP: TASK_MAX_RUNTIME_SECONDS must be a positive number" >&2
  exit 2
fi

# The env file is never printed. It supplies only the configured Provider key
# and endpoint to the child benchmark processes.
set -a
# shellcheck disable=SC1090
. "${ENV_FILE}"
set +a
export LLM4HLS_VITIS_HLS_ROOT="${LLM4HLS_VITIS_HLS_ROOT:-/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis}"
# Development-only, evidence-bound guard for a launched RTL simulation that
# exposes a testcase counter but makes no completed-test progress.  Set to 0
# to retain the full configured CoSim timeout.
export LLM4HLS_COSIM_NO_PROGRESS_TIMEOUT_S="${LLM4HLS_COSIM_NO_PROGRESS_TIMEOUT_S:-300}"
export LLM4HLS_BASELINE_COSIM_PROBE_TIMEOUT_S="${LLM4HLS_BASELINE_COSIM_PROBE_TIMEOUT_S:-300}"

# The benchmark executor buffers the child CLI's stdout until the task ends.
# This monitor reads only durable, local run artifacts and never changes Agent
# state. It deliberately prints no prompts, source code, provider data, or
# environment values.
live_monitor_pid=""
stop_live_monitor() {
  if [[ -n "${live_monitor_pid}" ]]; then
    kill "${live_monitor_pid}" 2>/dev/null || true
    wait "${live_monitor_pid}" 2>/dev/null || true
    live_monitor_pid=""
  fi
}
trap stop_live_monitor EXIT
trap 'stop_live_monitor; exit 130' INT TERM

start_live_monitor() {
  local output_dir="$1"
  local task_id="$2"
  local started_epoch="$3"
  (
    while true; do
      "${PYTHON_BIN}" - "${output_dir}" "${task_id}" "${started_epoch}" <<'PY' || true
import json
import sys
import time
from pathlib import Path

output_dir = Path(sys.argv[1])
task_id = sys.argv[2]
started_epoch = float(sys.argv[3])
elapsed = int(max(0, time.time() - started_epoch))
run_roots = sorted((output_dir / "runs").glob("*"))
run_roots = [path for path in run_roots if path.is_dir()]
if not run_roots:
    if (output_dir / "benchmark_plan.json").is_file():
        print(
            f"[AGENT LIVE] task={task_id} elapsed={elapsed}s "
            "stage=WAITING_FOR_RUNNER_OR_VITIS_SERIAL_LOCK"
        )
        raise SystemExit(0)
    print(
        f"[AGENT LIVE] task={task_id} elapsed={elapsed}s "
        "stage=PREPARING_RUN_ARTIFACTS"
    )
    raise SystemExit(0)

root = run_roots[-1]
budget = {}
try:
    budget = json.loads((root / "budget_state.json").read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, json.JSONDecodeError):
    pass
registry = {}
try:
    registry = json.loads((root / "candidate_registry.json").read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, json.JSONDecodeError):
    pass

started = {}
completed = set()
last_event = None
ledger = root / "budget_ledger.jsonl"
try:
    for raw in ledger.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        last_event = event
        action_id = event.get("action_id")
        if event.get("state") == "STARTED" and isinstance(action_id, str):
            started[action_id] = event
        elif event.get("state") == "COMPLETED" and isinstance(action_id, str):
            completed.add(action_id)
except OSError:
    pass

pending = [event for action_id, event in started.items() if action_id not in completed]
pending.sort(key=lambda event: int(event.get("sequence", -1)))
candidate_count = len(registry.get("candidates", {})) if isinstance(registry, dict) else 0
best_candidate = registry.get("best_candidate_id") if isinstance(registry, dict) else None
credits = budget.get("credits_used", "UNKNOWN")
credit_limit = budget.get("credit_limit", "UNKNOWN")
tokens = budget.get("tokens_used", "UNKNOWN")

if pending:
    event = pending[-1]
    progress_suffix = ""
    if str(event.get("kind", "")).casefold() == "cosim":
        progress_path = (
            root / "actions" / str(event.get("action_id", ""))
            / "work" / "cosim_progress.json"
        )
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            progress = {}
        rtl = progress.get("rtl_test_progress") if isinstance(progress, dict) else None
        if isinstance(rtl, dict):
            progress_suffix = (
                f" rtl_tests={rtl.get('completed', '?')}/{rtl.get('total', '?')}"
                f" no_progress={progress.get('no_progress_elapsed_seconds', '?')}s"
                f" guard={progress.get('configured_no_progress_timeout_seconds', '?')}s"
            )
    print(
        f"[AGENT LIVE] task={task_id} elapsed={elapsed}s "
        f"stage=RUNNING_{str(event.get('kind', 'ACTION')).upper()} "
        f"candidate={event.get('candidate_id', 'UNKNOWN')} "
        f"action={str(event.get('action_id', 'UNKNOWN'))[:12]} "
        f"credits={credits}/{credit_limit} tokens={tokens} "
        f"candidates={candidate_count} best={best_candidate or '-'}{progress_suffix}"
    )
elif (root / "v3_certified_result.json").is_file():
    print(
        f"[AGENT LIVE] task={task_id} elapsed={elapsed}s "
        f"stage=TERMINAL_ARTIFACT_WRITTEN credits={credits}/{credit_limit} "
        f"tokens={tokens} candidates={candidate_count} best={best_candidate or '-'}"
    )
elif (root / "v3_prototype_result.json").is_file():
    print(
        f"[AGENT LIVE] task={task_id} elapsed={elapsed}s "
        f"stage=INDEPENDENT_FINAL_CERTIFICATION credits={credits}/{credit_limit} "
        f"tokens={tokens} candidates={candidate_count} best={best_candidate or '-'}"
    )
else:
    kind = "UNKNOWN"
    candidate = "UNKNOWN"
    if isinstance(last_event, dict):
        kind = str(last_event.get("kind", kind)).upper()
        candidate = str(last_event.get("candidate_id", candidate))
    print(
        f"[AGENT LIVE] task={task_id} elapsed={elapsed}s "
        f"stage=BETWEEN_ACTIONS last={kind} candidate={candidate} "
        f"credits={credits}/{credit_limit} tokens={tokens} "
        f"candidates={candidate_count} best={best_candidate or '-'}"
    )
PY
      sleep "${PROGRESS_INTERVAL_SECONDS}"
    done
  ) &
  live_monitor_pid=$!
}

mkdir -p "${CAMPAIGN_DIR}/logs"
printf '%s\n' "FULL_AGENT_V09_VSCODE_CAMPAIGN_START"
printf '%s\n' "campaign_dir=${CAMPAIGN_DIR}"
printf '%s\n' "range=${START_INDEX}..${END_INDEX}"
printf '%s\n' "live_progress_interval_seconds=${PROGRESS_INTERVAL_SECONDS}"
printf '%s\n' "task_max_runtime_seconds=${TASK_MAX_RUNTIME_SECONDS}"
printf '%s\n' "task_001_existing_certified_run=${HARNESS_ROOT}/runs/full-agent-v09-full-corpus-20260729-a01/001b_v3d_fast_001_network_retry/runs/v3d_fast_001--deepseek-v4-pro--r001--4938c2c19fa4"

for index in $(seq "${START_INDEX}" "${END_INDEX}"); do
  task_id=$(printf 'v3d_fast_%03d' "${index}")
  slot=$(printf '%03d_%s' "${index}" "${task_id}")
  output_dir="${CAMPAIGN_DIR}/${slot}"
  log_file="${CAMPAIGN_DIR}/logs/${slot}.log"

  if [[ -e "${output_dir}" ]]; then
    echo "HARD_STOP: fresh output directory already exists: ${output_dir}" >&2
    exit 3
  fi

  printf '\n=== TASK %03d/028 %s START %s ===\n' "${index}" "${task_id}" "$(date -Is)"
  start_live_monitor "${output_dir}" "${task_id}" "$(date +%s)"
  set +e
  (
    cd "${HARNESS_ROOT}"
    "${PYTHON_BIN}" -m llm4hls_agent.v3_batch_benchmark \
      --corpus task_corpus/v3d-fast \
      --output-dir "${output_dir}" \
      --split all \
      --task "${task_id}" \
      --models deepseek-v4-pro \
      --repeats 1 \
      --backend vitis \
      --validation-profile fast-experiment \
      --final-validation-policy task_contract \
      --evidence-memory on \
      --max-planner-rounds 4 \
      --continuation-policy enforce \
      --continuation-policy-version v2 \
      --continuation-admission-manifest "${PROJECT_ROOT}/docs/experiments/artifacts/2026-07-28-track-a-rc1-head-bound/a2/continuation-v3-admission.json" \
      --full-agent-manifest llm4hls_agent/config/track_a_full_agent_v1.json \
      --experience-mode guided \
      --experience-ranker-version v3 \
      --experience-store "${PROJECT_ROOT}/docs/experiments/artifacts/2026-07-28-track-a-rc1-head-bound/a3/train-only-experience-store.jsonl" \
      --experience-admission-manifest "${PROJECT_ROOT}/docs/experiments/artifacts/2026-07-28-track-a-rc1-head-bound/a3/admission.json" \
      --experience-task-split hidden_like \
      --max-runtime "${TASK_MAX_RUNTIME_SECONDS}"
  ) 2>&1 | tee "${log_file}"
  command_status=${PIPESTATUS[0]}
  set -e
  stop_live_monitor

  "${PYTHON_BIN}" - "${output_dir}" "${task_id}" "${command_status}" <<'PY'
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
task_id = sys.argv[2]
command_status = int(sys.argv[3])
result_path = output_dir / "benchmark_results.jsonl"
record = None
if result_path.is_file():
    rows = [line for line in result_path.read_text(encoding="utf-8").splitlines() if line]
    if len(rows) == 1:
        record = json.loads(rows[0])

def formal_agent_terminal(root: Path):
    runs_root = root / "runs"
    run_dirs = [path for path in sorted(runs_root.glob("*")) if path.is_dir()]
    if len(run_dirs) != 1:
        return None
    run_dir = run_dirs[0]
    required = (
        run_dir / "budget_ledger.jsonl",
        run_dir / "candidate_registry.json",
        run_dir / "control" / "package_manifest.json",
        run_dir / "v3_prototype_result.json",
    )
    if not all(path.is_file() for path in required):
        return None
    for filename in ("v3_certified_result.json", "v3_prototype_result.json"):
        path = run_dir / filename
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        status = value.get("status") if isinstance(value, dict) else None
        if status in {"DONE", "FAILED"}:
            return {
                "status": status,
                "result_file": filename,
                "stop_reason": value.get("stop_reason"),
                "run_dir": str(run_dir),
            }
    return None

agent_terminal = formal_agent_terminal(output_dir)
if record is None or agent_terminal is None:
    summary = {
        "task_id": task_id,
        "terminal": False,
        "hard_stop": True,
        "reason": "MISSING_OR_AMBIGUOUS_BENCHMARK_RESULT",
        "command_status": command_status,
    }
else:
    evidence_level = record.get("evidence_level")
    status = record.get("status")
    agent_failed = agent_terminal["status"] == "FAILED"
    # A durable Agent FAILED result is a measurement outcome, not an
    # infrastructure failure.  Its B1 package, Candidate Registry and Ledger
    # were checked above.  Continue the corpus and keep the failure visible.
    hard_stop = False if agent_failed else (
        command_status != 0
        or status == "ERROR"
        or evidence_level == "NO_TERMINAL_RESULT"
        or record.get("provenance_validation") is False
    )
    summary = {
        "task_id": task_id,
        "terminal": True,
        "agent_terminal_status": agent_terminal["status"],
        "agent_terminal_result_file": agent_terminal["result_file"],
        "ordinary_agent_failure": agent_failed,
        "hard_stop": hard_stop,
        "command_status": command_status,
        "status": status,
        "stop_reason": agent_terminal["stop_reason"] or record.get("stop_reason"),
        "planner_calls": record.get("model_calls"),
        "tokens": record.get("tokens_used"),
        "credits": record.get("credits_used"),
        "search_final": record.get("fresh_final_success"),
        "b2_final": record.get("final_validation_success"),
        "e2e_success": record.get("e2e_success"),
        "wall_time_s": record.get("wall_time_s"),
        "run_dir": record.get("run_dir"),
    }

print("TASK_RESULT " + json.dumps(summary, ensure_ascii=False, sort_keys=True))
(output_dir.parent / "campaign_progress.jsonl").open("a", encoding="utf-8").write(
    json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n"
)
raise SystemExit(10 if summary["hard_stop"] else 0)
PY
  result_status=$?
  if (( result_status != 0 )); then
    echo "HARD_STOP: ${task_id} requires investigation; no later task was started." >&2
    exit "${result_status}"
  fi
  printf '=== TASK %03d/028 %s END %s ===\n' "${index}" "${task_id}" "$(date -Is)"
done

echo "FULL_AGENT_V09_VSCODE_CAMPAIGN_COMPLETE"
