"""Machine-JSON operations for the V3-E experience knowledge base."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping

from .v3_experience import canonical_json
from .v3_experience_analysis import (
    build_coverage_matrix,
    build_data_quality,
    load_public_corpus,
    plan_gap_driven_collection,
    write_analysis_artifacts,
)
from .v3_experience_evaluation import evaluate_generalization
from .v3_experience_importer import (
    ImportPolicy,
    import_historical_runs,
    write_import_artifacts,
)
from .v3_experience_kb import (
    ExperienceKnowledgeBase,
    ExplainableSimilarCaseRetriever,
    KBSnapshot,
    validate_kb_query,
)
from .v3_experience_kb_quality import BayesianAtomRanker
from .v3_experience_ml import build_learning_readiness, build_ml_datasets, write_ml_artifacts
from .v3_experience_store import JsonlExperienceRepository


_FORBIDDEN_SOURCE_PARTS = {"hidden", "hidden_like", "golden", "reference", "answer"}


def _json(path: str | Path) -> dict[str, object]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must contain one JSON object")
    return dict(value)


def _jsonl(path: str | Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line_number, raw in enumerate(Path(path).read_bytes().splitlines(), start=1):
        value = json.loads(raw)
        if not isinstance(value, Mapping):
            raise ValueError(f"JSONL line {line_number} is not an object")
        records.append(dict(value))
    return records


def _snapshot(path: str | None) -> KBSnapshot | None:
    if path is None:
        return None
    value = _json(path)
    required = {
        "schema_version",
        "byte_offset",
        "prefix_sha256",
        "record_count",
        "index_sha256",
    }
    if set(value) != required or value["schema_version"] != "v3e.experience-kb-snapshot.v1":
        raise ValueError("snapshot manifest is invalid")
    return KBSnapshot(
        byte_offset=int(value["byte_offset"]),
        prefix_sha256=str(value["prefix_sha256"]),
        record_count=int(value["record_count"]),
        index_sha256=str(value["index_sha256"]),
    )


def _safe_public_source(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    if any(part.casefold() in _FORBIDDEN_SOURCE_PARTS for part in resolved.parts):
        raise ValueError("historical import is restricted to public train/dev sources")
    return resolved


def _emit(value: Mapping[str, object]) -> None:
    sys.stdout.write(canonical_json(value).decode("utf-8") + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm4hls-experience")
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("import", help="import public real run artifacts into v1")
    command.add_argument("--source", action="append", required=True)
    command.add_argument("--store", required=True)
    command.add_argument("--output-dir", required=True)
    command.add_argument("--task-split", choices=("train", "dev"), default="train")

    command = commands.add_parser("migrate", help="derive v2 reports without rewriting v1")
    command.add_argument("--v1-store", required=True)
    command.add_argument("--output-root", required=True)
    command.add_argument("--public-run-root", action="append", default=[])
    command.add_argument("--corpus-manifest")
    command.add_argument("--available-credit", type=int, default=2400)
    command.add_argument("--provider-unavailable", action="store_true")
    command.add_argument("--vitis-unavailable", action="store_true")

    command = commands.add_parser("validate", help="fail-closed validate an external v2 JSONL")
    command.add_argument("--kb-root", required=True)
    command.add_argument("--input", required=True)

    command = commands.add_parser("snapshot", help="freeze and hash a v2 store prefix")
    command.add_argument("--kb-root", required=True)

    for name in ("stats", "coverage"):
        command = commands.add_parser(name)
        command.add_argument("--kb-root", required=True)
        command.add_argument("--snapshot")

    for name in ("retrieve", "rank"):
        command = commands.add_parser(name)
        command.add_argument("--kb-root", required=True)
        command.add_argument("--snapshot")
        command.add_argument("--query-json", required=True)

    command = commands.add_parser("evaluate")
    command.add_argument("--records", required=True)
    command.add_argument("--audit-groups", required=True)

    command = commands.add_parser("plan-collection")
    command.add_argument("--coverage-json", required=True)
    command.add_argument("--corpus-manifest", required=True)
    command.add_argument("--available-credit", type=int, default=2400)
    command.add_argument("--provider-unavailable", action="store_true")
    command.add_argument("--vitis-unavailable", action="store_true")

    command = commands.add_parser("export-ml")
    command.add_argument("--records", required=True)
    command.add_argument("--output-root", required=True)
    command.add_argument("--generalization")

    command = commands.add_parser("readiness")
    command.add_argument("--records", required=True)
    command.add_argument("--generalization")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "import":
            sources = [_safe_public_source(item) for item in args.source]
            repository = JsonlExperienceRepository(args.store)
            result = import_historical_runs(
                sources,
                repository,
                policy=ImportPolicy(task_split=args.task_split),
            )
            artifacts = write_import_artifacts(result, args.output_dir)
            _emit(
                {
                    "status": "OK",
                    "records": len(result.records),
                    "inserted": len(result.inserted_record_ids),
                    "duplicates": len(result.duplicate_record_ids),
                    "artifacts": {key: path.name for key, path in artifacts.items()},
                }
            )
            return 0
        if args.command == "migrate":
            result = write_analysis_artifacts(
                v1_store=args.v1_store,
                output_root=args.output_root,
                public_run_roots=[_safe_public_source(item) for item in args.public_run_root],
                corpus_manifest=args.corpus_manifest,
                provider_available=not args.provider_unavailable,
                vitis_available=not args.vitis_unavailable,
                available_credit=args.available_credit,
            )
            _emit(
                {
                    "status": "OK",
                    "stats": result["stats"],
                    "artifact_manifest": result["artifact_manifest"],
                }
            )
            return 0
        if args.command == "validate":
            _emit(ExperienceKnowledgeBase(args.kb_root).validate_external_jsonl(args.input))
            return 0
        if args.command == "snapshot":
            _emit(ExperienceKnowledgeBase(args.kb_root).snapshot(persist=True).to_dict())
            return 0
        if args.command in {"stats", "coverage", "retrieve", "rank"}:
            kb = ExperienceKnowledgeBase(args.kb_root)
            snapshot = _snapshot(getattr(args, "snapshot", None))
            records = list(kb.records(snapshot))
            if args.command == "stats":
                _emit(build_data_quality(records))
            elif args.command == "coverage":
                _emit(build_coverage_matrix(records))
            else:
                query = validate_kb_query(_json(args.query_json))
                retrieval = ExplainableSimilarCaseRetriever().retrieve(query, records)
                if args.command == "retrieve":
                    _emit(retrieval.to_dict())
                else:
                    _emit(
                        BayesianAtomRanker().rank(
                            query, [case.record for case in retrieval.considered]
                        )
                    )
            return 0
        if args.command == "evaluate":
            audit = _json(args.audit_groups)
            if not isinstance(audit.get("groups"), Mapping):
                raise ValueError("audit groups file is invalid")
            _emit(evaluate_generalization(_jsonl(args.records), audit_groups=audit["groups"]))
            return 0
        if args.command == "plan-collection":
            _emit(
                plan_gap_driven_collection(
                    _json(args.coverage_json),
                    load_public_corpus(args.corpus_manifest),
                    available_credit=args.available_credit,
                    provider_available=not args.provider_unavailable,
                    vitis_available=not args.vitis_unavailable,
                )
            )
            return 0
        if args.command == "export-ml":
            result = write_ml_artifacts(
                records_path=args.records,
                output_root=args.output_root,
                generalization_path=args.generalization,
            )
            _emit(
                {
                    "status": "OK",
                    "source_record_count": result["export"]["source_record_count"],
                    "split_counts": result["export"]["split_counts"],
                    "readiness": result["readiness"],
                }
            )
            return 0
        if args.command == "readiness":
            records = _jsonl(args.records)
            export = build_ml_datasets(records)
            generalization = _json(args.generalization) if args.generalization else None
            _emit(build_learning_readiness(records, export, generalization=generalization))
            return 0
    except (OSError, UnicodeError, ValueError) as exc:
        _emit({"status": "ERROR", "error_type": type(exc).__name__, "detail": str(exc)})
        return 2
    raise AssertionError("unhandled experience command")


def main_entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    main_entry()
