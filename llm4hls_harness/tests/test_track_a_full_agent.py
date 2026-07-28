from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from llm4hls_agent.full_agent_manifest import (
    FULL_AGENT_MANIFEST_SCHEMA,
    FULL_AGENT_PENDING,
    FULL_AGENT_PROFILE,
    FULL_AGENT_READY,
    FullAgentManifestError,
    load_full_agent_manifest,
)
from llm4hls_agent.v3_batch_benchmark import (
    BatchBenchmarkRunner,
    BenchmarkConfig,
    V3PrototypeCLIExecutor,
)
from llm4hls_agent.v3_continuation_admission import (
    CONTINUATION_ADMISSION_SCHEMA,
    CONTINUATION_POLICY_VERSION,
)
from llm4hls_agent.v3_continuation_v2 import (
    CONTINUATION_DECISION_SCHEMA_V2,
)
from llm4hls_agent.v3_strategy_ranker_v3 import (
    STRATEGY_RANKER_V3_SCHEMA,
)
from llm4hls_agent.v3_experience_v2_runtime import RANKER_ADMISSION_SCHEMA


_ROLES = (
    "a2_gate",
    "a2_admission",
    "a3_store",
    "a3_gate",
    "a3_admission",
)


def _manifest_payload(
    *,
    status: str,
    artifacts: dict[str, dict[str, str | None]],
) -> dict[str, object]:
    return {
        "schema_version": FULL_AGENT_MANIFEST_SCHEMA,
        "profile": FULL_AGENT_PROFILE,
        "artifact_kind": "runtime_full_agent_manifest",
        "status": status,
        "runtime_loader": True,
        "runtime_authority": (
            "EXPLICIT_MANIFEST_PLUS_MATCHING_RUNTIME_ARGUMENTS"
        ),
        "artifact_root": "artifacts",
        "token_policy": {
            "mode": "fixed",
            "dynamic_allowed": False,
        },
        "components": {
            "A1 Structured Evidence Memory": {"mode": "on"},
            "A2 Budget-Aware Continuation": {
                "mode": "enforce",
                "external_version": "v2",
                "implementation_version": CONTINUATION_POLICY_VERSION,
                "decision_schema": CONTINUATION_DECISION_SCHEMA_V2,
            },
            "A3 Experience Strategy Advisor": {
                "mode": "guided",
                "ranker_version": "v3",
                "implementation_schema": STRATEGY_RANKER_V3_SCHEMA,
            },
        },
        "foundations": {
            "B1 Candidate and Safety Baseline": {
                "enabled": True,
                "ablatable": False,
            },
            "B2 Independent Final Certification": {
                "enabled": True,
                "ablatable": False,
                "budget_domain": (
                    "FINAL_CERTIFICATION_OUTSIDE_AGENT_BUDGET"
                ),
                "feedback_policy": "NO_SAME_RUN_AGENT_FEEDBACK",
            },
        },
        "artifacts": artifacts,
    }


def _write_ready_manifest(root: Path) -> tuple[Path, dict[str, Path]]:
    artifact_root = root / "artifacts"
    artifact_root.mkdir(parents=True)
    paths: dict[str, Path] = {}
    bindings: dict[str, dict[str, str | None]] = {}
    for role in _ROLES:
        artifact = artifact_root / f"{role}.json"
        artifact.write_text(
            json.dumps({"role": role}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        paths[role] = artifact
        bindings[role] = {
            "path": artifact.name,
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }
    a2_admission = paths["a2_admission"]
    a2_admission.write_text(
        json.dumps(
            {
                "schema_version": CONTINUATION_ADMISSION_SCHEMA,
                "policy_version": CONTINUATION_POLICY_VERSION,
                "decision_schema": CONTINUATION_DECISION_SCHEMA_V2,
                "gate_json_path": paths["a2_gate"].name,
                "gate_json_sha256": bindings["a2_gate"]["sha256"],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    bindings["a2_admission"]["sha256"] = hashlib.sha256(
        a2_admission.read_bytes()
    ).hexdigest()
    a3_admission = paths["a3_admission"]
    a3_admission.write_text(
        json.dumps(
            {
                "schema_version": RANKER_ADMISSION_SCHEMA,
                "decision": "PASS",
                "ranker_schema": STRATEGY_RANKER_V3_SCHEMA,
                "store_path": paths["a3_store"].name,
                "store_sha256": bindings["a3_store"]["sha256"],
                "gate_path": paths["a3_gate"].name,
                "gate_sha256": bindings["a3_gate"]["sha256"],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    bindings["a3_admission"]["sha256"] = hashlib.sha256(
        a3_admission.read_bytes()
    ).hexdigest()
    manifest = root / "track_a_full_agent_v1.json"
    manifest.write_text(
        json.dumps(
            _manifest_payload(status=FULL_AGENT_READY, artifacts=bindings),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest, paths


class TrackAFullAgentManifestTests(unittest.TestCase):
    def test_ready_manifest_binds_every_required_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path, paths = _write_ready_manifest(Path(directory))

            manifest = load_full_agent_manifest(manifest_path)

            self.assertEqual(manifest.status, FULL_AGENT_READY)
            self.assertEqual(set(manifest.artifacts), set(_ROLES))
            self.assertEqual(
                manifest.artifact("a3_store").path,
                paths["a3_store"].resolve(),
            )
            manifest.verify_unchanged()

    def test_bound_artifact_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path, paths = _write_ready_manifest(Path(directory))
            manifest = load_full_agent_manifest(manifest_path)
            paths["a2_gate"].write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(
                FullAgentManifestError, "sha256 mismatch"
            ):
                manifest.verify_unchanged()

    def test_pending_manifest_is_fillable_but_not_runtime_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts").mkdir()
            manifest_path = root / "track_a_full_agent_v1.json"
            manifest_path.write_text(
                json.dumps(
                    _manifest_payload(
                        status=FULL_AGENT_PENDING,
                        artifacts={
                            role: {"path": None, "sha256": None}
                            for role in _ROLES
                        },
                    ),
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            pending = load_full_agent_manifest(
                manifest_path,
                require_ready=False,
            )
            self.assertEqual(pending.status, FULL_AGENT_PENDING)
            self.assertEqual(dict(pending.artifacts), {})
            with self.assertRaisesRegex(
                FullAgentManifestError, "is not READY"
            ):
                load_full_agent_manifest(manifest_path)

    def test_manifest_contract_and_symlink_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path, _ = _write_ready_manifest(root)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["token_policy"]["mode"] = "dynamic"
            manifest_path.write_text(
                json.dumps(payload, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                FullAgentManifestError, "token_policy"
            ):
                load_full_agent_manifest(manifest_path)

            manifest_path, _ = _write_ready_manifest(root / "second")
            symlink = root / "manifest-link.json"
            symlink.symlink_to(manifest_path)
            with self.assertRaisesRegex(
                FullAgentManifestError, "non-symlink"
            ):
                load_full_agent_manifest(symlink)

    def test_shipped_manifest_is_ready_and_requires_matching_batch_args(
        self,
    ) -> None:
        package_root = Path(__file__).resolve().parents[1] / "llm4hls_agent"
        manifest_path = package_root / "config" / "track_a_full_agent_v1.json"
        safe_baseline_path = (
            package_root / "config" / "track_a_safe_baseline_v1.json"
        )
        self.assertTrue(safe_baseline_path.is_file())
        manifest = load_full_agent_manifest(manifest_path)
        self.assertEqual(manifest.status, FULL_AGENT_READY)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus"
            corpus.mkdir()
            with self.assertRaisesRegex(
                ValueError, "runtime arguments mismatch"
            ):
                BenchmarkConfig(
                    corpus=corpus,
                    output_dir=root / "mismatch",
                    full_agent_manifest=manifest_path,
                )

            config = BenchmarkConfig(
                corpus=corpus,
                output_dir=root / "ready",
                backend="vitis",
                evidence_memory_mode="on",
                continuation_policy_mode="enforce",
                continuation_policy_version="v2",
                continuation_admission_manifest=manifest.artifact(
                    "a2_admission"
                ).path,
                full_agent_manifest=manifest_path,
                experience_mode="guided",
                experience_store=manifest.artifact("a3_store").path,
                experience_ranker_version="v3",
                experience_admission_manifest=manifest.artifact(
                    "a3_admission"
                ).path,
            )
            runner = BatchBenchmarkRunner(config)

        self.assertIs(type(runner.executor), V3PrototypeCLIExecutor)
        self.assertFalse(runner.executor.experimental_token_policy)
        self.assertEqual(
            config.full_agent_artifact_sha256s,
            {
                role: manifest.artifact(role).sha256
                for role in _ROLES
            },
        )
        self.assertEqual(
            config.public_dict()["full_agent_manifest_snapshot"]["sha256"],
            manifest.sha256,
        )


if __name__ == "__main__":
    unittest.main()
