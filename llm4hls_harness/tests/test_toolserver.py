from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

try:
    from llm4hls_agent.budget import BudgetConfig, BudgetExceeded, BudgetLedger
    from llm4hls_agent.task import load_public_task
    from llm4hls_agent.tools import (
        AmbiguousActionError,
        BackendResult,
        ToolArtifactError,
        ToolConfig,
        ToolServer,
    )
except ModuleNotFoundError:
    BudgetExceeded = RuntimeError
    AmbiguousActionError = ToolArtifactError = RuntimeError

    def _missing(*_args: object, **_kwargs: object):
        raise AssertionError("metered ToolServer is not implemented")

    BudgetConfig = BudgetLedger = ToolConfig = ToolServer = BackendResult = _missing  # type: ignore[misc,assignment]
    load_public_task = _missing


def make_task(root: Path) -> None:
    (root / "task.toml").write_text(
        "\n".join(
            [
                'task_id = "fixture"',
                'task_type = "optimize"',
                'top = "kernel"',
                'kernel_file = "kernel.cpp"',
                'header_files = ["kernel.h"]',
                'public_tb = "kernel_tb.cpp"',
                "budget = 40",
                "[target]",
                'part = "xcu55c-fsvh2892-2L-e"',
                "clock_ns = 5.0",
            ]
        ),
        encoding="utf-8",
    )
    (root / "kernel.cpp").write_text('#include "kernel.h"\nvoid kernel() {}\n', encoding="utf-8")
    (root / "kernel.h").write_text("void kernel();\n", encoding="utf-8")
    (root / "kernel_tb.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, kind: str, **_kwargs: object):
        self.calls.append(kind)
        return BackendResult(
            ok=True,
            phase="pass",
            return_code=0,
            elapsed_s=0.25,
            evidence=["fake tool completed"],
        )


class AlternateBackend(FakeBackend):
    pass


class ArtifactBackend(FakeBackend):
    def run(self, kind: str, **kwargs: object):
        work_dir = kwargs["work_dir"]
        assert isinstance(work_dir, Path)
        work_dir.mkdir(parents=True, exist_ok=False)
        (work_dir / "proof.txt").write_text("auditable evidence\n", encoding="utf-8")
        result = super().run(kind, **kwargs)
        return BackendResult(
            ok=result.ok,
            phase=result.phase,
            return_code=result.return_code,
            elapsed_s=result.elapsed_s,
            evidence=result.evidence,
            artifacts={"proof": "work/proof.txt"},
        )


class InterruptingBackend(FakeBackend):
    def run(self, kind: str, **_kwargs: object):
        self.calls.append(kind)
        raise KeyboardInterrupt("simulated hard interruption")


class TimeoutCaptureBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.effective_timeouts: list[float] = []

    def run(self, kind: str, **kwargs: object):
        config = kwargs["config"]
        assert isinstance(config, ToolConfig)
        self.effective_timeouts.append(config.timeout_for(kind))
        return super().run(kind, **kwargs)


class MeteredToolServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.task_dir = self.root / "task"
        self.task_dir.mkdir()
        make_task(self.task_dir)
        self.task = load_public_task(self.task_dir)
        self.run_dir = self.root / "run"
        self.backend = FakeBackend()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_server(
        self,
        *,
        total: int = 40,
        backend=None,
        runtime_limit_seconds: float = 3600.0,
    ):
        budget = BudgetLedger(
            self.run_dir / "budget_ledger.jsonl",
            BudgetConfig(
                credit_limit=total,
                costs={"csim": 1, "synth": 4, "cosim": 20},
                tool_limits={"csim": None, "synth": None, "cosim": None},
                token_limit=32768,
                runtime_limit_seconds=runtime_limit_seconds,
            ),
        )
        config = ToolConfig(
            vitis_root="/opt/xilinx/2025.2/Vitis",
            part=self.task.part,
            clock_ns=self.task.clock_ns,
            timeouts={"csim": 180.0, "synth": 600.0, "cosim": 90.0},
        )
        server = ToolServer(
            task=self.task,
            budget=budget,
            run_root=self.run_dir,
            config=config,
            backend=backend or self.backend,
        )
        return budget, server

    def test_completed_action_is_cached_without_second_call_or_charge(self) -> None:
        budget, server = self.make_server()

        first = server.csim(self.task.kernel_code, candidate_id="candidate_000")
        ledger_after_first = (self.run_dir / "budget_ledger.jsonl").read_text(
            encoding="utf-8"
        )
        second = server.csim(self.task.kernel_code, candidate_id="candidate_000")

        self.assertEqual(self.backend.calls, ["csim"])
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(second.action_id, first.action_id)
        self.assertEqual(len(first.action_id), 64)
        self.assertEqual(second.code_hash, self.task.kernel_sha256)
        self.assertEqual(second.tool_config_hash, first.tool_config_hash)
        self.assertEqual(budget.snapshot()["credits_used"], 1)
        self.assertEqual(
            (self.run_dir / "budget_ledger.jsonl").read_text(encoding="utf-8"),
            ledger_after_first,
        )

        events = [
            json.loads(line)
            for line in ledger_after_first.splitlines()
            if line.strip()
        ]
        self.assertEqual([event["state"] for event in events], ["INITIALIZED", "STARTED", "COMPLETED"])
        self.assertTrue((self.run_dir / first.result_ref).is_file())
        trace_events = [
            json.loads(line)["event"]
            for line in (self.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(trace_events[-1], "TOOL_CACHE_HIT")

    def test_budget_denial_happens_before_backend_execution(self) -> None:
        budget, server = self.make_server(total=1)

        with self.assertRaises(BudgetExceeded):
            server.synth(self.task.kernel_code, candidate_id="candidate_000")

        self.assertEqual(self.backend.calls, [])
        self.assertEqual(budget.snapshot()["credits_used"], 0)
        states = [
            json.loads(line)["state"]
            for line in (self.run_dir / "budget_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(states, ["INITIALIZED"])

    def test_tool_config_copies_and_freezes_timeout_mapping(self) -> None:
        timeouts = {"csim": 1.0, "synth": 2.0, "cosim": 3.0}
        config = ToolConfig(
            vitis_root="/opt/xilinx/2025.2/Vitis",
            part="xcu55c-fsvh2892-2L-e",
            clock_ns=5.0,
            timeouts=timeouts,
        )
        original_hash = config.hash_for("csim")

        timeouts["csim"] = 999.0

        self.assertEqual(config.timeout_for("csim"), 1.0)
        self.assertEqual(config.hash_for("csim"), original_hash)
        with self.assertRaises(TypeError):
            config.timeouts["csim"] = 4.0  # type: ignore[index]

    def test_tool_config_rejects_non_finite_clock_and_timeouts(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(clock=value), self.assertRaises(ValueError):
                ToolConfig(
                    vitis_root="/opt/xilinx/2025.2/Vitis",
                    part="part",
                    clock_ns=value,
                    timeouts={"csim": 1.0, "synth": 2.0, "cosim": 3.0},
                )
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                ToolConfig(
                    vitis_root="/opt/xilinx/2025.2/Vitis",
                    part="part",
                    clock_ns=5.0,
                    timeouts={"csim": value, "synth": 2.0, "cosim": 3.0},
                )

    def test_tool_config_rejects_tcl_metacharacters(self) -> None:
        for field, value in (
            ("part", "valid-part\nsource hidden/answer.tcl"),
            ("flow_target", "vivado; source hidden/answer.tcl"),
        ):
            values = {
                "vitis_root": "/opt/xilinx/2025.2/Vitis",
                "part": "valid-part",
                "clock_ns": 5.0,
                "timeouts": {"csim": 1.0, "synth": 2.0, "cosim": 3.0},
                "flow_target": "vivado",
            }
            values[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                ToolConfig(**values)

    def test_cache_hit_repairs_missing_derived_budget_snapshot(self) -> None:
        _, server = self.make_server()
        server.csim(self.task.kernel_bytes)
        state = self.run_dir / "budget_state.json"
        state.unlink()

        cached = server.csim(self.task.kernel_bytes)

        self.assertTrue(cached.cached)
        self.assertTrue(state.is_file())
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["credits_used"], 1)

    def test_cache_key_binds_public_headers_and_test_fixture(self) -> None:
        _, first_server = self.make_server()
        first = first_server.csim(self.task.kernel_bytes)

        second_dir = self.root / "second-task"
        second_dir.mkdir()
        make_task(second_dir)
        (second_dir / "kernel.h").write_text(
            "// different public fixture\nvoid kernel();\n", encoding="utf-8"
        )
        second_task = load_public_task(second_dir)
        budget = BudgetLedger(
            self.run_dir / "budget_ledger.jsonl",
            BudgetConfig(
                credit_limit=40,
                costs={"csim": 1, "synth": 4, "cosim": 20},
                tool_limits={"csim": None, "synth": None, "cosim": None},
                token_limit=32768,
                runtime_limit_seconds=3600.0,
            ),
        )
        second_server = ToolServer(
            task=second_task,
            budget=budget,
            run_root=self.run_dir,
            config=first_server.config,
            backend=self.backend,
        )

        second = second_server.csim(second_task.kernel_bytes)

        self.assertFalse(second.cached)
        self.assertNotEqual(first.action_id, second.action_id)
        self.assertEqual(self.backend.calls, ["csim", "csim"])

    def test_backend_identity_is_bound_into_action_cache_key(self) -> None:
        first_backend = FakeBackend()
        _, first_server = self.make_server(backend=first_backend)
        first = first_server.csim(self.task.kernel_bytes)

        second_backend = AlternateBackend()
        budget, second_server = self.make_server(backend=second_backend)
        second = second_server.csim(self.task.kernel_bytes)

        self.assertFalse(second.cached)
        self.assertNotEqual(first.action_id, second.action_id)
        self.assertEqual(first_backend.calls, ["csim"])
        self.assertEqual(second_backend.calls, ["csim"])
        self.assertEqual(budget.snapshot()["credits_used"], 2)

    def test_tampered_result_json_is_not_accepted_as_a_cache_hit(self) -> None:
        _, server = self.make_server()
        first = server.csim(self.task.kernel_bytes)
        path = self.run_dir / first.result_ref
        value = json.loads(path.read_text(encoding="utf-8"))
        value["ok"] = False
        value["phase"] = "runtime_fail"
        path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaises(ToolArtifactError):
            server.csim(self.task.kernel_bytes)

    def test_missing_or_changed_backend_artifact_invalidates_cache(self) -> None:
        backend = ArtifactBackend()
        _, server = self.make_server(backend=backend)
        first = server.synth(self.task.kernel_bytes)
        proof = (self.run_dir / first.result_ref).parent / first.artifacts["proof"]
        proof.unlink()

        with self.assertRaises(ToolArtifactError):
            server.synth(self.task.kernel_bytes)

    def test_remaining_runtime_caps_the_effective_tool_timeout(self) -> None:
        backend = TimeoutCaptureBackend()
        _, server = self.make_server(
            backend=backend,
            runtime_limit_seconds=1.0,
        )

        result = server.csim(self.task.kernel_bytes)

        self.assertEqual(len(backend.effective_timeouts), 1)
        self.assertGreater(backend.effective_timeouts[0], 0.0)
        self.assertLess(backend.effective_timeouts[0], 1.0)
        self.assertTrue(
            math.isclose(
                result.effective_timeout_seconds,
                backend.effective_timeouts[0],
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        )

    def test_started_without_result_becomes_stably_ambiguous_once(self) -> None:
        backend = InterruptingBackend()
        budget, server = self.make_server(backend=backend)
        with self.assertRaises(KeyboardInterrupt):
            server.csim(self.task.kernel_bytes)

        _, recovering = self.make_server(backend=backend)
        with self.assertRaises(AmbiguousActionError):
            recovering.csim(self.task.kernel_bytes)
        ledger_after_mark = (self.run_dir / "budget_ledger.jsonl").read_bytes()

        _, repeated = self.make_server(backend=backend)
        with self.assertRaises(AmbiguousActionError):
            repeated.csim(self.task.kernel_bytes)

        self.assertEqual(backend.calls, ["csim"])
        self.assertEqual(
            (self.run_dir / "budget_ledger.jsonl").read_bytes(), ledger_after_mark
        )
        self.assertEqual(budget.snapshot()["credits_used"], 1)


if __name__ == "__main__":
    unittest.main()
