"""Standalone entry point for frozen-Candidate Track-A certification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping

from .final_certification import (
    CertificationConfig,
    certify_v3_search_result,
)
from .task import load_public_task
from .tools import ToolConfig


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm4hls-certify",
        description=(
            "Certify a frozen V3 search Candidate with CSim, Synth, CoSim and "
            "the 100 MHz gate outside the Agent Credit ledger."
        ),
    )
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--search-run-dir", required=True)
    parser.add_argument("--maximum-clock-period-ns", type=float, default=10.0)
    return parser


def _tool_config(run_root: Path) -> ToolConfig:
    value = json.loads(
        (run_root / "v3_run_config.json").read_text(encoding="utf-8")
    )
    if not isinstance(value, Mapping) or not isinstance(value.get("tool"), Mapping):
        raise ValueError("v3_run_config.json lacks tool configuration")
    tool = value["tool"]
    timeouts = tool.get("timeouts")
    if not isinstance(timeouts, Mapping):
        raise ValueError("v3_run_config.json lacks tool timeouts")
    return ToolConfig(
        vitis_root=str(tool["vitis_root"]),
        part=str(tool["part"]),
        clock_ns=float(tool["clock_ns"]),
        timeouts={
            "csim": float(timeouts["csim"]),
            "synth": float(timeouts["synth"]),
            "cosim": float(timeouts["cosim"]),
        },
        flow_target=str(tool.get("flow_target", "vivado")),
        toolchain_id=str(tool.get("toolchain_id", "Vitis 2025.2")),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = Path(args.search_run_dir).resolve()
        task = load_public_task(args.task_dir)
        result = certify_v3_search_result(
            task,
            root,
            CertificationConfig(
                tool=_tool_config(root),
                maximum_clock_period_ns=args.maximum_clock_period_ns,
            ),
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3
    summary = {
        "status": result.get("status"),
        "agent_search_status": result.get("agent_search_status"),
        "task_id": result.get("task_id"),
        "final_certification": result.get("final_certification"),
        "result_ref": "v3_certified_result.json",
        "search_run_dir": str(root),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "DONE" else 2


def main_entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    main_entry()
