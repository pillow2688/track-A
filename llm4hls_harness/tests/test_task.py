from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

try:
    from llm4hls_agent.task import TaskPackageError, _public_path, load_public_task
except ModuleNotFoundError:
    class TaskPackageError(Exception):
        pass

    def load_public_task(_root: Path):
        raise AssertionError("public-only task loader is not implemented")

    def _public_path(_root: Path, _name: object, *, field: str):
        raise AssertionError("public-only task loader is not implemented")


def write_public_task(root: Path, *, kernel_file: str = "kernel.cpp") -> None:
    (root / "task.toml").write_text(
        "\n".join(
            [
                'task_id = "fixture"',
                'task_type = "structural"',
                "difficulty = 4",
                'top = "kernel"',
                f'kernel_file = "{kernel_file}"',
                'header_files = ["kernel.h"]',
                'public_tb = "kernel_tb.cpp"',
                "budget = 80",
                "requires_cosim = true",
                'initial_condition = "public evidence only"',
                "",
                "[target]",
                'part = "xcu55c-fsvh2892-2L-e"',
                "clock_ns = 5.0",
            ]
        ),
        encoding="utf-8",
    )
    (root / "description.md").write_text("Fixture description.\n", encoding="utf-8")
    (root / "kernel.cpp").write_bytes(b"#include \"kernel.h\"\nvoid kernel() {}\n")
    (root / "kernel.h").write_bytes(b"void kernel();\n")
    (root / "kernel_tb.cpp").write_bytes(b"int main() { return 0; }\n")


class PublicTaskLoaderTests(unittest.TestCase):
    def test_v1_compile_and_synthesis_fixtures_load_as_public_tasks(self) -> None:
        examples = Path(__file__).parents[1] / "examples"
        expected = {
            "u55c_compile_repair_task": "u55c_compile_repair",
            "u55c_synthesis_repair_task": "u55c_synthesis_repair",
        }
        for directory, task_id in expected.items():
            with self.subTest(directory=directory):
                task = load_public_task(examples / directory)
                self.assertEqual(task.id, task_id)
                self.assertEqual(task.part, "xcu55c-fsvh2892-2L-e")
                self.assertEqual(task.clock_ns, 10.0)
                self.assertTrue(task.requires_cosim)

    def test_loads_only_public_files_and_returns_immutable_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_public_task(root)
            (root / "hidden").mkdir()
            (root / "reference").mkdir()
            (root / "hidden" / "kernel_tb.cpp").write_bytes(b"forbidden")
            (root / "reference" / "kernel.cpp").write_bytes(b"forbidden")

            original_open = Path.open

            def guarded_open(path: Path, *args: object, **kwargs: object):
                if {"hidden", "reference"}.intersection(path.parts):
                    raise AssertionError(f"forbidden path was read: {path}")
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", guarded_open):
                task = load_public_task(root)

            self.assertEqual(task.id, "fixture")
            self.assertEqual(task.task_type, "structural")
            self.assertTrue(task.requires_cosim)
            self.assertEqual(task.kernel_bytes, (root / "kernel.cpp").read_bytes())
            self.assertEqual(set(task.headers), {"kernel.h"})
            self.assertNotIn("hidden", " ".join(task.public_file_hashes))
            self.assertNotIn("reference", " ".join(task.public_file_hashes))

            with self.assertRaises(FrozenInstanceError):
                task.top = "changed"  # type: ignore[misc]
            with self.assertRaises(TypeError):
                task.headers["changed.h"] = b""  # type: ignore[index]

    def test_rejects_task_paths_that_escape_or_enter_forbidden_directories(self) -> None:
        for kernel_file in (
            "../outside.cpp",
            "answer/kernel.cpp",
            "golden/kernel.cpp",
            "hidden/kernel.cpp",
            "hidden_like/kernel.cpp",
            "reference/kernel.cpp",
        ):
            with self.subTest(kernel_file=kernel_file):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    write_public_task(root, kernel_file=kernel_file)
                    with self.assertRaises(TaskPackageError):
                        load_public_task(root)

    def test_rejects_symlink_that_resolves_into_reference_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference = root / "reference"
            reference.mkdir()
            target = reference / "kernel.cpp"
            target.write_bytes(b"forbidden fixture")
            alias = root / "public-looking.cpp"
            try:
                alias.symlink_to(target)
            except OSError as exc:
                with patch.object(Path, "resolve", return_value=target):
                    with self.assertRaises(TaskPackageError, msg=str(exc)):
                        _public_path(root, alias.name, field="kernel_file")
            else:
                with self.assertRaises(TaskPackageError):
                    _public_path(root, alias.name, field="kernel_file")

    def test_fixed_public_metadata_names_use_the_same_resolved_path_guard(self) -> None:
        for redirected_name in ("task.toml", "description.md"):
            with self.subTest(redirected_name=redirected_name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    write_public_task(root)
                    original_resolve = Path.resolve

                    def redirected_resolve(path: Path, *args: object, **kwargs: object):
                        if path.parent == root and path.name == redirected_name:
                            return root / "reference" / redirected_name
                        return original_resolve(path, *args, **kwargs)

                    with patch.object(Path, "resolve", redirected_resolve):
                        with self.assertRaises(TaskPackageError):
                            load_public_task(root)

    def test_environment_target_overrides_task_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_public_task(root)
            with patch.dict(
                "os.environ",
                {"LLM4HLS_PART": "env-part", "LLM4HLS_CLOCK_NS": "7.5"},
            ):
                task = load_public_task(root)

            self.assertEqual(task.part, "env-part")
        self.assertEqual(task.clock_ns, 7.5)

    def test_empty_description_is_still_part_of_the_public_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            write_public_task(task_dir)
            (task_dir / "description.md").write_bytes(b"")

            task = load_public_task(task_dir)

            self.assertIn("description.md", task.public_file_hashes)
            self.assertEqual(
                task.public_file_hashes["description.md"],
                hashlib.sha256(b"").hexdigest(),
            )

    def test_rejects_task_root_inside_forbidden_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "reference" / "task"
            root.mkdir(parents=True)
            write_public_task(root)

            with self.assertRaises(TaskPackageError):
                load_public_task(root)

    def test_rejects_tcl_metacharacters_in_public_metadata(self) -> None:
        cases = {
            "top": ('top = "kernel"', 'top = "kernel\\nsource hidden/answer.tcl"'),
            "kernel_file": (
                'kernel_file = "kernel.cpp"',
                'kernel_file = "kernel.cpp}\\nsource hidden/answer.tcl"',
            ),
        }
        for field, (original, malicious) in cases.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_public_task(root)
                task_toml = root / "task.toml"
                task_toml.write_text(
                    task_toml.read_text(encoding="utf-8").replace(original, malicious),
                    encoding="utf-8",
                )

                with self.assertRaises(TaskPackageError):
                    load_public_task(root)


class V2FixtureTests(unittest.TestCase):
    def test_v2_fixture_is_self_contained_optimize_task(self) -> None:
        examples = Path(__file__).parents[1] / "examples"

        task = load_public_task(examples / "u55c_v2_optimize_task")

        self.assertEqual(task.task_type, "optimize")
        self.assertEqual(task.top, "vector_add")
        self.assertEqual(task.budget, 160)
        self.assertTrue(task.requires_cosim)
        self.assertEqual(task.part, "xcu55c-fsvh2892-2L-e")
        self.assertEqual(task.clock_ns, 10.0)
        self.assertIn("VECTOR_SIZE = 256", task.headers["kernel.h"].decode())
        self.assertIn("PIPELINE II=16", task.kernel_code)
        self.assertFalse(
            {"reference", "hidden"}.intersection(
                part.casefold()
                for name in task.public_file_hashes
                for part in Path(name).parts
            )
        )


if __name__ == "__main__":
    unittest.main()
