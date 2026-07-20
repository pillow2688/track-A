#!/usr/bin/env bash
set -Eeuo pipefail

expected_version="2025.2"
vitis_root="${LLM4HLS_VITIS_HLS_ROOT:-/opt/xilinx/2025.2/Vitis}"
settings_path="${vitis_root}/settings64.sh"
output_dir="${LLM4HLS_OUTPUT_DIR:-/outputs}"
preflight_timeout="${LLM4HLS_PREFLIGHT_TIMEOUT_S:-60}"
python_bin="${PYTHON_BIN:-python3}"
preflight_log="$(mktemp)"
trap 'rm -f "${preflight_log}"' EXIT

emit_error() {
    "${python_bin}" -c \
        'import json,sys; print(json.dumps({"status":"ERROR","evidence":"VITIS_PREFLIGHT_ONLY","error_type":sys.argv[1],"detail":sys.argv[2],"hls_actions_started":0}, sort_keys=True), file=sys.stderr)' \
        "$1" "$2"
    exit 3
}

[[ "$(uname -m)" == "x86_64" ]] || emit_error \
    "UNSUPPORTED_ARCHITECTURE" \
    "AMD Vitis 2025.2 preflight requires linux/amd64 (x86_64)."

[[ -f "${settings_path}" ]] || emit_error \
    "VITIS_SETTINGS_MISSING" \
    "Expected external Vitis settings file: ${settings_path}"

mkdir -p "${output_dir}" 2>"${preflight_log}" || emit_error \
    "OUTPUT_MOUNT_UNAVAILABLE" \
    "Cannot create the configured output directory: ${output_dir}"
[[ -w "${output_dir}" ]] || emit_error \
    "OUTPUT_MOUNT_NOT_WRITABLE" \
    "Configured output directory is not writable: ${output_dir}"

# Vendor setup scripts commonly reference optional unset variables.
set +u
set +e
source "${settings_path}" >"${preflight_log}" 2>&1
source_status=$?
set -e
set -u
[[ ${source_status} -eq 0 ]] || emit_error \
    "VITIS_SETTINGS_FAILED" \
    "Sourcing settings64.sh failed; mount the complete 2025.2 install tree at its original absolute path."

vitis_run="$(command -v vitis-run || true)"
[[ -n "${vitis_run}" ]] || emit_error \
    "VITIS_RUN_MISSING" \
    "settings64.sh did not place vitis-run on PATH."
case "${vitis_run}" in
    "${vitis_root}"/*) ;;
    *) emit_error \
        "VITIS_RUN_OUTSIDE_ROOT" \
        "Resolved vitis-run is not inside the configured Vitis root: ${vitis_run}" ;;
esac

if ! timeout \
    --signal=TERM \
    --kill-after=5s \
    "${preflight_timeout}s" \
    "${vitis_run}" --version >"${preflight_log}" 2>&1; then
    emit_error \
        "VITIS_VERSION_COMMAND_FAILED" \
        "vitis-run --version failed or exceeded the ${preflight_timeout}s preflight timeout."
fi

grep -Eq '(^|[^0-9])v?2025\.2([^0-9]|$)' "${preflight_log}" || emit_error \
    "VITIS_VERSION_MISMATCH" \
    "The external vitis-run does not report the required 2025.2 version."

version_line="$(grep -m1 -E 'vitis-run.*2025\.2' "${preflight_log}" || true)"
if [[ -z "${XILINXD_LICENSE_FILE:-}" && -z "${LM_LICENSE_FILE:-}" ]]; then
    printf '%s\n' \
        'WARNING: neither XILINXD_LICENSE_FILE nor LM_LICENSE_FILE is set; licensed stages may fail.' >&2
fi

result_payload="$("${python_bin}" -c \
    'import json,sys; print(json.dumps({"status":"PASS","evidence":"VITIS_PREFLIGHT_ONLY","required_version":"2025.2","version":sys.argv[1],"runtime_source":"EXTERNAL_MOUNT","result_file":"vitis-2025.2-preflight.json","hls_actions_started":0}, sort_keys=True))' \
    "${version_line}")"
printf '%s\n' "${result_payload}" | tee "${output_dir}/vitis-2025.2-preflight.json"
