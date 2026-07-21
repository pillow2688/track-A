"""Standalone CLI for the sanitized V3-E historical experience importer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from llm4hls_agent.v3_experience import TASK_SPLITS
from llm4hls_agent.v3_experience_importer import (
    ImportPolicy,
    build_import_report,
    import_historical_runs,
    write_import_artifacts,
)
from llm4hls_agent.v3_experience_store import JsonlExperienceRepository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="v3-experience-import",
        description=(
            "Import sanitized Candidate-level history from V3 terminal runs. "
            "No source, Patch, Prompt, log, secret, golden or hidden-like content "
            "is copied into the experience store."
        ),
    )
    parser.add_argument(
        "--runs-root",
        action="append",
        required=True,
        help="Run directory or result marker to scan; repeat for multiple roots.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for experience_store.jsonl and import reports.",
    )
    parser.add_argument(
        "--task-split",
        choices=sorted(TASK_SPLITS),
        default="unspecified",
        help=(
            "Explicit default split. The safe default is unspecified; use train "
            "only for history authorized for hidden-like retrieval."
        ),
    )
    parser.add_argument(
        "--algorithm-family",
        default="unspecified",
        help="Explicit default family; historical task names are never used to infer it.",
    )
    parser.add_argument(
        "--split-override",
        action="append",
        default=[],
        metavar="TASK_ID=SPLIT",
        help="Explicit per-task split metadata; repeat as needed.",
    )
    parser.add_argument(
        "--family-override",
        action="append",
        default=[],
        metavar="TASK_ID=FAMILY",
        help="Explicit per-task algorithm-family metadata; repeat as needed.",
    )
    parser.add_argument(
        "--include-fixtures",
        action="store_true",
        help=(
            "Retain sanitized scripted/demo V3 records as non-ranking fixtures. "
            "Oracle/golden and deterministic executor results remain excluded."
        ),
    )
    return parser


def _overrides(values: list[str], *, split: bool) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values:
        task_id, separator, value = item.partition("=")
        task_id = task_id.strip()
        value = value.strip()
        if not separator or not task_id or not value:
            raise ValueError("override must use TASK_ID=VALUE")
        if split and value not in TASK_SPLITS:
            raise ValueError(f"invalid task split: {value}")
        result[task_id] = value
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        policy = ImportPolicy(
            task_split=args.task_split,
            algorithm_family=args.algorithm_family,
            split_overrides=_overrides(args.split_override, split=True),
            family_overrides=_overrides(args.family_override, split=False),
            include_fixtures=args.include_fixtures,
        )
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        store_path = output_dir / "experience_store.jsonl"
        store_path.touch(exist_ok=True)
        repository = JsonlExperienceRepository(store_path)
        result = import_historical_runs(
            [Path(root) for root in args.runs_root],
            repository,
            policy=policy,
        )
        write_import_artifacts(result, output_dir)
    except (OSError, ValueError, RuntimeError) as exc:
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
        return 2

    report = build_import_report(result)
    print(
        json.dumps(
            {
                "status": "OK",
                "store": "experience_store.jsonl",
                "import_report": "experience_import_report.json",
                "stats": "experience_stats.json",
                "data_quality_report": "experience_data_quality_report.md",
                "imported_run_count": report["imported_run_count"],
                "record_count": report["record_count"],
                "inserted_count": report["inserted_count"],
                "duplicate_count": report["duplicate_count"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main_entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    main_entry()
