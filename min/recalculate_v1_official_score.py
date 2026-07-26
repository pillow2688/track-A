#!/usr/bin/env python3
"""Recalculate V1 public evidence with the official scoring.py formula.

The result remains a proxy because official grading uses a hidden functional
testbench. Clock timing and resource use are retained only as engineering
metrics and never enter the score calculation.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import statistics
import tomllib
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ACCEPTANCE_ROOT = (
    PROJECT_ROOT / "min" / "benchmarks" / "public_3x10V1"
)
DEFAULT_OFFICIAL_SCORING = Path("/home/ying/下载/scoring.py")
TASK_ROOT = (
    PROJECT_ROOT
    / "llm4hls_harness"
    / "task_corpus"
    / "official"
    / "fpt26-harness-public"
)
TASKS = (
    "projection_bugfix",
    "dotProduct_optimize",
    "residual_stream_deadlock",
)
TARGET_CLOCK_NS = 5.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--acceptance-root",
        type=Path,
        default=DEFAULT_ACCEPTANCE_ROOT,
    )
    parser.add_argument(
        "--official-scoring",
        type=Path,
        default=DEFAULT_OFFICIAL_SCORING,
    )
    return parser


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _exclusive_json(path: Path, value: object) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _exclusive_text(path: Path, value: str) -> None:
    payload = value.encode("utf-8")
    if not payload.endswith(b"\n"):
        payload += b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _official_cap(path: Path) -> float:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(
            isinstance(target, ast.Name) and target.id == "ACCEL_CAP"
            for target in targets
        ):
            continue
        value_node = node.value
        value = ast.literal_eval(value_node)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            break
        return float(value)
    raise RuntimeError(f"ACCEL_CAP not found in {path}")


def _task_metadata(task_id: str) -> dict[str, Any]:
    path = TASK_ROOT / task_id / "task.toml"
    with path.open("rb") as handle:
        value = tomllib.load(handle)
    difficulty = value.get("difficulty")
    if isinstance(difficulty, bool) or not isinstance(difficulty, int):
        raise RuntimeError(f"{task_id}: invalid difficulty")
    target = value.get("target")
    if not isinstance(target, Mapping):
        raise RuntimeError(f"{task_id}: invalid target")
    return {
        "difficulty": difficulty,
        "requires_cosim": value.get("requires_cosim") is True,
        "clock_ns": float(target["clock_ns"]),
        "task_toml": str(path.resolve()),
        "task_toml_sha256": _sha256(path),
    }


def _stage(outcome: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    validation = outcome.get("validation")
    if not isinstance(validation, Mapping):
        return {}
    value = validation.get(name)
    return value if isinstance(value, Mapping) else {}


def _action(run_dir: Path, stage: Mapping[str, Any]) -> dict[str, Any] | None:
    ref = stage.get("result_ref")
    if not isinstance(ref, str):
        return None
    value = _load(run_dir / ref)
    return value if isinstance(value, dict) else None


def _official_latency(action: Mapping[str, Any] | None) -> int | float | None:
    if action is None:
        return None
    report = action.get("report")
    if not isinstance(report, Mapping):
        return None
    latency = report.get("latency")
    if not isinstance(latency, Mapping):
        return None
    worst = latency.get("worst")
    if isinstance(worst, (int, float)) and not isinstance(worst, bool):
        return worst
    average = latency.get("average")
    if isinstance(average, (int, float)) and not isinstance(average, bool):
        return average
    return None


def _engineering_metrics(action: Mapping[str, Any]) -> dict[str, Any]:
    report = action.get("report")
    report = report if isinstance(report, Mapping) else {}
    clock = report.get("estimated_clock_period_ns")
    resources = report.get("resources")
    available = report.get("available_resources")
    resources = resources if isinstance(resources, Mapping) else {}
    available = available if isinstance(available, Mapping) else {}
    utilization: dict[str, float | None] = {}
    for name in ("LUT", "FF", "DSP", "BRAM_18K", "URAM"):
        used = resources.get(name)
        limit = available.get(name)
        utilization[name] = (
            round(100.0 * float(used) / float(limit), 8)
            if isinstance(used, (int, float))
            and not isinstance(used, bool)
            and isinstance(limit, (int, float))
            and not isinstance(limit, bool)
            and float(limit) > 0
            else None
        )
    return {
        "estimated_clock_period_ns": clock,
        "target_clock_ns": TARGET_CLOCK_NS,
        "meets_target_clock": (
            isinstance(clock, (int, float))
            and not isinstance(clock, bool)
            and float(clock) <= TARGET_CLOCK_NS
        ),
        "resources": dict(resources),
        "available_resources": dict(available),
        "resource_utilization_percent": utilization,
        "excluded_from_official_score": True,
    }


def _score(
    *,
    difficulty: int,
    functional_pass: bool,
    synth_pass: bool,
    baseline_latency: int | float | None,
    candidate_latency: int | float | None,
    cap: float,
) -> dict[str, Any]:
    acceleration: float | None = None
    if (
        functional_pass
        and synth_pass
        and candidate_latency
        and baseline_latency
    ):
        acceleration = float(baseline_latency) / float(candidate_latency)
    is_opt = acceleration is not None and acceleration > 1.0
    ppa_norm = min(acceleration, cap) / cap if acceleration else 0.0
    correctness_component = 0.0
    synth_component = 0.0
    ppa_component = 0.0
    if functional_pass:
        correctness_component = difficulty * 0.5
        synth_component = difficulty * 0.2 * (1.0 if synth_pass else 0.0)
        ppa_component = difficulty * 0.3 * ppa_norm
        score = correctness_component + synth_component + ppa_component
    else:
        score = 0.0
    return {
        "baseline_latency_cycles": baseline_latency,
        "candidate_latency_cycles": candidate_latency,
        "acceleration": acceleration,
        "capped_acceleration": (
            min(acceleration, cap) if acceleration is not None else None
        ),
        "ppa_norm": ppa_norm,
        "is_opt": is_opt,
        "components": {
            "correctness": round(correctness_component, 10),
            "synthesizable": round(synth_component, 10),
            "ppa": round(ppa_component, 10),
        },
        "official_score_proxy": round(score, 4),
    }


def _record(
    *,
    formal_record: Mapping[str, Any],
    cap: float,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    run_dir = Path(str(formal_record["run_dir"]))
    result = _load(run_dir / "minimal_result.json")
    if not isinstance(result, Mapping):
        raise RuntimeError(f"{run_dir}: invalid result")
    selected_id = result.get("selected_candidate_id")
    selected_key = "candidate" if selected_id == "candidate_001" else "baseline"
    selected = result.get(selected_key)
    baseline = result.get("baseline")
    if not isinstance(selected, Mapping) or not isinstance(baseline, Mapping):
        raise RuntimeError(f"{run_dir}: selected/baseline outcome missing")

    selected_synth = _action(run_dir, _stage(selected, "synth"))
    baseline_synth = _action(run_dir, _stage(baseline, "synth"))
    if selected_synth is None:
        raise RuntimeError(f"{run_dir}: selected Synth result missing")
    candidate_latency = _official_latency(selected_synth)
    baseline_latency = _official_latency(baseline_synth)
    if selected_id == "candidate_000" and baseline_latency is not None:
        candidate_latency = baseline_latency

    requires_cosim = metadata["requires_cosim"] is True
    cosim_stage = _stage(selected, "cosim")
    cosim_pass: bool | None = (
        cosim_stage.get("status") == "PASS" if requires_cosim else None
    )
    functional_pass = bool(
        selected.get("functional_pass") is True
        and (cosim_pass is not False)
    )
    synth_pass = selected_synth.get("ok") is True
    calculated = _score(
        difficulty=int(metadata["difficulty"]),
        functional_pass=functional_pass,
        synth_pass=synth_pass,
        baseline_latency=baseline_latency,
        candidate_latency=candidate_latency,
        cap=cap,
    )
    stored = result.get("selected_public_score_proxy")
    matches = (
        isinstance(stored, (int, float))
        and not isinstance(stored, bool)
        and math.isclose(
            float(stored),
            float(calculated["official_score_proxy"]),
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    )
    return {
        "slot_index": formal_record["slot_index"],
        "task_id": formal_record["task_id"],
        "repetition": formal_record["repetition"],
        "run_dir": str(run_dir.resolve()),
        "selected_candidate_id": selected_id,
        "baseline_fallback": selected_id == "candidate_000",
        "difficulty": metadata["difficulty"],
        "public_functional_proxy_pass": functional_pass,
        "selected_synth_pass": synth_pass,
        "selected_cosim_pass": cosim_pass,
        **calculated,
        "engineering_metrics": _engineering_metrics(selected_synth),
        "stored_selected_public_score_proxy": stored,
        "stored_proxy_matches_official_formula": matches,
        "hidden_correctness_caveat": (
            "If official hidden correctness fails, the actual official score "
            "is zero regardless of this public-evidence proxy."
        ),
    }


def _task_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [float(record["official_score_proxy"]) for record in records]
    accelerations = [
        float(record["acceleration"])
        for record in records
        if record["acceleration"] is not None
    ]
    return {
        "runs": len(records),
        "score_sum_over_repeats": round(sum(scores), 4),
        "score_mean": round(statistics.fmean(scores), 4),
        "score_min": min(scores),
        "score_max": max(scores),
        "baseline_fallback_runs": sum(
            record["baseline_fallback"] is True for record in records
        ),
        "is_opt_runs": sum(record["is_opt"] is True for record in records),
        "full_ppa_runs": sum(
            math.isclose(float(record["ppa_norm"]), 1.0)
            for record in records
        ),
        "known_acceleration_runs": len(accelerations),
        "acceleration_mean_when_known": (
            round(statistics.fmean(accelerations), 8)
            if accelerations
            else None
        ),
        "target_clock_met_runs": sum(
            record["engineering_metrics"]["meets_target_clock"] is True
            for record in records
        ),
        "clock_or_resources_used_in_score": False,
    }


def _report(result: Mapping[str, Any]) -> str:
    summary = result["summary"]
    dot_records = [
        record
        for record in result["records"]
        if record["task_id"] == "dotProduct_optimize"
    ]
    dot_rows = "\n".join(
        "| {rep:02d} | {selected} | {base} | {candidate} | {accel} | "
        "{ppa:.4f} | {score:.4f} | {clock:.3f} | {clock_ok} |".format(
            rep=record["repetition"],
            selected=record["selected_candidate_id"],
            base=record["baseline_latency_cycles"],
            candidate=record["candidate_latency_cycles"],
            accel=(
                f"{record['acceleration']:.6f}"
                if record["acceleration"] is not None
                else "null"
            ),
            ppa=record["ppa_norm"],
            score=record["official_score_proxy"],
            clock=record["engineering_metrics"]["estimated_clock_period_ns"],
            clock_ok=(
                "是"
                if record["engineering_metrics"]["meets_target_clock"]
                else "否"
            ),
        )
        for record in dot_records
    )
    per_task = summary["per_task"]
    return f"""# V1 官方 Score Proxy 重新计算

## 结论

以 `/home/ying/下载/scoring.py`（SHA-256
`{result['official_scoring']['sha256']}`）为唯一公式来源重新计算后：

| 任务 | difficulty | 10 次分数合计 | 平均 Score Proxy | fallback 次数 | 满 PPA 次数 |
|---|---:|---:|---:|---:|---:|
| projection_bugfix | 2 | {per_task['projection_bugfix']['score_sum_over_repeats']:.4f} | {per_task['projection_bugfix']['score_mean']:.4f} | {per_task['projection_bugfix']['baseline_fallback_runs']} | {per_task['projection_bugfix']['full_ppa_runs']} |
| dotProduct_optimize | 3 | {per_task['dotProduct_optimize']['score_sum_over_repeats']:.4f} | {per_task['dotProduct_optimize']['score_mean']:.4f} | {per_task['dotProduct_optimize']['baseline_fallback_runs']} | {per_task['dotProduct_optimize']['full_ppa_runs']} |
| residual_stream_deadlock | 4 | {per_task['residual_stream_deadlock']['score_sum_over_repeats']:.4f} | {per_task['residual_stream_deadlock']['score_mean']:.4f} | {per_task['residual_stream_deadlock']['baseline_fallback_runs']} | {per_task['residual_stream_deadlock']['full_ppa_runs']} |

- 每个 repetition 的三任务合计平均：**{summary['mean_three_task_score_per_repetition']:.4f} / 9.0000**
- 30 个实验 Slot 的均值：**{summary['mean_score_per_slot']:.4f}**
- 30 个 Slot 分数合计：**{summary['score_sum_over_30_slots']:.4f}**
- 30/30 与 V1 已存 `selected_public_score_proxy` 完全一致。需要纠正的是
  对分数的解释，不是这 30 个已存数值。

这仍是 **Score Proxy**：V1 只有公开验证证据。正式服务器若 hidden
functional（以及必要的 cosim）失败，该任务实际官方分数为 0。

## 官方公式

功能通过时：

```text
acceleration = baseline_latency_cycles / candidate_latency_cycles
ppa_norm = min(acceleration, 8) / 8
score = difficulty × (0.5 + 0.2 × synth_pass + 0.3 × ppa_norm)
```

功能失败时分数严格为 0。`is_opt` 只判断 acceleration 是否严格大于 1，
不充当计分 Gate。

因此：

- baseline fallback 且 acceleration=1：
  `score = difficulty × 0.7375`，DotProduct 为 **2.2125**，不是 0。
- `0 < acceleration ≤ 1` 仍有 PPA 分：
  `score = difficulty × (0.7 + 0.0375 × acceleration)`。
- latency 为 0 或缺失时 acceleration 为 null、PPA 为 0；只要功能和 Synth
  通过仍得 `difficulty × 0.7`。Projection 的 latency=0，因此为 **1.4000**。

## DotProduct 逐次结果

| Run | 最终选择 | baseline cycles | candidate cycles | acceleration | ppa_norm | 官方 Score Proxy | clock ns（工程） | ≤5ns |
|---|---|---:|---:|---:|---:|---:|---:|:---:|
{dot_rows}

run_07 和 run_09：

```text
acceleration = 1027 / 35 = 29.342857×
capped acceleration = 8×
ppa_norm = 1
score = 3 × (0.5 + 0.2 + 0.3) = 3.0000
```

两次估算 clock 都是 31.133 ns，但官方 `scoring.py` 不读取 clock，
因此不会扣分。只要服务器 hidden correctness 和 Synth PASS，二者就是
DotProduct 满分。

## 工程指标与官方分数的边界

- clock、LUT、FF、DSP、BRAM、URAM 已逐 run 保留在 JSON 的
  `engineering_metrics`。
- 28/30 个最终选择满足 5 ns；不满足的是 DotProduct run_07/run_09。
- 所有资源均在报告的器件可用量内。
- 这些指标可用于工程决策和风险提示，但没有进入本次官方 Score Proxy 的
  correctness、synthesizable 或 cycle-acceleration 三个计分项。

## 对原 V1 汇总的更正

原报告中的平均 Score Proxy：

- Projection 1.4000
- DotProduct 2.4038
- Residual 3.0978
- Overall per-slot 2.3005

数值本身与官方公式一致。需要废止的是“只有 objective success 或
hardware-qualified 才有官方分”的解释：6 次 DotProduct baseline fallback
共贡献 **13.2750** 分；31.133 ns 的两次各贡献 **3.0000**。
"""


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.acceptance_root.resolve()
    scoring_path = args.official_scoring.resolve()
    output_json = root / "official_score_proxy_recalculation.json"
    output_report = root / "official_score_proxy_recalculation.md"
    for path in (output_json, output_report):
        if path.exists():
            raise RuntimeError(f"refusing to overwrite evidence: {path}")

    cap = _official_cap(scoring_path)
    if not math.isclose(cap, 8.0, rel_tol=0.0, abs_tol=0.0):
        raise RuntimeError(f"unexpected official acceleration cap: {cap}")
    formal_records_path = root / "formal" / "formal_records.json"
    formal_records = _load(formal_records_path)
    if not isinstance(formal_records, list) or len(formal_records) != 30:
        raise RuntimeError("expected exactly 30 formal records")

    metadata = {task: _task_metadata(task) for task in TASKS}
    records = [
        _record(
            formal_record=record,
            cap=cap,
            metadata=metadata[str(record["task_id"])],
        )
        for record in formal_records
    ]
    if not all(
        record["stored_proxy_matches_official_formula"] is True
        for record in records
    ):
        raise RuntimeError("stored V1 score proxy disagrees with official formula")

    per_task = {
        task: _task_summary(
            [record for record in records if record["task_id"] == task]
        )
        for task in TASKS
    }
    repetition_scores = {
        str(repetition): round(
            sum(
                float(record["official_score_proxy"])
                for record in records
                if record["repetition"] == repetition
            ),
            4,
        )
        for repetition in range(1, 11)
    }
    all_scores = [
        float(record["official_score_proxy"]) for record in records
    ]
    score_sum = round(sum(all_scores), 4)
    result = {
        "schema_version": "track-a.v1-official-score-proxy-recalculation.v1",
        "official_scoring": {
            "path": str(scoring_path),
            "sha256": _sha256(scoring_path),
            "acceleration_cap": cap,
            "score_formula": (
                "difficulty * (0.5*correct + 0.2*synth_pass "
                "+ 0.3*min(acceleration,8)/8)"
            ),
            "clock_or_resource_terms": False,
        },
        "source_evidence": {
            "formal_records": str(formal_records_path.resolve()),
            "formal_records_sha256": _sha256(formal_records_path),
            "task_metadata": metadata,
        },
        "proxy_scope": {
            "uses_public_functional_and_cosim_evidence": True,
            "hidden_correctness_was_not_run": True,
            "actual_official_score_is_zero_if_hidden_correctness_fails": True,
        },
        "summary": {
            "per_task": per_task,
            "score_sum_over_30_slots": score_sum,
            "mean_score_per_slot": round(statistics.fmean(all_scores), 4),
            "three_task_score_by_repetition": repetition_scores,
            "mean_three_task_score_per_repetition": round(
                statistics.fmean(repetition_scores.values()), 4
            ),
            "maximum_three_task_score": sum(
                int(metadata[task]["difficulty"]) for task in TASKS
            ),
            "stored_proxy_matches": sum(
                record["stored_proxy_matches_official_formula"] is True
                for record in records
            ),
            "selected_synth_passes": sum(
                record["selected_synth_pass"] is True for record in records
            ),
            "engineering_target_clock_passes": sum(
                record["engineering_metrics"]["meets_target_clock"] is True
                for record in records
            ),
            "baseline_fallback_score_sum": round(
                sum(
                    float(record["official_score_proxy"])
                    for record in records
                    if record["baseline_fallback"] is True
                ),
                4,
            ),
            "clock_and_resources_excluded_from_score": True,
        },
        "records": records,
    }
    _exclusive_json(output_json, result)
    _exclusive_text(output_report, _report(result))
    print(
        json.dumps(
            {
                "status": "PASS",
                "json": str(output_json),
                "report": str(output_report),
                "score_sum": score_sum,
                "mean_per_slot": result["summary"]["mean_score_per_slot"],
                "mean_three_task": result["summary"][
                    "mean_three_task_score_per_repetition"
                ],
                "stored_matches": result["summary"]["stored_proxy_matches"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
