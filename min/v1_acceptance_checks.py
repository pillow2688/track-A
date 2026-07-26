#!/usr/bin/env python3
"""Run Phase V1 static/test checks and persist machine-readable evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


MIN_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MIN_DIR.parent
HARNESS_ROOT = PROJECT_ROOT / "llm4hls_harness"
DEFAULT_ROOT = MIN_DIR / "benchmarks" / "public_3x10V1"


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _write_json(path: Path, value: object) -> None:
    _write(
        path,
        (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8"),
    )


def _run(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    log_root: Path,
) -> dict[str, object]:
    started = time.monotonic()
    process = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    log_path = log_root / f"{name}.log"
    _write(
        log_path,
        (
            "$ "
            + " ".join(command)
            + "\n\n[stdout]\n"
            + process.stdout
            + "\n[stderr]\n"
            + process.stderr
        ).encode("utf-8"),
    )
    return {
        "name": name,
        "command": command,
        "cwd": str(cwd),
        "exit_code": process.returncode,
        "wall_seconds": time.monotonic() - started,
        "log_ref": str(log_path.relative_to(log_root.parent)),
        "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
        "pass": process.returncode == 0,
    }


def _secret_scan(root: Path) -> dict[str, object]:
    configured_secret = os.environ.get("OPENAI_API_KEY", "").encode("utf-8")
    findings: list[dict[str, object]] = []
    scanned_files = 0
    scanned_bytes = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith("secret_scan_"):
            continue
        scanned_files += 1
        data = path.read_bytes()
        scanned_bytes += len(data)
        reasons: list[str] = []
        if configured_secret and configured_secret in data:
            reasons.append("CONFIGURED_API_KEY_EXACT_MATCH")
        folded = data.lower()
        if b"authorization: bearer " in folded:
            reasons.append("AUTHORIZATION_BEARER_HEADER")
        if reasons:
            findings.append(
                {
                    "path": str(path.relative_to(root)),
                    "reasons": reasons,
                }
            )
    return {
        "schema_version": "track-a.v1-secret-scan.v1",
        "root": str(root),
        "configured_secret_available_for_exact_scan": bool(configured_secret),
        "scanned_files": scanned_files,
        "scanned_bytes": scanned_bytes,
        "findings": findings,
        "pass": not findings,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--acceptance-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--full-test-python", type=Path, required=True)
    parser.add_argument(
        "--stage",
        choices=("preflight", "preflight_recheck", "final"),
        required=True,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.acceptance_root.resolve()
    logs = root / "checks" / args.stage
    logs.mkdir(parents=True, exist_ok=True)
    base_env = os.environ.copy()
    test_env = base_env.copy()
    for variable in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
        test_env.pop(variable, None)
    min_env = test_env | {
        "PYTHONPATH": os.pathsep.join(
            [str(MIN_DIR), str(HARNESS_ROOT)]
        )
    }
    harness_env = test_env | {
        "PYTHONPATH": os.pathsep.join(
            [str(PROJECT_ROOT), str(HARNESS_ROOT)]
        )
    }
    checks = [
        _run(
            name="min_tests",
            command=[
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "min",
                "-p",
                "test_*.py",
                "-v",
            ],
            cwd=PROJECT_ROOT,
            env=min_env,
            log_root=logs,
        ),
        _run(
            name="full_harness_tests",
            command=[
                str(args.full_test_python),
                "-m",
                "unittest",
                "discover",
                "-s",
                ".",
                "-t",
                ".",
                "-p",
                "test_*.py",
            ],
            cwd=HARNESS_ROOT,
            env=harness_env,
            log_root=logs,
        ),
        _run(
            name="compileall",
            command=[
                sys.executable,
                "-m",
                "compileall",
                "-q",
                "min",
                "llm4hls_harness/llm4hls_agent",
            ],
            cwd=PROJECT_ROOT,
            env=base_env,
            log_root=logs,
        ),
        _run(
            name="git_diff_check",
            command=[
                "git",
                "diff",
                "--check",
                "--",
                "min",
                "llm4hls_harness/llm4hls_agent/budget.py",
                "llm4hls_harness/llm4hls_agent/repair.py",
                "llm4hls_harness/llm4hls_agent/top_interface_guard.py",
            ],
            cwd=PROJECT_ROOT,
            env=base_env,
            log_root=logs,
        ),
    ]
    implementation_paths = (
        MIN_DIR / "minimal_flow.py",
        MIN_DIR / "v1_planner_io.py",
        MIN_DIR / "v1_patch_pipeline.py",
    )
    implementation = "\n".join(
        path.read_text(encoding="utf-8") for path in implementation_paths
    )
    hardcoded = [
        identifier
        for identifier in (
            "projection_bugfix",
            "dotProduct_optimize",
            "residual_stream_deadlock",
        )
        if identifier in implementation
    ]
    scope_check = {
        "name": "implementation_scope",
        "task_ids_hardcoded": hardcoded,
        "forbidden_hidden_reference_golden_tokens": [
            token
            for token in ("/hidden/", "/reference/", "/golden/")
            if token in implementation
        ],
        "pass": not hardcoded
        and not any(
            token in implementation
            for token in ("/hidden/", "/reference/", "/golden/")
        ),
    }
    checks.append(scope_check)
    secret_scan = _secret_scan(root)
    secret_scan_name = f"secret_scan_{args.stage}.json"
    _write_json(root / secret_scan_name, secret_scan)
    checks.append(
        {
            "name": "secret_scan",
            "pass": secret_scan["pass"],
            "result_ref": secret_scan_name,
        }
    )
    result = {
        "schema_version": "track-a.v1-acceptance-checks.v1",
        "stage": args.stage,
        "status": "PASS" if all(check["pass"] for check in checks) else "FAIL",
        "checks": checks,
    }
    _write_json(root / f"{args.stage}_checks.json", result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
