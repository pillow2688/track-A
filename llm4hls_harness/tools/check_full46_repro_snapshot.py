#!/usr/bin/env python3
"""Validate the public-input FULL46 reproducibility snapshot without tools."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


FORBIDDEN = {"answer", "golden", "hidden", "hidden_like", "reference"}


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--harness", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    repo, harness, snapshot = args.repo.resolve(), args.harness.resolve(), args.snapshot.resolve()
    manifest_path = snapshot / "snapshot_manifest.json"
    failures: list[str] = []
    if not manifest_path.is_file():
        raise SystemExit("FAIL missing snapshot_manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tasks = manifest.get("tasks", [])
    if len(tasks) != 46 or len({entry.get("task_id") for entry in tasks}) != 46:
        failures.append("task_count_or_identity")
    if manifest.get("mode_counts") != {"OPTIMIZE": 11, "REPAIR": 14, "STRUCTURAL_FIX": 11, "SYNTH_FIX": 10}:
        failures.append("mode_distribution")
    if set(manifest.get("excluded_path_components", [])) != FORBIDDEN:
        failures.append("exclusion_policy")
    for entry in manifest.get("public_task_files", []):
        source = harness / entry["path"]
        if any(part in FORBIDDEN for part in Path(entry["path"]).parts):
            failures.append(f"forbidden_snapshot_entry:{entry['path']}")
        elif not source.is_file() or digest(source) != entry["sha256"]:
            failures.append(f"public_task_hash:{entry['path']}")
    for key, entry in (manifest.get("copied_inputs") or {}).items():
        snapshot_file = Path(entry["snapshot"])
        if not snapshot_file.is_file() or digest(snapshot_file) != entry["sha256"]:
            failures.append(f"copied_input_hash:{key}")
    config_path = snapshot / "experiment_config_snapshot.json"
    if not config_path.is_file() or digest(config_path) != manifest.get("experiment_config_snapshot_sha256"):
        failures.append("experiment_config_snapshot")
    for entry in manifest.get("frozen_report_hashes", []):
        source = repo / entry["path"]
        if not source.is_file() or digest(source) != entry["sha256"]:
            failures.append(f"report_hash:{entry['path']}")
    status = "PASS" if not failures else "FAIL"
    print(json.dumps({"status": status, "tasks": len(tasks), "public_task_files": len(manifest.get("public_task_files", [])), "failures": failures}, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
