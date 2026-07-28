from __future__ import annotations

import fcntl
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm4hls_agent import final_certification as final_certification_module
from llm4hls_agent.final_certification import (
    CERTIFICATION_BUDGET_DOMAIN,
    CERTIFICATION_IMPLEMENTATION_VERSION,
    CertificationConfig,
    FinalCertificationError,
    certify_frozen_candidate,
    certify_v3_search_result,
    freeze_search_candidate,
)
from llm4hls_agent.task import PublicTask, load_public_task
from llm4hls_agent.tools import BackendResult, ToolConfig


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _write_task(
    root: Path,
    *,
    task_id: str = "certification_fixture",
) -> None:
    (root / "task.toml").write_text(
        "\n".join(
            [
                f'task_id = "{task_id}"',
                'task_type = "optimize"',
                'top = "kernel"',
                'kernel_file = "kernel.cpp"',
                'header_files = ["kernel.h"]',
                'public_tb = "kernel_tb.cpp"',
                "max_credits = 1",
                "max_tokens = 1",
                "[target]",
                'part = "xcu55c-fsvh2892-2L-e"',
                "clock_ns = 5.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "kernel.cpp").write_text(
        '#include "kernel.h"\nvoid kernel() {}\n', encoding="utf-8"
    )
    (root / "kernel.h").write_text("void kernel();\n", encoding="utf-8")
    (root / "kernel_tb.cpp").write_text(
        "int main() { return 0; }\n", encoding="utf-8"
    )


def _write_search_run(
    root: Path,
    *,
    task: PublicTask,
    tool: ToolConfig,
) -> None:
    task_code = task.kernel_bytes
    source_ref = "candidates/candidate_001/kernel.cpp"
    source = root / source_ref
    source.parent.mkdir(parents=True)
    source.write_bytes(task_code)
    source_sha256 = __import__("hashlib").sha256(task_code).hexdigest()
    binding = {
        "schema_version": "v3.terminal-candidate-binding.v1",
        "candidate_id": "candidate_001",
        "parent_id": "candidate_000",
        "source_ref": source_ref,
        "source_sha256": source_sha256,
        "binding_source": "FINAL_VERIFIED_CANDIDATE",
        "required_validation_actions": ["csim", "synth", "cosim"],
        "validation_status": {
            "csim": "PASS",
            "synth": "PASS",
            "cosim": "PASS",
        },
        "promotion_status": "FINAL_VERIFIED",
        "selection_reason": "FINAL_CANDIDATE_COMMITTED",
        "candidate_decision_ref": "control/candidate_operations/fixture.json",
        "registry_revision": 2,
        "binding_sha256": "fixture-binding",
    }
    result = {
        "workflow": "V3_A1_VERTICAL_PROTOTYPE",
        "status": "DONE",
        "stop_reason": "FINAL_VALIDATED",
        "task_id": task.id,
        "terminal_candidate_binding": binding,
        "budget": {
            "budget_domain": "agent_search",
            "credits_used": 1,
            "credit_limit": 1,
        },
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "v3_prototype_result.json").write_text(
        json.dumps(result, sort_keys=True) + "\n", encoding="utf-8"
    )
    (root / "v3_run_config.json").write_text(
        json.dumps({"tool": tool.to_dict()}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "v3_task_spec.json").write_text(
        json.dumps(
            {
                "task_id": task.id,
                "top": task.top,
                "kernel_file": task.kernel_name,
                "public_tb": task.public_tb_name,
                "public_file_hashes": dict(task.public_file_hashes),
                "part": task.part,
                "clock_ns": task.clock_ns,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "budget_ledger.jsonl").write_text(
        '{"budget_domain":"agent_search","state":"INITIALIZED"}\n',
        encoding="utf-8",
    )


class CertificationBackend:
    def __init__(self, *, clock_period_ns: float = 5.0) -> None:
        self.clock_period_ns = clock_period_ns
        self.calls: list[str] = []
        self.fingerprint_calls = 0

    def fingerprint(self) -> str:
        self.fingerprint_calls += 1
        return f"certification-backend:{self.clock_period_ns}"

    def run(
        self,
        kind: str,
        *,
        work_dir: Path,
        **_kwargs: object,
    ) -> BackendResult:
        self.calls.append(kind)
        work_dir.mkdir(parents=True)
        artifact = work_dir / f"{kind}.log"
        artifact.write_text("fixture\n", encoding="utf-8")
        report = (
            {"estimated_clock_period_ns": self.clock_period_ns}
            if kind == "synth"
            else None
        )
        cosim = {"status": "Pass"} if kind == "cosim" else None
        return BackendResult(
            ok=True,
            phase="pass",
            return_code=0,
            elapsed_s=0.01,
            evidence=[f"{kind} passed"],
            artifacts={"log": f"work/{artifact.name}"},
            report=report,
            cosim=cosim,
        )


class FinalCertificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        task_root = root / "task"
        task_root.mkdir()
        _write_task(task_root)
        self.task = load_public_task(task_root)
        self.run_root = root / "run"
        self.config = CertificationConfig(
            ToolConfig(
                vitis_root="/opt/xilinx/2025.2/Vitis",
                part=self.task.part,
                clock_ns=self.task.clock_ns,
                timeouts={"csim": 1.0, "synth": 1.0, "cosim": 1.0},
                toolchain_id="fixture-toolchain",
            )
        )
        _write_search_run(
            self.run_root,
            task=self.task,
            tool=self.config.tool,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_certification_runs_all_stages_without_charging_agent_ledger(self) -> None:
        backend = CertificationBackend()
        ledger_before = (self.run_root / "budget_ledger.jsonl").read_bytes()

        combined = certify_v3_search_result(
            self.task, self.run_root, self.config, backend=backend
        )

        self.assertEqual(backend.calls, ["csim", "synth", "cosim"])
        self.assertEqual(combined["status"], "DONE")
        certification = combined["final_certification"]
        self.assertEqual(certification["budget_domain"], CERTIFICATION_BUDGET_DOMAIN)
        self.assertEqual(certification["agent_credits_charged"], 0)
        receipt_path = (
            self.run_root
            / str(certification["receipt_ref"])
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(
            receipt["certification_implementation_version"],
            CERTIFICATION_IMPLEMENTATION_VERSION,
        )
        self.assertEqual(
            receipt["stages"]["csim"]["artifacts"]["log"],
            "work/csim.log",
        )
        freeze = json.loads(
            (self.run_root / "control" / "frozen_candidate.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            freeze["search_tool_config"],
            self.config.tool.to_dict(),
        )
        self.assertEqual(
            (self.run_root / "budget_ledger.jsonl").read_bytes(), ledger_before
        )

    def test_completed_receipt_is_idempotent(self) -> None:
        backend = CertificationBackend()
        first = certify_frozen_candidate(
            self.task, self.run_root, self.config, backend=backend
        )
        second = certify_frozen_candidate(
            self.task, self.run_root, self.config, backend=backend
        )

        self.assertEqual(first, second)
        self.assertEqual(backend.calls, ["csim", "synth", "cosim"])

    def test_completed_receipt_rejects_missing_artifact_without_rerun(self) -> None:
        first_backend = CertificationBackend()
        receipt = certify_frozen_candidate(
            self.task, self.run_root, self.config, backend=first_backend
        )
        certification_root = (
            self.run_root / "certification" / str(receipt["certification_id"])
        )
        artifact_ref = receipt["stages"]["csim"]["artifacts"]["log"]
        (
            certification_root / "actions" / "csim" / str(artifact_ref)
        ).unlink()
        second_backend = CertificationBackend()

        with self.assertRaisesRegex(FinalCertificationError, "artifact is missing"):
            certify_frozen_candidate(
                self.task, self.run_root, self.config, backend=second_backend
            )

        self.assertEqual(second_backend.calls, [])

    def test_completed_receipt_recomputes_outcome_from_action_results(self) -> None:
        first_backend = CertificationBackend(clock_period_ns=10.1)
        receipt = certify_frozen_candidate(
            self.task, self.run_root, self.config, backend=first_backend
        )
        receipt_path = (
            self.run_root
            / "certification"
            / str(receipt["certification_id"])
            / "receipt.json"
        )
        tampered = dict(receipt)
        tampered["status"] = "PASS"
        tampered["failure_reasons"] = []
        tampered["clock_gate"] = {
            **receipt["clock_gate"],
            "passed": True,
            "reason": "CLOCK_AT_OR_ABOVE_100_MHZ",
        }
        tampered["receipt_sha256"] = _sha256_json(
            {
                key: value
                for key, value in tampered.items()
                if key != "receipt_sha256"
            }
        )
        receipt_path.write_text(
            json.dumps(tampered, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        second_backend = CertificationBackend(clock_period_ns=10.1)

        with self.assertRaisesRegex(
            FinalCertificationError,
            "clock gate is inconsistent",
        ):
            certify_frozen_candidate(
                self.task,
                self.run_root,
                self.config,
                backend=second_backend,
            )

        self.assertEqual(second_backend.calls, [])

    def test_concurrent_certification_claim_fails_before_backend(self) -> None:
        lock_path = self.run_root / "control" / "final_certification.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        backend = CertificationBackend()

        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(
                FinalCertificationError,
                "already in progress",
            ):
                certify_frozen_candidate(
                    self.task,
                    self.run_root,
                    self.config,
                    backend=backend,
                )
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        self.assertEqual(backend.calls, [])
        self.assertEqual(backend.fingerprint_calls, 0)

    def test_combined_result_write_remains_inside_run_level_claim(self) -> None:
        primary_backend = CertificationBackend()
        contender_backend = CertificationBackend()
        contender_config = CertificationConfig(
            self.config.tool,
            maximum_clock_period_ns=9.0,
        )
        original_atomic_json = final_certification_module._atomic_json
        contender_attempted = False

        def atomic_json_with_contender(path: Path, value: object) -> bytes:
            nonlocal contender_attempted
            if path.name == "v3_certified_result.json" and not contender_attempted:
                contender_attempted = True
                with self.assertRaisesRegex(
                    FinalCertificationError,
                    "already in progress",
                ):
                    certify_v3_search_result(
                        self.task,
                        self.run_root,
                        contender_config,
                        backend=contender_backend,
                    )
            return original_atomic_json(path, value)

        with patch.object(
            final_certification_module,
            "_atomic_json",
            side_effect=atomic_json_with_contender,
        ):
            combined = certify_v3_search_result(
                self.task,
                self.run_root,
                self.config,
                backend=primary_backend,
            )

        self.assertTrue(contender_attempted)
        self.assertEqual(combined["status"], "DONE")
        self.assertEqual(primary_backend.calls, ["csim", "synth", "cosim"])
        self.assertEqual(contender_backend.calls, [])
        self.assertEqual(contender_backend.fingerprint_calls, 0)

    def test_cached_stage_rejects_tampered_identity_fields_without_rerun(
        self,
    ) -> None:
        first_backend = CertificationBackend()
        receipt = certify_frozen_candidate(
            self.task, self.run_root, self.config, backend=first_backend
        )
        certification_root = (
            self.run_root / "certification" / str(receipt["certification_id"])
        )
        (certification_root / "receipt.json").unlink()
        result_path = certification_root / "actions" / "csim" / "result.json"
        original = json.loads(result_path.read_text(encoding="utf-8"))

        for field in (
            "schema_version",
            "budget_domain",
            "certification_implementation_version",
            "task_fingerprint",
        ):
            with self.subTest(field=field):
                tampered = dict(original)
                tampered[field] = f"tampered-{field}"
                result_path.write_text(
                    json.dumps(tampered, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                second_backend = CertificationBackend()

                with self.assertRaisesRegex(
                    FinalCertificationError,
                    f"mismatched {field}",
                ):
                    certify_frozen_candidate(
                        self.task,
                        self.run_root,
                        self.config,
                        backend=second_backend,
                    )

                self.assertEqual(second_backend.calls, [])

        result_path.write_text(
            json.dumps(original, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_cached_stage_rejects_changed_artifact_without_rerun(self) -> None:
        first_backend = CertificationBackend()
        receipt = certify_frozen_candidate(
            self.task, self.run_root, self.config, backend=first_backend
        )
        certification_root = (
            self.run_root / "certification" / str(receipt["certification_id"])
        )
        (certification_root / "receipt.json").unlink()
        artifact_ref = receipt["stages"]["csim"]["artifacts"]["log"]
        (
            certification_root / "actions" / "csim" / str(artifact_ref)
        ).write_text(
            "tampered\n", encoding="utf-8"
        )
        second_backend = CertificationBackend()

        with self.assertRaisesRegex(FinalCertificationError, "content changed"):
            certify_frozen_candidate(
                self.task, self.run_root, self.config, backend=second_backend
            )

        self.assertEqual(second_backend.calls, [])

    def test_clock_above_ten_nanoseconds_fails_without_agent_feedback(self) -> None:
        backend = CertificationBackend(clock_period_ns=10.1)

        receipt = certify_frozen_candidate(
            self.task, self.run_root, self.config, backend=backend
        )

        self.assertEqual(receipt["status"], "FAIL")
        self.assertIn("CLOCK_BELOW_100_MHZ", receipt["failure_reasons"])
        self.assertEqual(
            receipt["next_action_on_failure"], "START_NEW_AGENT_SEARCH_RUN"
        )

    def test_source_tamper_is_rejected_before_backend_execution(self) -> None:
        freeze = freeze_search_candidate(self.task, self.run_root)
        source = self.run_root / str(freeze["source_ref"])
        source.write_text("void kernel() { /* tampered */ }\n", encoding="utf-8")
        backend = CertificationBackend()

        with self.assertRaisesRegex(FinalCertificationError, "hash is stale"):
            certify_frozen_candidate(
                self.task, self.run_root, self.config, backend=backend
            )

        self.assertEqual(backend.calls, [])

    def test_wrong_task_is_rejected_before_backend_and_preserves_ledger(self) -> None:
        wrong_task_root = Path(self.temporary.name) / "wrong_task"
        wrong_task_root.mkdir()
        _write_task(wrong_task_root, task_id="other_task")
        wrong_task = load_public_task(wrong_task_root)
        ledger_before = (self.run_root / "budget_ledger.jsonl").read_bytes()
        backend = CertificationBackend()

        with self.assertRaisesRegex(FinalCertificationError, "task_id"):
            certify_frozen_candidate(
                wrong_task,
                self.run_root,
                self.config,
                backend=backend,
            )

        self.assertEqual(backend.calls, [])
        self.assertEqual(backend.fingerprint_calls, 0)
        self.assertEqual(
            (self.run_root / "budget_ledger.jsonl").read_bytes(),
            ledger_before,
        )

    def test_tool_config_mismatch_rejects_before_backend_and_preserves_ledger(
        self,
    ) -> None:
        mismatched_config = CertificationConfig(
            ToolConfig(
                vitis_root=self.config.tool.vitis_root,
                part=self.config.tool.part,
                clock_ns=self.config.tool.clock_ns,
                timeouts={
                    "csim": 1.0,
                    "synth": 2.0,
                    "cosim": 1.0,
                },
                flow_target=self.config.tool.flow_target,
                toolchain_id=self.config.tool.toolchain_id,
            )
        )
        ledger_before = (self.run_root / "budget_ledger.jsonl").read_bytes()
        backend = CertificationBackend()

        with self.assertRaisesRegex(
            FinalCertificationError,
            "tool configuration does not match",
        ):
            certify_frozen_candidate(
                self.task,
                self.run_root,
                mismatched_config,
                backend=backend,
            )

        self.assertEqual(backend.calls, [])
        self.assertEqual(backend.fingerprint_calls, 0)
        self.assertEqual(
            (self.run_root / "budget_ledger.jsonl").read_bytes(),
            ledger_before,
        )

    def test_supplied_search_result_must_match_persisted_result(self) -> None:
        supplied = json.loads(
            (self.run_root / "v3_prototype_result.json").read_text(
                encoding="utf-8"
            )
        )
        supplied["task_id"] = "other_task"

        with self.assertRaisesRegex(
            FinalCertificationError,
            "differs from v3_prototype_result.json",
        ):
            freeze_search_candidate(
                self.task,
                self.run_root,
                search_result=supplied,
            )


if __name__ == "__main__":
    unittest.main()
