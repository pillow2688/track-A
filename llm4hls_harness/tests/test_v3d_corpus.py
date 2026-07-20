from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from llm4hls_agent.task import (
    TaskPackageError,
    _FORBIDDEN_DIRECTORIES,
    load_public_task,
)
from llm4hls_agent.v3_openai_planner import (
    V3OpenAIPlannerError,
    _FORBIDDEN_PLANNER_PATH_COMPONENTS,
    _public_planner_path,
    _resolve_binding,
)
from llm4hls_agent.v3_phase_router import PhaseRouter
from llm4hls_agent.v3d_corpus import (
    ACCEPTANCE_SCHEMA_VERSION,
    CORPUS_SCHEMA_VERSION,
    MUTATION_ENGINE_VERSION,
    MUTATION_SCHEMA_VERSION,
    PHASE_MODES,
    PLANNER_FORBIDDEN_COMPONENTS,
    PLANNER_VISIBLE_PATHS,
    MutationEngine,
    TASK_SPECS,
    V3DCorpusError,
    _validate_public_metadata,
    corpus_drift,
    render_corpus_files,
    task_directories,
    validate_acceptance,
    validate_corpus_manifest,
    validate_mutation_manifest,
    write_corpus,
)


class V3DFastCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project = Path(__file__).resolve().parents[1]
        cls.corpus = cls.project / "task_corpus" / "v3d-fast"

    def test_corpus_has_28_complete_loadable_tasks_with_required_distribution(
        self,
    ) -> None:
        manifest_value = json.loads(
            (self.corpus / "corpus_manifest.json").read_text(encoding="utf-8")
        )
        manifest = validate_corpus_manifest(manifest_value)
        root_manifest = json.loads(
            (self.project / "task_corpus" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        collection = next(
            item
            for item in root_manifest["collections"]
            if item["id"] == "v3d_fast_corpus"
        )

        self.assertEqual(manifest["schema_version"], CORPUS_SCHEMA_VERSION)
        self.assertEqual(manifest["task_count"], 28)
        self.assertEqual(
            manifest["mode_counts"],
            {
                "REPAIR": 8,
                "SYNTH_FIX": 6,
                "STRUCTURAL_FIX": 6,
                "OPTIMIZE": 8,
            },
        )
        self.assertEqual(len(TASK_SPECS), 28)
        self.assertEqual(len(task_directories(self.corpus)), 28)
        self.assertEqual(collection["path"], "v3d-fast/tasks")
        self.assertEqual(collection["corpus_manifest"], "v3d-fast/corpus_manifest.json")
        self.assertEqual(collection["mode_counts"], manifest["mode_counts"])

        required_files = {
            "task.toml",
            "description.md",
            "kernel.cpp",
            "kernel.h",
            "kernel_tb.cpp",
            "hidden_like/kernel_tb.cpp",
            "golden/kernel.cpp",
            "mutation_manifest.json",
            "acceptance.json",
        }
        observed_modes: Counter[str] = Counter()
        for entry, task_dir in zip(
            manifest["tasks"], task_directories(self.corpus), strict=True
        ):
            with self.subTest(task_id=entry["task_id"]):
                actual_files = {
                    str(path.relative_to(task_dir))
                    for path in task_dir.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(actual_files, required_files)
                self.assertFalse(any(path.is_symlink() for path in task_dir.rglob("*")))

                task = load_public_task(task_dir)
                self.assertEqual(task.id, entry["task_id"])
                self.assertEqual(task.top, "kernel")
                self.assertEqual(task.kernel_name, "kernel.cpp")
                self.assertEqual(task.public_tb_name, "kernel_tb.cpp")
                self.assertEqual(set(task.headers), {"kernel.h"})
                acceptance = validate_acceptance(
                    json.loads(
                        (task_dir / "acceptance.json").read_text(encoding="utf-8")
                    )
                )
                self.assertEqual(task.requires_cosim, acceptance["requires_cosim"])
                self.assertTrue(set(task.public_file_hashes) <= set(PLANNER_VISIBLE_PATHS))
                for relative, digest in task.public_file_hashes.items():
                    self.assertEqual(
                        hashlib.sha256((task_dir / relative).read_bytes()).hexdigest(),
                        digest,
                    )
                self.assertFalse(
                    set(PLANNER_FORBIDDEN_COMPONENTS)
                    & {
                        part.casefold()
                        for name in task.public_file_hashes
                        for part in Path(name).parts
                    }
                )
                observed_modes[str(entry["mode"])] += 1

        self.assertEqual(dict(observed_modes), manifest["mode_counts"])

    def test_mutation_and_acceptance_manifests_are_hash_bound_and_replayable(
        self,
    ) -> None:
        engine = MutationEngine()
        corpus_manifest = validate_corpus_manifest(
            json.loads(
                (self.corpus / "corpus_manifest.json").read_text(encoding="utf-8")
            )
        )
        entries = {
            str(entry["task_id"]): entry for entry in corpus_manifest["tasks"]
        }
        for spec in TASK_SPECS:
            task_dir = self.corpus / "tasks" / spec.task_id
            with self.subTest(task_id=spec.task_id):
                mutation_bytes = (task_dir / "mutation_manifest.json").read_bytes()
                acceptance_bytes = (task_dir / "acceptance.json").read_bytes()
                mutation = validate_mutation_manifest(json.loads(mutation_bytes))
                acceptance = validate_acceptance(json.loads(acceptance_bytes))
                entry = entries[spec.task_id]
                golden_bytes = (task_dir / "golden" / "kernel.cpp").read_bytes()
                mutated_bytes = (task_dir / "kernel.cpp").read_bytes()

                self.assertEqual(mutation["schema_version"], MUTATION_SCHEMA_VERSION)
                self.assertEqual(
                    entry["mutation_manifest_sha256"],
                    hashlib.sha256(mutation_bytes).hexdigest(),
                )
                self.assertEqual(
                    entry["acceptance_sha256"],
                    hashlib.sha256(acceptance_bytes).hexdigest(),
                )
                self.assertEqual(mutation["engine_version"], MUTATION_ENGINE_VERSION)
                self.assertEqual(mutation["base_sha256"], hashlib.sha256(golden_bytes).hexdigest())
                self.assertEqual(mutation["mutated_sha256"], hashlib.sha256(mutated_bytes).hexdigest())
                self.assertEqual(mutation["operator"], spec.operator)
                self.assertEqual(mutation["seed"], spec.seed)
                self.assertEqual(mutation["expected_mode"], spec.mode)
                self.assertEqual(mutation["expected_failing_gate"], spec.failing_gate)
                self.assertEqual(
                    mutation["mutation_summary"], spec.mutation_summary_zh
                )
                self.assertEqual(acceptance["schema_version"], ACCEPTANCE_SCHEMA_VERSION)
                self.assertEqual(acceptance["task_id"], spec.task_id)
                self.assertEqual(acceptance["expected_mode"], spec.mode)
                self.assertEqual(acceptance["expected_failing_gate"], spec.failing_gate)
                self.assertEqual(acceptance["golden_kernel_sha256"], mutation["base_sha256"])
                self.assertEqual(acceptance["mutated_kernel_sha256"], mutation["mutated_sha256"])

                replay = engine.mutate(
                    golden_bytes.decode("utf-8"),
                    operator=spec.operator,
                    seed=spec.seed,
                )
                self.assertEqual(replay.source.encode("utf-8"), mutated_bytes)
                self.assertEqual(replay.base_range_sha256, mutation["source_range"]["base_range_sha256"])
                self.assertEqual(replay.base_start_line, mutation["source_range"]["base_start_line"])
                self.assertEqual(replay.base_end_line, mutation["source_range"]["base_end_line"])
                self.assertEqual(replay.mutated_start_line, mutation["source_range"]["mutated_start_line"])
                self.assertEqual(replay.mutated_end_line, mutation["source_range"]["mutated_end_line"])

    def test_public_metadata_contains_only_contract_not_evaluator_answers(self) -> None:
        evaluator_labels = {
            *PHASE_MODES,
            *(spec.operator for spec in TASK_SPECS),
            *(spec.mutation_summary_zh for spec in TASK_SPECS),
            "expected_failing_gate",
            "expected_mode",
            "mutation_summary",
            "失败门",
            "基线",
            "缺陷",
            "修复提示",
        }
        for index, spec in enumerate(TASK_SPECS, start=1):
            task_dir = self.corpus / "tasks" / spec.task_id
            task_toml = (task_dir / "task.toml").read_text(encoding="utf-8")
            description = (task_dir / "description.md").read_text(encoding="utf-8")
            exposed = task_toml + "\n" + description
            with self.subTest(task_id=spec.task_id):
                self.assertEqual(spec.task_id, f"v3d_fast_{index:03d}")
                self.assertIn('task_type = "generate"', task_toml)
                self.assertIn("kernel(const int input[16], int output[16])", description)
                self.assertIn("合法输入元素范围", exposed)
                for label in evaluator_labels:
                    self.assertNotIn(label.casefold(), exposed.casefold())

                task = load_public_task(task_dir)
                self.assertEqual(task.task_type, "generate")
                self.assertNotIn(spec.mode.casefold(), task.initial_condition.casefold())
                self.assertNotIn(spec.operator.casefold(), task.description.casefold())

        spec = TASK_SPECS[0]
        for leaked_value in (
            spec.mode,
            spec.operator,
            spec.mutation_summary_zh,
            "expected_failing_gate",
        ):
            with self.subTest(rejected_leak=leaked_value):
                with self.assertRaisesRegex(V3DCorpusError, "leaks evaluator"):
                    _validate_public_metadata(
                        spec,
                        'task_id = "v3d_fast_001"\ntask_type = "generate"\n',
                        f"合法功能契约。{leaked_value}",
                    )

    def test_checked_in_corpus_has_no_generator_drift(self) -> None:
        self.assertEqual(corpus_drift(self.corpus), ())
        rendered = render_corpus_files()
        self.assertEqual(len(rendered), 257)
        with tempfile.TemporaryDirectory() as directory:
            regenerated = Path(directory) / "v3d-fast"
            write_corpus(regenerated)
            self.assertEqual(corpus_drift(regenerated), ())
            self.assertEqual(
                {
                    str(path.relative_to(regenerated)): path.read_bytes()
                    for path in regenerated.rglob("*")
                    if path.is_file()
                },
                rendered,
            )

    def test_acceptance_gate_matrix_routes_to_the_declared_v3c_mode(self) -> None:
        router = PhaseRouter()

        def record(status: str) -> dict[str, object] | None:
            return None if status == "NOT_RUN" else {"status": status}

        for task_dir in task_directories(self.corpus):
            acceptance = validate_acceptance(
                json.loads((task_dir / "acceptance.json").read_text(encoding="utf-8"))
            )
            task = load_public_task(task_dir)
            baseline = acceptance["baseline_validation"]
            with self.subTest(task_id=task.id):
                decision = router.route(
                    baseline_csim=record(baseline["csim"]),
                    baseline_synth=record(baseline["synth"]),
                    baseline_cosim=record(baseline["cosim"]),
                    task_metadata=task,
                )
                self.assertEqual(decision.mode.value, acceptance["expected_mode"])

    def test_optimize_may_require_cosim_but_must_bind_both_cosim_passes(self) -> None:
        optimize_acceptances = []
        for spec in TASK_SPECS:
            if spec.mode != "OPTIMIZE":
                continue
            value = json.loads(
                (
                    self.corpus
                    / "tasks"
                    / spec.task_id
                    / "acceptance.json"
                ).read_text(encoding="utf-8")
            )
            optimize_acceptances.append(value)
            if value["requires_cosim"]:
                self.assertEqual(value["baseline_validation"]["cosim"], "PASS")
                self.assertEqual(value["golden_validation"]["cosim"], "PASS")
        self.assertTrue(any(item["requires_cosim"] for item in optimize_acceptances))

        tampered = dict(optimize_acceptances[0])
        tampered["requires_cosim"] = True
        tampered["baseline_validation"] = dict(tampered["baseline_validation"])
        tampered["baseline_validation"]["cosim"] = "NOT_RUN"
        with self.assertRaisesRegex(V3DCorpusError, "baseline validation"):
            validate_acceptance(tampered)
        tampered["baseline_validation"]["cosim"] = "PASS"
        validate_acceptance(tampered)

    def test_loader_and_planner_reject_private_corpus_paths(self) -> None:
        self.assertEqual(
            set(PLANNER_FORBIDDEN_COMPONENTS),
            {"answer", "golden", "hidden", "hidden_like", "reference"},
        )
        self.assertEqual(set(PLANNER_FORBIDDEN_COMPONENTS), _FORBIDDEN_DIRECTORIES)
        self.assertEqual(
            set(PLANNER_FORBIDDEN_COMPONENTS),
            _FORBIDDEN_PLANNER_PATH_COMPONENTS,
        )
        fixture = self.corpus / "tasks" / TASK_SPECS[0].task_id
        for component in PLANNER_FORBIDDEN_COMPONENTS:
            with self.subTest(boundary="loader", component=component), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for source in fixture.iterdir():
                    if source.is_file():
                        shutil.copyfile(source, root / source.name)
                private = root / component
                private.mkdir()
                shutil.copyfile(fixture / "kernel.cpp", private / "kernel.cpp")
                task_toml = root / "task.toml"
                task_toml.write_text(
                    task_toml.read_text(encoding="utf-8").replace(
                        'kernel_file = "kernel.cpp"',
                        f'kernel_file = "{component}/kernel.cpp"',
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(TaskPackageError):
                    load_public_task(root)

            with self.subTest(boundary="planner", component=component), tempfile.TemporaryDirectory() as directory:
                run_root = Path(directory).resolve()
                private = run_root / component
                private.mkdir()
                artifact = private / "kernel.cpp"
                artifact.write_bytes(b"void kernel() {}\n")
                binding = {
                    "ref": f"{component}/kernel.cpp",
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                }
                with self.assertRaisesRegex(V3OpenAIPlannerError, "forbidden"):
                    _resolve_binding(run_root, binding, name="incumbent.source")

                with self.assertRaisesRegex(V3OpenAIPlannerError, "forbidden"):
                    _public_planner_path(
                        f"public/{component.upper()}/kernel.h",
                        "read_only_headers",
                    )

        self.assertEqual(
            _public_planner_path("public/kernel.h", "read_only_headers"),
            "public/kernel.h",
        )

    @unittest.skipUnless(shutil.which("g++"), "g++ is required for corpus CSim smoke")
    def test_public_csim_shape_and_golden_public_hidden_like_closure(self) -> None:
        compiler = shutil.which("g++")
        assert compiler is not None
        with tempfile.TemporaryDirectory() as directory:
            binaries = Path(directory)
            for spec in TASK_SPECS:
                task_dir = self.corpus / "tasks" / spec.task_id
                cases = (
                    ("baseline-public", task_dir / "kernel.cpp", task_dir / "kernel_tb.cpp", spec.mode != "REPAIR"),
                    ("golden-public", task_dir / "golden" / "kernel.cpp", task_dir / "kernel_tb.cpp", True),
                    ("golden-hidden-like", task_dir / "golden" / "kernel.cpp", task_dir / "hidden_like" / "kernel_tb.cpp", True),
                )
                for label, kernel, testbench, expected_pass in cases:
                    with self.subTest(task_id=spec.task_id, case=label):
                        executable = binaries / f"{spec.task_id}-{label}"
                        compile_result = subprocess.run(
                            [
                                compiler,
                                "-std=c++17",
                                "-Wno-unknown-pragmas",
                                f"-I{task_dir}",
                                str(kernel),
                                str(testbench),
                                "-o",
                                str(executable),
                            ],
                            check=False,
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        self.assertEqual(
                            compile_result.returncode,
                            0,
                            compile_result.stderr,
                        )
                        run_result = subprocess.run(
                            [str(executable)],
                            check=False,
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        if expected_pass:
                            self.assertEqual(run_result.returncode, 0, run_result.stderr)
                        else:
                            self.assertNotEqual(run_result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
