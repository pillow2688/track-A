from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .qa import scan_tree, stage_submission
from .reporting import create_benchmark_snapshot, create_oracle_snapshot, generate_submission_docs


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate and QA V3-D submission artifacts.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate Markdown reports from curated run JSON")
    generate.add_argument("--repo-root", type=Path, required=True)
    generate.add_argument("--manifest", type=Path, required=True)
    generate.add_argument("--output-dir", type=Path, required=True)

    snapshot = subparsers.add_parser("snapshot-benchmark", help="create a path-free benchmark summary snapshot")
    snapshot.add_argument("--input", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)

    oracle_snapshot = subparsers.add_parser("snapshot-oracle", help="create a path-free Oracle summary snapshot")
    oracle_snapshot.add_argument("--input", type=Path, required=True)
    oracle_snapshot.add_argument("--output", type=Path, required=True)

    scan = subparsers.add_parser("scan", help="scan a staging tree for secrets and forbidden artifacts")
    scan.add_argument("--root", type=Path, required=True)
    scan.add_argument("--max-file-bytes", type=int, default=5 * 1024 * 1024)
    scan.add_argument("--json-output", type=Path)

    stage = subparsers.add_parser("stage", help="create a reviewable NOT FINAL staging tree")
    stage.add_argument("--source-root", type=Path, required=True)
    stage.add_argument("--output-root", type=Path, required=True)
    stage.add_argument("--spec", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "generate":
        paths = generate_submission_docs(
            repo_root=args.repo_root,
            manifest_path=args.manifest,
            output_dir=args.output_dir,
        )
        print(json.dumps({"status": "PASS", "generated": [str(path) for path in paths]}, indent=2))
        return 0
    if args.command == "scan":
        findings = scan_tree(args.root, max_file_bytes=args.max_file_bytes)
        payload = {"status": "PASS" if not findings else "FAIL", "findings": [asdict(item) for item in findings]}
        rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if args.json_output:
            args.json_output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0 if not findings else 2
    if args.command == "snapshot-benchmark":
        path = create_benchmark_snapshot(source_path=args.input, output_path=args.output)
        print(json.dumps({"status": "PASS", "snapshot": str(path)}, indent=2))
        return 0
    if args.command == "snapshot-oracle":
        path = create_oracle_snapshot(source_path=args.input, output_path=args.output)
        print(json.dumps({"status": "PASS", "snapshot": str(path)}, indent=2))
        return 0
    manifest = stage_submission(source_root=args.source_root, output_root=args.output_root, spec_path=args.spec)
    print(
        json.dumps(
            {
                "status": "PASS",
                "staging": str(args.output_root),
                "manifest": str(args.output_root / "staging_manifest.json"),
                "file_count": manifest["file_count"],
                "skipped_file_count": manifest["skipped_file_count"],
                "source_tree_state": manifest["source_tree_state"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
