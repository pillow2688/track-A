from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm4hls_agent.v3d_oracle_validator import (
    GATES,
    CorpusOracleValidator,
    DeterministicOracleBackend,
    EvidenceClass,
    OracleConfig,
    OracleConfigurationError,
    VitisOracleBackend,
    _ppa_observations,
    _vitis_gate_status,
    expected_gate_statuses,
    main,
    mode_gate_scope,
)
from llm4hls_agent.tools import BackendResult
from llm4hls_agent.vitis import ProcessResult


class _ProjectedBackend:
    evidence_class = EvidenceClass.DETERMINISTIC
    real_anchor_authority = None
    requires_vitis_lock = False

    def __init__(self, *, mismatch: bool = False, clock=None) -> None:
        self.mismatch = mismatch
        self.clock = clock

    def fingerprint(self) -> str:
        return "tests.projected-oracle:v1"

    def validate(self, task, *, run_dir, timeout_seconds):
        del run_dir, timeout_seconds
        if self.clock is not None:
            self.clock.value += 2.0
        expected = expected_gate_statuses(task.acceptance)
        observations = {
            subject: {
                gate: {"status": expected[subject][gate]}
                for gate in GATES
            }
            for subject in ("baseline", "golden")
        }
        if self.mismatch:
            first_gate = mode_gate_scope(task.mode)[0]
            current = observations["baseline"][first_gate]["status"]
            observations["baseline"][first_gate]["status"] = (
                "PASS" if current != "PASS" else "FAIL"
            )
        return observations


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class _LyingRealBackend(_ProjectedBackend):
    evidence_class = EvidenceClass.REAL_VITIS
    real_anchor_authority = "VITIS_STRUCTURED_CORPUS_ORACLE_V1"


class _FakeVitisProcessRunner:
    """Create the reports VitisBackend expects without launching Vitis."""

    def run(self, command, *, cwd, timeout_s):
        del command, timeout_s
        tcl_path = cwd / "run_hls.tcl"
        if not tcl_path.is_file():
            return ProcessResult(0, "", "", 0.01, False)
        tcl = tcl_path.read_text(encoding="utf-8")
        if "csim_design -setup" in tcl:
            executable = cwd / "csim_proj" / "sol" / "csim" / "build" / "csim.exe"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"fake executable\n")
        if "csynth_design" in tcl:
            project = "cosim_proj" if "cosim_design" in tcl else "synth_proj"
            report = cwd / project / "sol" / "syn" / "report" / "csynth.xml"
            report.parent.mkdir(parents=True, exist_ok=True)
            golden = "golden" in cwd.parts
            latency = 50 if golden else 100
            interval = 1 if golden else 4
            report.write_text(
                f"""<Report>
<PerformanceEstimates>
  <SummaryOfTimingAnalysis><EstimatedClockPeriod>4.0</EstimatedClockPeriod></SummaryOfTimingAnalysis>
  <SummaryOfOverallLatency>
    <Best-caseLatency>{latency}</Best-caseLatency>
    <Average-caseLatency>{latency}</Average-caseLatency>
    <Worst-caseLatency>{latency}</Worst-caseLatency>
    <Interval-min>{interval}</Interval-min>
    <Interval-max>{interval}</Interval-max>
  </SummaryOfOverallLatency>
</PerformanceEstimates>
<AreaEstimates>
  <Resources><LUT>100</LUT><FF>100</FF><DSP>1</DSP><BRAM_18K>0</BRAM_18K><URAM>0</URAM></Resources>
  <AvailableResources><LUT>10000</LUT><FF>10000</FF><DSP>100</DSP><BRAM_18K>100</BRAM_18K><URAM>10</URAM></AvailableResources>
</AreaEstimates>
</Report>
""",
                encoding="utf-8",
            )
        return ProcessResult(0, "fake Vitis", "", 0.01, False)


class V3DOracleValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project = Path(__file__).resolve().parents[1]
        cls.corpus = cls.project / "task_corpus" / "v3d-fast"
        cls.manifest = json.loads(
            (cls.corpus / "corpus_manifest.json").read_text(encoding="utf-8")
        )

    def test_mode_gate_scopes_and_expected_ppa_are_explicit(self) -> None:
        expected_scopes = {
            "REPAIR": ("csim", "synth"),
            "SYNTH_FIX": ("csim", "synth"),
            "STRUCTURAL_FIX": ("csim", "synth", "cosim"),
            "OPTIMIZE": ("csim", "synth", "ppa"),
        }
        observed = set()
        for entry in self.manifest["tasks"]:
            task_dir = self.corpus / entry["path"]
            acceptance = json.loads(
                (task_dir / "acceptance.json").read_text(encoding="utf-8")
            )
            mode = entry["mode"]
            statuses = expected_gate_statuses(acceptance)
            observed.add(mode)
            self.assertEqual(mode_gate_scope(mode), expected_scopes[mode])
            if mode == "OPTIMIZE":
                self.assertEqual(statuses["baseline"]["ppa"], "FAIL")
                self.assertEqual(statuses["golden"]["ppa"], "PASS")
            else:
                self.assertEqual(statuses["baseline"]["ppa"], "NOT_RUN")
                self.assertEqual(statuses["golden"]["ppa"], "NOT_RUN")
            if mode == "REPAIR":
                self.assertEqual(statuses["baseline"]["synth"], "NOT_RUN")
                self.assertEqual(statuses["golden"]["synth"], "PASS")
        self.assertEqual(observed, set(expected_scopes))

    def test_optimize_requires_cosim_adds_baseline_and_golden_cosim(self) -> None:
        acceptance = {
            "expected_mode": "OPTIMIZE",
            "requires_cosim": True,
            "baseline_validation": {
                "csim": "PASS",
                "synth": "PASS",
                "cosim": "PASS",
            },
            "golden_validation": {
                "csim": "PASS",
                "synth": "PASS",
                "cosim": "PASS",
            },
        }
        self.assertEqual(
            mode_gate_scope("OPTIMIZE", requires_cosim=True),
            ("csim", "synth", "cosim", "ppa"),
        )
        # Corpus schema validation is tested by v3d_corpus; this isolates the
        # Oracle routing contract so a requires-cosim optimize task executes
        # both baseline and golden CoSim once that schema admits it.
        with patch(
            "llm4hls_agent.v3d_oracle_validator.validate_acceptance",
            return_value=acceptance,
        ):
            statuses = expected_gate_statuses(acceptance)
        self.assertEqual(statuses["baseline"]["cosim"], "PASS")
        self.assertEqual(statuses["golden"]["cosim"], "PASS")

    def test_ppa_admission_requires_strict_worst_latency_improvement(self) -> None:
        def report(latency: int, interval: int, lut: int) -> dict[str, object]:
            return {
                "latency": {"worst": latency},
                "interval": {"max": interval},
                "resources": {
                    "LUT": lut,
                    "FF": 1,
                    "DSP": 0,
                    "BRAM_18K": 0,
                    "URAM": 0,
                },
                "available_resources": {
                    "LUT": 1000,
                    "FF": 1000,
                    "DSP": 100,
                    "BRAM_18K": 100,
                    "URAM": 100,
                },
            }

        baseline, golden = _ppa_observations(
            report(100, 4, 100), report(100, 1, 1)
        )
        self.assertEqual(baseline["status"], "PASS")
        self.assertEqual(golden["status"], "FAIL")
        self.assertIn("strictly lower", golden["comparison_policy"])

        baseline, golden = _ppa_observations(
            report(100, 4, 100), report(99, 8, 200)
        )
        self.assertEqual(baseline["status"], "FAIL")
        self.assertEqual(golden["status"], "PASS")

    def test_expected_fail_gates_require_semantic_evidence(self) -> None:
        def failed(
            phase: str,
            *evidence: str,
            cosim: dict[str, object] | None = None,
        ) -> BackendResult:
            return BackendResult(
                False,
                phase,
                1,
                0.1,
                list(evidence),
                {},
                cosim=cosim,
            )

        cases = (
            (
                "repair mismatch",
                "csim",
                failed("runtime_fail", "public mismatch: expected 7, got 0"),
                "FAIL",
            ),
            (
                "repair compile error",
                "csim",
                failed("compile_error", "source did not compile"),
                "ERROR",
            ),
            (
                "synth unsupported source",
                "synth",
                failed(
                    "synth_error",
                    "ERROR: [HLS 214-194] Undefined function operator new[]",
                    "Encountered problem during source synthesis",
                ),
                "FAIL",
            ),
            (
                "synth missing report",
                "synth",
                failed("synth_error", "csynth.xml is missing"),
                "ERROR",
            ),
            (
                "structural deadlock without report",
                "cosim",
                failed(
                    "cosim_fail",
                    "cosim report is missing",
                    "ERROR!!! DEADLOCK DETECTED",
                    "Blocked by empty input FIFO",
                ),
                "FAIL",
            ),
            (
                "structural rtl mismatch report",
                "cosim",
                failed("cosim_fail", "return_code=1", cosim={"status": "Fail"}),
                "FAIL",
            ),
            (
                "structural missing report only",
                "cosim",
                failed("cosim_fail", "cosim report is missing"),
                "ERROR",
            ),
            (
                "semantic xsim timeout",
                "cosim",
                failed("timeout", "subprocess timeout expired", "# xsim kernel"),
                "FAIL",
            ),
            (
                "generic tool timeout",
                "cosim",
                failed("timeout", "subprocess timeout expired"),
                "ERROR",
            ),
            (
                "xsim internal exception",
                "cosim",
                failed(
                    "cosim_fail",
                    "cosim report is missing",
                    "XSIM internal exception while starting simulation",
                ),
                "ERROR",
            ),
        )
        for label, gate, result, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(_vitis_gate_status(gate, result), expected)

    def test_demo_full_queue_is_structured_but_never_a_real_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outcome = CorpusOracleValidator(
                OracleConfig(
                    corpus=self.corpus,
                    output_dir=directory,
                    backend="demo",
                )
            ).run()
            self.assertEqual(outcome.summary["status"], "COMPLETE")
            self.assertTrue(outcome.summary["oracle_pass"])
            self.assertEqual(
                outcome.summary["counts"],
                {
                    "accepted": 28,
                    "rejected": 0,
                    "pending": 0,
                    "real_vitis_anchors": 0,
                    "fixture_acceptances": 28,
                },
            )
            self.assertFalse(outcome.summary["backend"]["real_anchor_authorized"])
            self.assertTrue(all(not record["real_anchor"] for record in outcome.records))
            self.assertTrue((Path(directory) / "accepted.json").is_file())
            self.assertTrue((Path(directory) / "rejected.json").is_file())

    def test_max_tasks_resume_advances_the_durable_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = CorpusOracleValidator(
                OracleConfig(
                    corpus=self.corpus,
                    output_dir=directory,
                    backend="deterministic",
                    max_tasks=2,
                ),
                backend=_ProjectedBackend(),
            ).run()
            self.assertEqual(first.summary["counts"]["accepted"], 2)
            self.assertEqual(first.summary["counts"]["pending"], 26)
            self.assertEqual(
                first.summary["execution"]["stopped_reason"], "MAX_TASKS_REACHED"
            )

            second = CorpusOracleValidator(
                OracleConfig(
                    corpus=self.corpus,
                    output_dir=directory,
                    backend="deterministic",
                    max_tasks=3,
                    resume=True,
                ),
                backend=_ProjectedBackend(),
            ).run()
            self.assertEqual(second.summary["counts"]["accepted"], 5)
            self.assertEqual(second.summary["counts"]["pending"], 23)
            self.assertEqual(second.summary["execution"]["resumed_tasks"], 2)
            queue = json.loads(
                (Path(directory) / "oracle_queue.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [entry["state"] for entry in queue["entries"][:6]],
                ["ACCEPTED"] * 5 + ["PENDING"],
            )

    def test_runtime_limit_stops_before_the_next_serial_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = _Clock()
            outcome = CorpusOracleValidator(
                OracleConfig(
                    corpus=self.corpus,
                    output_dir=directory,
                    backend="deterministic",
                    max_runtime_seconds=1.0,
                ),
                backend=_ProjectedBackend(clock=clock),
                monotonic=clock,
            ).run()
            self.assertEqual(outcome.summary["counts"]["accepted"], 1)
            self.assertEqual(outcome.summary["counts"]["pending"], 27)
            self.assertEqual(
                outcome.summary["execution"]["stopped_reason"],
                "MAX_RUNTIME_REACHED",
            )

    def test_mismatch_is_copied_and_registered_in_rejected_corpus(self) -> None:
        task_id = self.manifest["tasks"][0]["task_id"]
        with tempfile.TemporaryDirectory() as directory:
            outcome = CorpusOracleValidator(
                OracleConfig(
                    corpus=self.corpus,
                    output_dir=directory,
                    backend="deterministic",
                    task_filters=(task_id,),
                ),
                backend=_ProjectedBackend(mismatch=True),
            ).run()
            self.assertEqual(outcome.summary["counts"]["rejected"], 1)
            self.assertFalse(outcome.summary["oracle_pass"])
            record = outcome.records[0]
            self.assertIn("BASELINE_CSIM_STATUS_MISMATCH", record["reason_codes"])
            rejected = Path(directory) / "rejected_corpus"
            copied = rejected / "tasks" / task_id
            self.assertTrue((copied / "kernel.cpp").is_file())
            reason = json.loads(
                (copied / "rejection.json").read_text(encoding="utf-8")
            )
            self.assertIn("BASELINE_CSIM_STATUS_MISMATCH", reason["reason_codes"])
            registry = json.loads(
                (rejected / "rejected_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(registry["rejection_count"], 1)

    def test_resume_can_retry_a_rejected_queue_entry_without_erasing_history(self) -> None:
        task_id = self.manifest["tasks"][0]["task_id"]
        with tempfile.TemporaryDirectory() as directory:
            first = CorpusOracleValidator(
                OracleConfig(
                    corpus=self.corpus,
                    output_dir=directory,
                    backend="deterministic",
                    task_filters=(task_id,),
                ),
                backend=_ProjectedBackend(mismatch=True),
            ).run()
            self.assertEqual(first.summary["counts"]["rejected"], 1)

            second = CorpusOracleValidator(
                OracleConfig(
                    corpus=self.corpus,
                    output_dir=directory,
                    backend="deterministic",
                    task_filters=(task_id,),
                    resume=True,
                    retry_rejected=True,
                ),
                backend=_ProjectedBackend(),
            ).run()
            self.assertTrue(second.summary["oracle_pass"])
            self.assertEqual(second.summary["counts"]["accepted"], 1)
            self.assertEqual(second.records[0]["attempt"], 2)
            registry = json.loads(
                (
                    Path(directory)
                    / "rejected_corpus"
                    / "rejected_manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(registry["rejection_count"], 1)

    def test_deterministic_backend_cannot_impersonate_a_real_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                OracleConfigurationError, "cannot claim evidence class"
            ):
                CorpusOracleValidator(
                    OracleConfig(
                        corpus=self.corpus,
                        output_dir=directory,
                        backend="deterministic",
                    ),
                    backend=_LyingRealBackend(),
                )

    def test_injected_vitis_runner_is_fixture_only_and_never_real(self) -> None:
        task_id = next(
            entry["task_id"]
            for entry in self.manifest["tasks"]
            if entry["mode"] == "OPTIMIZE"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vitis_root = root / "fake-vitis"
            vitis_root.mkdir()
            (vitis_root / "settings64.sh").write_text("# fake\n", encoding="utf-8")
            config = OracleConfig(
                corpus=self.corpus,
                output_dir=root / "output",
                backend="deterministic",
                task_filters=(task_id,),
                vitis_root=str(vitis_root),
            )
            backend = VitisOracleBackend(
                vitis_root=str(vitis_root),
                csim_timeout_seconds=60,
                synth_timeout_seconds=60,
                cosim_timeout_seconds=60,
                toolchain_id="fake Vitis",
                runner=_FakeVitisProcessRunner(),
            )
            outcome = CorpusOracleValidator(config, backend=backend).run()
            self.assertTrue(outcome.summary["oracle_pass"])
            self.assertEqual(backend.evidence_class, EvidenceClass.DETERMINISTIC)
            self.assertIsNone(backend.real_anchor_authority)
            self.assertFalse(outcome.summary["backend"]["real_anchor_authorized"])
            self.assertEqual(outcome.summary["counts"]["real_vitis_anchors"], 0)
            self.assertEqual(outcome.summary["counts"]["fixture_acceptances"], 1)
            record = outcome.records[0]
            self.assertFalse(record["real_anchor"])
            self.assertEqual(
                record["observations"]["baseline"]["ppa"]["status"], "FAIL"
            )
            self.assertEqual(
                record["observations"]["golden"]["ppa"]["status"], "PASS"
            )
            self.assertTrue(
                record["observations"]["golden"]["synth"]["artifact_hashes"]
            )

    def test_default_vitis_runner_probes_and_binds_real_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vitis"
            root.mkdir()
            arguments = {
                "vitis_root": str(root),
                "csim_timeout_seconds": 60,
                "synth_timeout_seconds": 60,
                "cosim_timeout_seconds": 60,
                "toolchain_id": "Vitis probe fixture",
            }
            with self.assertRaisesRegex(
                OracleConfigurationError, "settings file is missing"
            ):
                VitisOracleBackend(**arguments)

            (root / "settings64.sh").write_text("# fixture\n", encoding="utf-8")
            with self.assertRaisesRegex(
                OracleConfigurationError, "executable is missing or not executable"
            ):
                VitisOracleBackend(**arguments)

            executable = root / "bin" / "vitis-run"
            executable.parent.mkdir()
            executable.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '****** vitis-run v2025.2 (64-bit)'\n"
                "printf '%s\\n' '  **** SW Build 6295257'\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            backend = VitisOracleBackend(**arguments)
            self.assertEqual(backend.evidence_class, EvidenceClass.REAL_VITIS)
            self.assertEqual(
                backend.real_anchor_authority,
                "VITIS_STRUCTURED_CORPUS_ORACLE_V1",
            )
            self.assertTrue(backend.uses_default_runner)
            self.assertEqual(backend.toolchain_id, "Vitis 2025.2")
            self.assertEqual(
                backend.toolchain_probe,
                {
                    "executable_path": str(executable.resolve()),
                    "executable_sha256": hashlib.sha256(
                        executable.read_bytes()
                    ).hexdigest(),
                    "detected_version": "2025.2",
                    "version_summary": (
                        "****** vitis-run v2025.2 (64-bit) | "
                        "**** SW Build 6295257"
                    ),
                    "toolchain_id": "Vitis 2025.2",
                },
            )

            spoofed_arguments = {**arguments, "toolchain_id": "Vitis 2099.9"}
            spoofed_backend = VitisOracleBackend(**spoofed_arguments)
            self.assertEqual(spoofed_backend.toolchain_id, "Vitis 2025.2")
            self.assertEqual(spoofed_backend.fingerprint(), backend.fingerprint())

            executable.write_text(
                executable.read_text(encoding="utf-8") + "# changed binary bytes\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            changed_executable_backend = VitisOracleBackend(**arguments)
            self.assertNotEqual(
                changed_executable_backend.fingerprint(), backend.fingerprint()
            )

    def test_default_vitis_runner_rejects_unverified_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vitis"
            (root / "bin").mkdir(parents=True)
            (root / "settings64.sh").write_text("# fixture\n", encoding="utf-8")
            executable = root / "bin" / "vitis-run"
            executable.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'vitis-run v2024.2'\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            arguments = {
                "vitis_root": str(root),
                "csim_timeout_seconds": 60,
                "synth_timeout_seconds": 60,
                "cosim_timeout_seconds": 60,
                "toolchain_id": "Vitis 2025.2",
            }
            with self.assertRaisesRegex(
                OracleConfigurationError,
                r"unsupported version 2024\.2; expected 2025\.2",
            ):
                VitisOracleBackend(**arguments)

            executable.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
            executable.chmod(0o755)
            with self.assertRaisesRegex(
                OracleConfigurationError, "failed with return code 9"
            ):
                VitisOracleBackend(**arguments)

    def test_custom_real_backend_string_cannot_authorize_anchor(self) -> None:
        task_id = self.manifest["tasks"][0]["task_id"]
        with tempfile.TemporaryDirectory() as directory:
            outcome = CorpusOracleValidator(
                OracleConfig(
                    corpus=self.corpus,
                    output_dir=directory,
                    backend="vitis",
                    task_filters=(task_id,),
                ),
                backend=_LyingRealBackend(),
            ).run()
            self.assertTrue(outcome.summary["oracle_pass"])
            self.assertFalse(outcome.summary["backend"]["real_anchor_authorized"])
            self.assertEqual(outcome.summary["counts"]["real_vitis_anchors"], 0)
            self.assertFalse(outcome.records[0]["real_anchor"])

    @unittest.skipUnless(shutil.which("g++"), "g++ is required for native CSim")
    def test_deterministic_native_csim_accepts_one_repair_fixture(self) -> None:
        task_id = self.manifest["tasks"][0]["task_id"]
        with tempfile.TemporaryDirectory() as directory:
            outcome = CorpusOracleValidator(
                OracleConfig(
                    corpus=self.corpus,
                    output_dir=directory,
                    backend="deterministic",
                    task_filters=(task_id,),
                ),
                backend=DeterministicOracleBackend("g++"),
            ).run()
            self.assertTrue(outcome.summary["oracle_pass"])
            record = outcome.records[0]
            self.assertEqual(
                record["observations"]["baseline"]["csim"]["status"], "FAIL"
            )
            self.assertEqual(
                record["observations"]["golden"]["csim"]["status"], "PASS"
            )
            self.assertFalse(record["real_anchor"])

    def test_cli_emits_partial_structured_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            from io import StringIO

            output = StringIO()
            code = main(
                [
                    "--corpus",
                    str(self.corpus),
                    "--output-dir",
                    directory,
                    "--backend",
                    "demo",
                    "--max-tasks",
                    "1",
                ],
                stdout=output,
            )
            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "PARTIAL")
            self.assertEqual(payload["accepted"], 1)
            self.assertEqual(payload["pending"], 27)
            self.assertEqual(payload["real_vitis_anchors"], 0)


if __name__ == "__main__":
    unittest.main()
