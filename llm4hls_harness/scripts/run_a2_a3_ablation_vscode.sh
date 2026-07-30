#!/usr/bin/env bash
# Run one fresh A2/A3 ablation group from a VS Code terminal.
# Campaign orchestration only: this file does not change Agent product logic.

set -euo pipefail

GROUP="${1:-}"
HARNESS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_ROOT="$(cd "${HARNESS_ROOT}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_ROOT}/../track-A/.venv/bin/python3}"
ENV_FILE="${ABLATION_ENV_FILE:-${PROJECT_ROOT}/../track-A/llm4hls_harness/.env}"
TASK_IDS="${TASK_IDS:-v3d_fast_001 v3d_fast_008 v3d_fast_009 v3d_fast_012 v3d_fast_018 v3d_fast_021 v3d_fast_026}"
PROGRESS_INTERVAL_SECONDS="${PROGRESS_INTERVAL_SECONDS:-15}"
MAX_RUNTIME_SECONDS="${MAX_RUNTIME_SECONDS:-2400}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "HARD_STOP: Python runtime not executable: ${PYTHON_BIN}" >&2
  exit 2
fi
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "HARD_STOP: Provider env file is missing: ${ENV_FILE}" >&2
  exit 2
fi
if [[ ! "${PROGRESS_INTERVAL_SECONDS}" =~ ^[0-9]+$ ]] \
  || (( PROGRESS_INTERVAL_SECONDS < 5 )); then
  echo "HARD_STOP: PROGRESS_INTERVAL_SECONDS must be an integer of at least 5" >&2
  exit 2
fi
case "${GROUP}" in
  FULL_REF|MINUS_A2|MINUS_A3|MINUS_A2_A3) ;;
  *)
    echo "Usage: $0 {FULL_REF|MINUS_A2|MINUS_A3|MINUS_A2_A3}" >&2
    exit 2
    ;;
esac

# The env file is never printed. It supplies only the configured Provider key
# and endpoint to the child benchmark processes.
set -a
# shellcheck disable=SC1090
. "${ENV_FILE}"
set +a
export LLM4HLS_VITIS_HLS_ROOT="${LLM4HLS_VITIS_HLS_ROOT:-/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis}"

CAMPAIGN_DIR="${CAMPAIGN_DIR:-${HARNESS_ROOT}/runs/a2-a3-ablation-v09-vscode-${GROUP}-$(date -u +%Y%m%dT%H%M%SZ)}"
if [[ -e "${CAMPAIGN_DIR}" ]]; then
  echo "HARD_STOP: fresh campaign directory already exists: ${CAMPAIGN_DIR}" >&2
  exit 3
fi
mkdir -p "${CAMPAIGN_DIR}/logs"

readonly A2_ADMISSION="${PROJECT_ROOT}/docs/experiments/artifacts/2026-07-28-track-a-rc1-head-bound/a2/continuation-v3-admission.json"
readonly A3_STORE="${PROJECT_ROOT}/docs/experiments/artifacts/2026-07-28-track-a-rc1-head-bound/a3/train-only-experience-store.jsonl"
readonly A3_ADMISSION="${PROJECT_ROOT}/docs/experiments/artifacts/2026-07-28-track-a-rc1-head-bound/a3/admission.json"
readonly FULL_MANIFEST="${HARNESS_ROOT}/llm4hls_agent/config/track_a_full_agent_v1.json"

GROUP_ARGS=(--evidence-memory on --max-planner-rounds 4)
case "${GROUP}" in
  FULL_REF)
    GROUP_ARGS+=(
      --continuation-policy enforce
      --continuation-policy-version v2
      --continuation-admission-manifest "${A2_ADMISSION}"
      --experience-mode guided
      --experience-ranker-version v3
      --experience-store "${A3_STORE}"
      --experience-admission-manifest "${A3_ADMISSION}"
      --experience-task-split hidden_like
      --full-agent-manifest "${FULL_MANIFEST}"
    )
    ;;
  MINUS_A2)
    GROUP_ARGS+=(
      --continuation-policy off
      --continuation-policy-version v2
      --experience-mode guided
      --experience-ranker-version v3
      --experience-store "${A3_STORE}"
      --experience-admission-manifest "${A3_ADMISSION}"
      --experience-task-split hidden_like
    )
    ;;
  MINUS_A3)
    GROUP_ARGS+=(
      --continuation-policy enforce
      --continuation-policy-version v2
      --continuation-admission-manifest "${A2_ADMISSION}"
      --experience-mode off
      --experience-ranker-version v3
    )
    ;;
  MINUS_A2_A3)
    GROUP_ARGS+=(
      --continuation-policy off
      --continuation-policy-version v2
      --experience-mode off
      --experience-ranker-version v3
    )
    ;;
esac

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

# The executor buffers child CLI stdout. Read only durable local artifacts so
# VS Code can still show the current Agent operation without exposing prompts,
# source code, provider responses, or credentials.
start_live_monitor() {
  local output_dir="$1"
  local task_id="$2"
  local started_epoch="$3"
  (
    while true; do
      "${PYTHON_BIN}" - "${output_dir}" "${GROUP}" "${task_id}" "${started_epoch}" <<'PY' || true
import json
import sys
import time
from pathlib import Path

output_dir = Path(sys.argv[1])
group, task_id = sys.argv[2], sys.argv[3]
elapsed = int(max(0, time.time() - float(sys.argv[4])))
roots = [path for path in sorted((output_dir / "runs").glob("*")) if path.is_dir()]
if not roots:
    print(f"[ABLATION LIVE] group={group} task={task_id} elapsed={elapsed}s stage=PREPARING_RUN_ARTIFACTS")
    raise SystemExit(0)
root = roots[-1]

def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}

budget = load(root / "budget_state.json")
registry = load(root / "candidate_registry.json")
started, completed = {}, set()
try:
    for raw in (root / "budget_ledger.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("state") == "STARTED" and isinstance(event.get("action_id"), str):
            started[event["action_id"]] = event
        elif event.get("state") == "COMPLETED" and isinstance(event.get("action_id"), str):
            completed.add(event["action_id"])
except OSError:
    pass
pending = [event for action_id, event in started.items() if action_id not in completed]
pending.sort(key=lambda event: int(event.get("sequence", -1)))
candidate_count = len(registry.get("candidates", {})) if isinstance(registry, dict) else 0
best = registry.get("best_candidate_id", "-") if isinstance(registry, dict) else "-"
credits = budget.get("credits_used", "UNKNOWN")
limit = budget.get("credit_limit", "UNKNOWN")
tokens = budget.get("tokens_used", "UNKNOWN")
if pending:
    action = pending[-1]
    stage = f"RUNNING_{str(action.get('kind', 'ACTION')).upper()}"
    candidate = action.get("candidate_id", "UNKNOWN")
else:
    stage = "INDEPENDENT_FINAL_CERTIFICATION" if (root / "v3_prototype_result.json").is_file() else "BETWEEN_ACTIONS"
    candidate = "-"
print(
    f"[ABLATION LIVE] group={group} task={task_id} elapsed={elapsed}s stage={stage} "
    f"candidate={candidate} credits={credits}/{limit} tokens={tokens} "
    f"candidates={candidate_count} best={best or '-'}"
)
PY
      sleep "${PROGRESS_INTERVAL_SECONDS}"
    done
  ) &
  live_monitor_pid=$!
}

"${PYTHON_BIN}" - "${CAMPAIGN_DIR}/campaign_config.json" "${GROUP}" "${TASK_IDS}" "${GROUP_ARGS[@]}" <<'PY'
import json
import sys
from pathlib import Path

task_ids = sys.argv[3].split()
is_020_stress_measurement = task_ids == ["v3d_fast_020"]
payload = {
    "schema_version": "vscode-a2-a3-ablation-campaign.v1",
    "group": sys.argv[2],
    "task_ids": task_ids,
    "resolved_group_arguments": sys.argv[4:],
    "a1": "on",
    "fresh_runs_only": True,
}
if is_020_stress_measurement:
    payload.update({
        "measurement_scope": "DATAFLOW_COSIM_STRESS_ONLY",
        "excluded_from_primary_causal_statistics": True,
        "exclusion_reason": "DETERMINISTIC_BASELINE_COSIM_TIMEOUT_DOMINATES_TREATMENT_EFFECT",
    })
else:
    payload.update({
        "excluded_task": "v3d_fast_020",
        "exclusion_reason": "DETERMINISTIC_BASELINE_COSIM_TIMEOUT_DOMINATES_TREATMENT_EFFECT",
    })
Path(sys.argv[1]).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY

printf '%s\n' "A2_A3_ABLATION_VSCODE_CAMPAIGN_START"
printf '%s\n' "group=${GROUP}"
printf '%s\n' "campaign_dir=${CAMPAIGN_DIR}"
printf '%s\n' "tasks=${TASK_IDS}"
printf '%s\n' "live_progress_interval_seconds=${PROGRESS_INTERVAL_SECONDS}"

for task_id in ${TASK_IDS}; do
  output_dir="${CAMPAIGN_DIR}/${task_id}"
  log_file="${CAMPAIGN_DIR}/logs/${task_id}.log"
  if [[ -e "${output_dir}" ]]; then
    echo "HARD_STOP: fresh output directory already exists: ${output_dir}" >&2
    exit 3
  fi
  printf '\n=== ABLATION %s %s START %s ===\n' "${GROUP}" "${task_id}" "$(date -Is)"
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
      "${GROUP_ARGS[@]}" \
      --max-runtime "${MAX_RUNTIME_SECONDS}"
  ) 2>&1 | tee "${log_file}"
  command_status=${PIPESTATUS[0]}
  set -e
  stop_live_monitor

  "${PYTHON_BIN}" - "${output_dir}" "${GROUP}" "${task_id}" "${command_status}" <<'PY'
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
group, task_id, command_status = sys.argv[2], sys.argv[3], int(sys.argv[4])
rows = []
path = root / "benchmark_results.jsonl"
if path.is_file():
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
record = rows[0] if len(rows) == 1 else None

def formal_agent_terminal(output_dir: Path):
    runs_root = output_dir / "runs"
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
        candidate = run_dir / filename
        if not candidate.is_file():
            continue
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        status = value.get("status") if isinstance(value, dict) else None
        if status in {"DONE", "FAILED"}:
            return {
                "status": status,
                "result_file": filename,
                "stop_reason": value.get("stop_reason"),
            }
    return None

agent_terminal = formal_agent_terminal(root)
agent_failed = bool(agent_terminal and agent_terminal["status"] == "FAILED")
summary = {
    "group": group, "task_id": task_id, "command_status": command_status,
    "terminal": agent_terminal is not None,
    "agent_terminal_status": agent_terminal["status"] if agent_terminal else "UNKNOWN",
    "agent_terminal_result_file": agent_terminal["result_file"] if agent_terminal else "UNKNOWN",
    "ordinary_agent_failure": agent_failed,
    "status": record.get("status") if record else "UNKNOWN",
    "stop_reason": (agent_terminal["stop_reason"] if agent_terminal else None) or (record.get("stop_reason") if record else "MISSING_OR_AMBIGUOUS_BENCHMARK_RESULT"),
    "planner_calls": record.get("model_calls") if record else "UNKNOWN",
    "tokens": record.get("tokens_used") if record else "UNKNOWN",
    "credits": record.get("credits_used") if record else "UNKNOWN",
    "b2_final": record.get("final_validation_success") if record else False,
}
print("ABLATION_TASK_RESULT " + json.dumps(summary, ensure_ascii=False, sort_keys=True))
(root.parent / "campaign_progress.jsonl").open("a", encoding="utf-8").write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
raise SystemExit(10 if not summary["terminal"] or (command_status != 0 and not agent_failed) else 0)
PY
  result_status=$?
  if (( result_status != 0 )); then
    echo "HARD_STOP: ${GROUP}/${task_id} has no complete terminal result; no later task was started." >&2
    exit "${result_status}"
  fi
  printf '=== ABLATION %s %s END %s ===\n' "${GROUP}" "${task_id}" "$(date -Is)"
done

echo "A2_A3_ABLATION_VSCODE_CAMPAIGN_COMPLETE group=${GROUP}"
