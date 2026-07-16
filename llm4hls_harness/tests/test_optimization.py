from __future__ import annotations

import unittest

from llm4hls_agent.optimization import (
    ALLOWED_OPTIMIZATIONS,
    select_optimization,
)


class OptimizationSelectorTests(unittest.TestCase):
    @staticmethod
    def metrics(
        *, interval: int, latency: int, evidence: list[str] | None = None
    ) -> dict[str, object]:
        return {
            "latency": {"best": latency, "average": latency, "worst": latency},
            "interval": {"min": interval, "max": interval},
            "evidence": evidence or [],
        }

    def test_high_interval_selects_pipeline_even_with_conservative_pragma(self) -> None:
        source = (
            "for (int i = 0; i < 256; ++i) {\n"
            "#pragma HLS PIPELINE II=16\n"
            "c[i] = a[i] + b[i];\n}"
        )

        decision = select_optimization(
            self.metrics(interval=16, latency=4098),
            source=source,
            attempted=(),
            failures=(),
        )

        self.assertEqual(decision.optimization_class, "LOOP_PIPELINE")
        self.assertEqual(decision.stop_reason, None)
        self.assertTrue(decision.metrics_digest)

    def test_high_latency_after_pipeline_selects_unroll(self) -> None:
        source = (
            "for (int i = 0; i < 256; ++i) {\n"
            "#pragma HLS PIPELINE II=1\n"
            "c[i] = a[i] + b[i];\n}"
        )

        decision = select_optimization(
            self.metrics(interval=1, latency=260),
            source=source,
            attempted=("LOOP_PIPELINE",),
            failures=(),
        )

        self.assertEqual(decision.optimization_class, "LOOP_UNROLL")

    def test_memory_port_evidence_selects_memory_layout(self) -> None:
        decision = select_optimization(
            self.metrics(
                interval=4,
                latency=300,
                evidence=["Unable to schedule load operation due to limited memory ports"],
            ),
            source="for (int i = 0; i < 256; ++i) {}",
            attempted=("LOOP_PIPELINE", "LOOP_UNROLL"),
            failures=(),
        )

        self.assertEqual(decision.optimization_class, "MEMORY_LAYOUT")

    def test_no_new_evidence_does_not_repeat_failed_class(self) -> None:
        metrics = self.metrics(interval=16, latency=4098)
        first = select_optimization(
            metrics,
            source="for (int i = 0; i < 256; ++i) {}",
            attempted=(),
            failures=(),
        )
        second = select_optimization(
            metrics,
            source="for (int i = 0; i < 256; ++i) {}",
            attempted=("LOOP_PIPELINE",),
            failures=(("LOOP_PIPELINE", first.metrics_digest),),
        )

        self.assertNotEqual(second.optimization_class, "LOOP_PIPELINE")

    def test_exhausted_whitelist_stops_explicitly(self) -> None:
        decision = select_optimization(
            self.metrics(interval=1, latency=1),
            source="for (int i = 0; i < 1; ++i) {}",
            attempted=ALLOWED_OPTIMIZATIONS,
            failures=(),
        )

        self.assertIsNone(decision.optimization_class)
        self.assertEqual(decision.stop_reason, "NO_DISTINCT_OPTIMIZATION")


if __name__ == "__main__":
    unittest.main()
