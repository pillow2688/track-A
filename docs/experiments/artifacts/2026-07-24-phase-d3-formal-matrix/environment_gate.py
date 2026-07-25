#!/usr/bin/env python3
"""Record the D3 environment gate without persisting any secret value."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


EXPECTED_BASE_URL = "https://api.deepseek.com"
EXPECTED_MODEL = "deepseek-v4-pro"
EXPECTED_VITIS_ROOT = Path(
    "/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis"
)


def main() -> int:
    artifact_dir = Path(__file__).resolve().parent
    base_url = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
    model = os.environ.get("LLM4HLS_MODEL", "")
    key_present = bool(os.environ.get("OPENAI_API_KEY", ""))
    vitis_raw = os.environ.get("LLM4HLS_VITIS_HLS_ROOT", "")
    vitis_root = Path(vitis_raw).expanduser() if vitis_raw else None
    vitis_run = (
        vitis_root / "bin/vitis-run" if vitis_root is not None else None
    )
    version_summary = ""
    if vitis_run is not None and vitis_run.is_file():
        completed = subprocess.run(
            [str(vitis_run), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        version_summary = completed.stdout or completed.stderr
    rotation_attested = (
        os.environ.get("LLM4HLS_KEY_ROTATED_AFTER_DISCLOSURE", "") == "1"
    )
    checks = {
        "base_url_set": bool(base_url),
        "base_url_expected": base_url == EXPECTED_BASE_URL,
        "api_key_set": key_present,
        "model_set": bool(model),
        "model_expected": model == EXPECTED_MODEL,
        "vitis_root_set": vitis_root is not None,
        "vitis_root_expected": bool(
            vitis_root is not None
            and vitis_root.resolve() == EXPECTED_VITIS_ROOT.resolve()
        ),
        "vitis_run_available": bool(
            vitis_run is not None and vitis_run.is_file()
        ),
        "vitis_version_2025_2": "2025.2" in version_summary,
        "key_rotation_attested_after_disclosure": rotation_attested,
    }
    core_ready = all(
        checks[key]
        for key in (
            "base_url_set",
            "base_url_expected",
            "api_key_set",
            "model_set",
            "model_expected",
            "vitis_root_set",
            "vitis_root_expected",
            "vitis_run_available",
            "vitis_version_2025_2",
        )
    )
    status = (
        "PASS"
        if core_ready and rotation_attested
        else "BLOCKED_KEY_ROTATION_REQUIRED"
        if core_ready
        else "BLOCKED_ENVIRONMENT"
    )
    output = {
        "schema_version": "phase-d3.environment-gate.v1",
        "status": status,
        "environment": {
            "OPENAI_BASE_URL": "SET" if base_url else "NOT_SET",
            "OPENAI_API_KEY": "SET" if key_present else "NOT_SET",
            "LLM4HLS_MODEL": "SET" if model else "NOT_SET",
            "LLM4HLS_VITIS_HLS_ROOT": (
                "SET" if vitis_root is not None else "NOT_SET"
            ),
            "vitis-run": (
                "AVAILABLE"
                if vitis_run is not None and vitis_run.is_file()
                else "UNAVAILABLE"
            ),
            "Vitis version": "2025.2"
            if "2025.2" in version_summary
            else "OTHER",
        },
        "checks": checks,
        "key_policy": {
            "previously_disclosed_key_must_not_be_used": True,
            "rotation_attestation_variable": (
                "LLM4HLS_KEY_ROTATED_AFTER_DISCLOSURE"
            ),
            "required_attestation_value": "1",
        },
        "model_calls_started": False,
        "secret_value_recorded": False,
    }
    path = artifact_dir / "environment-preflight.json"
    path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": status,
                "environment": output["environment"],
                "key_rotation_attested_after_disclosure": rotation_attested,
                "model_calls_started": False,
                "secret_value_recorded": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if status == "PASS" else 4


if __name__ == "__main__":
    raise SystemExit(main())
