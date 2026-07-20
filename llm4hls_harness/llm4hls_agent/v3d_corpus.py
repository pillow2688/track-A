"""Deterministic generator and validators for the V3-D fast task corpus.

The corpus is development evidence, not a hidden benchmark.  Runtime task
loading remains public-only: ``hidden_like/`` and ``golden/`` are generated for
offline regression and are never returned by :func:`load_public_task`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


CORPUS_SCHEMA_VERSION = "v3d.fast-corpus.v1"
MUTATION_SCHEMA_VERSION = "v3d.mutation-manifest.v1"
ACCEPTANCE_SCHEMA_VERSION = "v3d.acceptance.v1"
MUTATION_ENGINE_VERSION = "v3d.mutation-engine.v1"
GENERATOR_VERSION = "v3d.fast-corpus-generator.v1"

PHASE_MODES = ("REPAIR", "SYNTH_FIX", "STRUCTURAL_FIX", "OPTIMIZE")
FAILING_GATES = ("csim", "synth", "cosim", "ppa")
VALIDATION_STATES = ("PASS", "FAIL", "NOT_RUN")
PLANNER_FORBIDDEN_COMPONENTS = (
    "answer",
    "golden",
    "hidden",
    "hidden_like",
    "reference",
)
PLANNER_VISIBLE_PATHS = (
    "task.toml",
    "description.md",
    "kernel.cpp",
    "kernel.h",
    "kernel_tb.cpp",
)

_BEGIN_MARKER = "// V3D_MUTATION_BEGIN"
_END_MARKER = "// V3D_MUTATION_END"


class V3DCorpusError(ValueError):
    """Raised when a generated task or corpus artifact violates the schema."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_opaque_task_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    suffix = value.removeprefix("v3d_fast_")
    return len(suffix) == 3 and suffix.isdigit()


@dataclass(frozen=True)
class MutationResult:
    """One hash-bound deterministic source mutation."""

    source: str
    base_sha256: str
    mutated_sha256: str
    base_range_sha256: str
    base_start_line: int
    base_end_line: int
    mutated_start_line: int
    mutated_end_line: int


@dataclass(frozen=True)
class _MutationOperator:
    name: str
    mode: str
    failing_gate: str
    expected_region: str
    render: Callable[[random.Random, int], str]
    version: int = 1


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    mode: str
    operator: str
    seed: int
    family: str
    difficulty: int
    mutation_summary_zh: str
    require_optimize_cosim: bool = False

    @property
    def failing_gate(self) -> str:
        return {
            "REPAIR": "csim",
            "SYNTH_FIX": "synth",
            "STRUCTURAL_FIX": "cosim",
            "OPTIMIZE": "ppa",
        }[self.mode]

    @property
    def task_type(self) -> str:
        # PhaseRouter derives the mode from observed validation, not from this
        # public hint.  A mode-neutral value avoids leaking evaluator labels.
        return "generate"

    @property
    def requires_cosim(self) -> bool:
        return self.mode == "STRUCTURAL_FIX" or self.require_optimize_cosim


_COMMON_REGION = """    for (int i = 0; i < V3D_SIZE; ++i) {
        output[i] = input[i] * 3 + 7;
    }
"""

_SYNTH_REGION = """void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
    for (int i = 0; i < V3D_SIZE; ++i) {
        output[i] = input[i] * 3 + 7;
    }
}
"""

_STRUCTURAL_REGION = """static void v3d_produce(
    const int input[V3D_SIZE],
    hls::stream<int>& main_stream,
    hls::stream<int>& side_stream) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        main_stream.write(input[i]);
        side_stream.write(input[i] + 1);
    }
}
"""

_OPTIMIZE_REGION = """#pragma HLS ARRAY_PARTITION variable=input cyclic factor=4 dim=1
#pragma HLS ARRAY_PARTITION variable=output cyclic factor=4 dim=1
v3d_map:
    for (int i = 0; i < V3D_SIZE; ++i) {
#pragma HLS PIPELINE II=1
#pragma HLS UNROLL factor=4
        output[i] = input[i] * 3 + 7;
    }
"""


def _tag(rng: random.Random, seed: int) -> str:
    """Return a stable identifier so the seed participates in source bytes."""

    return f"{seed:x}_{rng.randrange(0x10000):04x}"


def _repair_loop(body: str, rng: random.Random, seed: int) -> str:
    index = f"v3d_i_{_tag(rng, seed)}"
    return body.replace("{i}", index)


def _render_off_by_one(rng: random.Random, seed: int) -> str:
    return _repair_loop(
        "    for (int {i} = 0; {i} < V3D_SIZE - 1; ++{i}) {\n"
        "        output[{i}] = input[{i}] * 3 + 7;\n"
        "    }\n",
        rng,
        seed,
    )


def _render_wrong_multiplier(rng: random.Random, seed: int) -> str:
    return _repair_loop(
        "    for (int {i} = 0; {i} < V3D_SIZE; ++{i}) {\n"
        "        output[{i}] = input[{i}] * 2 + 7;\n"
        "    }\n",
        rng,
        seed,
    )


def _render_bias_sign(rng: random.Random, seed: int) -> str:
    return _repair_loop(
        "    for (int {i} = 0; {i} < V3D_SIZE; ++{i}) {\n"
        "        output[{i}] = input[{i}] * 3 - 7;\n"
        "    }\n",
        rng,
        seed,
    )


def _render_shifted_index(rng: random.Random, seed: int) -> str:
    return _repair_loop(
        "    for (int {i} = 0; {i} < V3D_SIZE; ++{i}) {\n"
        "        output[{i}] = input[({i} + 1) % V3D_SIZE] * 3 + 7;\n"
        "    }\n",
        rng,
        seed,
    )


def _render_running_sum(rng: random.Random, seed: int) -> str:
    index = f"v3d_i_{_tag(rng, seed)}"
    accumulator = f"v3d_acc_{seed:x}"
    return (
        f"    int {accumulator} = 0;\n"
        f"    for (int {index} = 0; {index} < V3D_SIZE; ++{index}) {{\n"
        f"        {accumulator} += input[{index}] * 3 + 7;\n"
        f"        output[{index}] = {accumulator};\n"
        "    }\n"
    )


def _render_clamp_negative(rng: random.Random, seed: int) -> str:
    index = f"v3d_i_{_tag(rng, seed)}"
    value = f"v3d_value_{seed:x}"
    return (
        f"    for (int {index} = 0; {index} < V3D_SIZE; ++{index}) {{\n"
        f"        int {value} = input[{index}] * 3 + 7;\n"
        f"        output[{index}] = {value} < 0 ? 0 : {value};\n"
        "    }\n"
    )


def _render_branch_inversion(rng: random.Random, seed: int) -> str:
    index = f"v3d_i_{_tag(rng, seed)}"
    return (
        f"    for (int {index} = 0; {index} < V3D_SIZE; ++{index}) {{\n"
        f"        if (input[{index}] >= 0) {{\n"
        f"            output[{index}] = input[{index}] * 3 - 7;\n"
        "        } else {\n"
        f"            output[{index}] = input[{index}] * 3 + 7;\n"
        "        }\n"
        "    }\n"
    )


def _render_rotated_output(rng: random.Random, seed: int) -> str:
    return _repair_loop(
        "    for (int {i} = 0; {i} < V3D_SIZE; ++{i}) {\n"
        "        output[({i} + 1) % V3D_SIZE] = input[{i}] * 3 + 7;\n"
        "    }\n",
        rng,
        seed,
    )


def _render_dynamic_allocation(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {{
    int* v3d_scratch_{tag} = new int[V3D_SIZE];
    for (int i = 0; i < V3D_SIZE; ++i) {{
        v3d_scratch_{tag}[i] = input[i] * 3 + 7;
        output[i] = v3d_scratch_{tag}[i];
    }}
    delete[] v3d_scratch_{tag};
}}
"""


def _render_recursion(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""static int v3d_recursive_value_{tag}(const int input[V3D_SIZE], int index) {{
    if (index == 0) return input[0] * 3 + 7;
    return v3d_recursive_value_{tag}(input, index - 1)
        + (input[index] - input[index - 1]) * 3;
}}

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {{
    for (int i = 0; i < V3D_SIZE; ++i) {{
        output[i] = v3d_recursive_value_{tag}(input, i);
    }}
}}
"""


def _render_function_pointer(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""static int v3d_transform_{tag}(int value) {{
    return value * 3 + 7;
}}

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {{
    int (*v3d_function_{tag})(int) = &v3d_transform_{tag};
    for (int i = 0; i < V3D_SIZE; ++i) {{
        output[i] = v3d_function_{tag}(input[i]);
    }}
}}
"""


def _render_std_vector(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""#include <vector>

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {{
    std::vector<int> v3d_buffer_{tag}(V3D_SIZE);
    for (int i = 0; i < V3D_SIZE; ++i) {{
        v3d_buffer_{tag}[i] = input[i] * 3 + 7;
        output[i] = v3d_buffer_{tag}[i];
    }}
}}
"""


def _render_exception(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""struct v3d_exception_{tag} {{}};

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {{
    for (int i = 0; i < V3D_SIZE; ++i) {{
        try {{
            if (input[i] == 2147483647) throw v3d_exception_{tag}{{}};
            output[i] = input[i] * 3 + 7;
        }} catch (const v3d_exception_{tag}&) {{
            output[i] = 0;
        }}
    }}
}}
"""


def _render_virtual_dispatch(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""struct v3d_transform_base_{tag} {{
    virtual int apply(int value) const = 0;
}};

struct v3d_transform_impl_{tag} : v3d_transform_base_{tag} {{
    int apply(int value) const override {{ return value * 3 + 7; }}
}};

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {{
    v3d_transform_impl_{tag} implementation;
    const v3d_transform_base_{tag}* transform = &implementation;
    for (int i = 0; i < V3D_SIZE; ++i) {{
        output[i] = transform->apply(input[i]);
    }}
}}
"""


def _render_structural(order: str, chunk: int | None = None) -> Callable[[random.Random, int], str]:
    def render(rng: random.Random, seed: int) -> str:
        tag = _tag(rng, seed)
        if chunk is None:
            first, second = (
                ("main_stream.write(input[i]);", "side_stream.write(input[i] + 1);")
                if order == "main"
                else ("side_stream.write(input[i] + 1);", "main_stream.write(input[i]);")
            )
            body = (
                "    for (int i = 0; i < V3D_SIZE; ++i) {\n"
                f"        {first}\n"
                "    }\n"
                "    for (int i = 0; i < V3D_SIZE; ++i) {\n"
                f"        {second}\n"
                "    }\n"
            )
        else:
            first, second = (
                ("main_stream.write(input[i]);", "side_stream.write(input[i] + 1);")
                if order == "main"
                else ("side_stream.write(input[i] + 1);", "main_stream.write(input[i]);")
            )
            body = (
                f"    constexpr int v3d_chunk_{tag} = {chunk};\n"
                f"    for (int base = 0; base < V3D_SIZE; base += v3d_chunk_{tag}) {{\n"
                f"        for (int i = base; i < base + v3d_chunk_{tag} && i < V3D_SIZE; ++i) {{\n"
                f"            {first}\n"
                "        }\n"
                f"        for (int i = base; i < base + v3d_chunk_{tag} && i < V3D_SIZE; ++i) {{\n"
                f"            {second}\n"
                "        }\n"
                "    }\n"
            )
        return (
            "static void v3d_produce(\n"
            "    const int input[V3D_SIZE],\n"
            "    hls::stream<int>& main_stream,\n"
            "    hls::stream<int>& side_stream) {\n"
            "#pragma HLS INLINE off\n"
            + body
            + "}\n"
        )

    return render


def _render_optimize(
    *, pipeline_ii: int | None = 1, unroll: int | None = 4, partition: bool = True
) -> Callable[[random.Random, int], str]:
    def render(rng: random.Random, seed: int) -> str:
        tag = _tag(rng, seed)
        lines: list[str] = []
        if partition:
            lines.extend(
                [
                    "#pragma HLS ARRAY_PARTITION variable=input cyclic factor=4 dim=1",
                    "#pragma HLS ARRAY_PARTITION variable=output cyclic factor=4 dim=1",
                ]
            )
        lines.extend(
            [
                f"v3d_map_{tag}:",
                "    for (int i = 0; i < V3D_SIZE; ++i) {",
            ]
        )
        if pipeline_ii is not None:
            lines.append(f"#pragma HLS PIPELINE II={pipeline_ii}")
        if unroll is not None:
            lines.append(f"#pragma HLS UNROLL factor={unroll}")
        lines.extend(
            [
                "        output[i] = input[i] * 3 + 7;",
                "    }",
            ]
        )
        return "\n".join(lines) + "\n"

    return render


def _render_serial_two_pass(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""    int v3d_temporary_{tag}[V3D_SIZE];
v3d_scale_{tag}:
    for (int i = 0; i < V3D_SIZE; ++i) {{
        v3d_temporary_{tag}[i] = input[i] * 3;
    }}
v3d_bias_{tag}:
    for (int i = 0; i < V3D_SIZE; ++i) {{
        output[i] = v3d_temporary_{tag}[i] + 7;
    }}
"""


def _operator(
    name: str,
    mode: str,
    gate: str,
    expected_region: str,
    render: Callable[[random.Random, int], str],
) -> _MutationOperator:
    return _MutationOperator(name, mode, gate, expected_region, render)


_OPERATORS: Mapping[str, _MutationOperator] = {
    item.name: item
    for item in (
        _operator("repair_off_by_one", "REPAIR", "csim", _COMMON_REGION, _render_off_by_one),
        _operator("repair_wrong_multiplier", "REPAIR", "csim", _COMMON_REGION, _render_wrong_multiplier),
        _operator("repair_bias_sign", "REPAIR", "csim", _COMMON_REGION, _render_bias_sign),
        _operator("repair_shifted_index", "REPAIR", "csim", _COMMON_REGION, _render_shifted_index),
        _operator("repair_running_sum", "REPAIR", "csim", _COMMON_REGION, _render_running_sum),
        _operator("repair_clamp_negative", "REPAIR", "csim", _COMMON_REGION, _render_clamp_negative),
        _operator("repair_branch_inversion", "REPAIR", "csim", _COMMON_REGION, _render_branch_inversion),
        _operator("repair_rotated_output", "REPAIR", "csim", _COMMON_REGION, _render_rotated_output),
        _operator("synth_dynamic_allocation", "SYNTH_FIX", "synth", _SYNTH_REGION, _render_dynamic_allocation),
        _operator("synth_recursion", "SYNTH_FIX", "synth", _SYNTH_REGION, _render_recursion),
        _operator("synth_function_pointer", "SYNTH_FIX", "synth", _SYNTH_REGION, _render_function_pointer),
        _operator("synth_std_vector", "SYNTH_FIX", "synth", _SYNTH_REGION, _render_std_vector),
        _operator("synth_exception", "SYNTH_FIX", "synth", _SYNTH_REGION, _render_exception),
        _operator("synth_virtual_dispatch", "SYNTH_FIX", "synth", _SYNTH_REGION, _render_virtual_dispatch),
        _operator("struct_main_burst", "STRUCTURAL_FIX", "cosim", _STRUCTURAL_REGION, _render_structural("main")),
        _operator("struct_side_burst", "STRUCTURAL_FIX", "cosim", _STRUCTURAL_REGION, _render_structural("side")),
        _operator("struct_main_chunk_3", "STRUCTURAL_FIX", "cosim", _STRUCTURAL_REGION, _render_structural("main", 3)),
        _operator("struct_side_chunk_3", "STRUCTURAL_FIX", "cosim", _STRUCTURAL_REGION, _render_structural("side", 3)),
        _operator("struct_main_chunk_4", "STRUCTURAL_FIX", "cosim", _STRUCTURAL_REGION, _render_structural("main", 4)),
        _operator("struct_side_chunk_4", "STRUCTURAL_FIX", "cosim", _STRUCTURAL_REGION, _render_structural("side", 4)),
        _operator("opt_remove_pipeline", "OPTIMIZE", "ppa", _OPTIMIZE_REGION, _render_optimize(pipeline_ii=None)),
        _operator("opt_pipeline_ii_2", "OPTIMIZE", "ppa", _OPTIMIZE_REGION, _render_optimize(pipeline_ii=2)),
        _operator("opt_pipeline_ii_4", "OPTIMIZE", "ppa", _OPTIMIZE_REGION, _render_optimize(pipeline_ii=4)),
        _operator("opt_pipeline_ii_8", "OPTIMIZE", "ppa", _OPTIMIZE_REGION, _render_optimize(pipeline_ii=8)),
        _operator("opt_remove_unroll", "OPTIMIZE", "ppa", _OPTIMIZE_REGION, _render_optimize(unroll=None)),
        _operator("opt_unroll_factor_2", "OPTIMIZE", "ppa", _OPTIMIZE_REGION, _render_optimize(unroll=2)),
        _operator("opt_remove_partition", "OPTIMIZE", "ppa", _OPTIMIZE_REGION, _render_optimize(partition=False)),
        _operator("opt_serial_two_pass", "OPTIMIZE", "ppa", _OPTIMIZE_REGION, _render_serial_two_pass),
    )
}


TASK_SPECS: tuple[TaskSpec, ...] = (
    TaskSpec("v3d_fast_001", "REPAIR", "repair_off_by_one", 101, "map", 1, "循环上界遗漏最后一个元素。"),
    TaskSpec("v3d_fast_002", "REPAIR", "repair_wrong_multiplier", 102, "map", 1, "逐元素缩放系数不符合公开语义。"),
    TaskSpec("v3d_fast_003", "REPAIR", "repair_bias_sign", 103, "map", 1, "逐元素偏置方向错误。"),
    TaskSpec("v3d_fast_004", "REPAIR", "repair_shifted_index", 104, "map", 2, "输入索引发生循环错位。"),
    TaskSpec("v3d_fast_005", "REPAIR", "repair_running_sum", 105, "map", 2, "独立映射被错误地累积。"),
    TaskSpec("v3d_fast_006", "REPAIR", "repair_clamp_negative", 106, "map", 2, "负结果被未声明地截断。"),
    TaskSpec("v3d_fast_007", "REPAIR", "repair_branch_inversion", 107, "map", 2, "符号分支对非负输入采用错误公式。"),
    TaskSpec("v3d_fast_008", "REPAIR", "repair_rotated_output", 108, "map", 2, "输出索引发生循环错位。"),
    TaskSpec("v3d_fast_009", "SYNTH_FIX", "synth_dynamic_allocation", 201, "synth_map", 2, "C 仿真正确，但核心路径使用动态内存。"),
    TaskSpec("v3d_fast_010", "SYNTH_FIX", "synth_recursion", 202, "synth_map", 3, "C 仿真正确，但实现依赖递归调用。"),
    TaskSpec("v3d_fast_011", "SYNTH_FIX", "synth_function_pointer", 203, "synth_map", 3, "C 仿真正确，但数据路径使用函数指针。"),
    TaskSpec("v3d_fast_012", "SYNTH_FIX", "synth_std_vector", 204, "synth_map", 2, "C 仿真正确，但核心路径使用动态 STL 容器。"),
    TaskSpec("v3d_fast_013", "SYNTH_FIX", "synth_exception", 205, "synth_map", 3, "C 仿真正确，但核心路径包含异常控制流。"),
    TaskSpec("v3d_fast_014", "SYNTH_FIX", "synth_virtual_dispatch", 206, "synth_map", 3, "C 仿真正确，但核心路径包含虚函数分派。"),
    TaskSpec("v3d_fast_015", "STRUCTURAL_FIX", "struct_main_burst", 301, "dual_stream", 4, "双流生产者先突发写主流，RTL 有界 FIFO 形成背压环。"),
    TaskSpec("v3d_fast_016", "STRUCTURAL_FIX", "struct_side_burst", 302, "dual_stream", 4, "双流生产者先突发写旁路流，RTL 有界 FIFO 形成背压环。"),
    TaskSpec("v3d_fast_017", "STRUCTURAL_FIX", "struct_main_chunk_3", 303, "dual_stream", 4, "主流按三元素分块领先旁路流，超过 FIFO 深度。"),
    TaskSpec("v3d_fast_018", "STRUCTURAL_FIX", "struct_side_chunk_3", 304, "dual_stream", 4, "旁路流按三元素分块领先主流，超过 FIFO 深度。"),
    TaskSpec("v3d_fast_019", "STRUCTURAL_FIX", "struct_main_chunk_4", 305, "dual_stream", 4, "主流按四元素分块领先旁路流，超过 FIFO 深度。"),
    TaskSpec("v3d_fast_020", "STRUCTURAL_FIX", "struct_side_chunk_4", 306, "dual_stream", 4, "旁路流按四元素分块领先主流，超过 FIFO 深度。"),
    TaskSpec("v3d_fast_021", "OPTIMIZE", "opt_remove_pipeline", 401, "opt_map", 2, "功能正确，但核心循环未流水化。"),
    TaskSpec("v3d_fast_022", "OPTIMIZE", "opt_pipeline_ii_2", 402, "opt_map", 2, "功能正确，但请求的流水间隔仍有收紧空间。"),
    TaskSpec("v3d_fast_023", "OPTIMIZE", "opt_pipeline_ii_4", 403, "opt_map", 2, "功能正确，但流水启动间隔偏大。"),
    TaskSpec("v3d_fast_024", "OPTIMIZE", "opt_pipeline_ii_8", 404, "opt_map", 2, "功能正确，但流水启动间隔明显偏大。"),
    TaskSpec("v3d_fast_025", "OPTIMIZE", "opt_remove_unroll", 405, "opt_map", 2, "功能正确，但循环并行度未显式展开。"),
    TaskSpec("v3d_fast_026", "OPTIMIZE", "opt_unroll_factor_2", 406, "opt_map", 3, "功能正确，但循环展开因子保守。"),
    TaskSpec("v3d_fast_027", "OPTIMIZE", "opt_remove_partition", 407, "opt_map", 3, "功能正确，但数组端口并行性不足。"),
    TaskSpec("v3d_fast_028", "OPTIMIZE", "opt_serial_two_pass", 408, "opt_map", 3, "功能正确，但可融合的数据路径被串行拆成两遍。", True),
)


class MutationEngine:
    """Apply one registered mutation to one marker-delimited golden source."""

    version = MUTATION_ENGINE_VERSION

    def mutate(self, base_source: str, *, operator: str, seed: int) -> MutationResult:
        if operator not in _OPERATORS:
            raise V3DCorpusError(f"unknown mutation operator: {operator}")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise V3DCorpusError("mutation seed must be a non-negative integer")
        begin = base_source.find(_BEGIN_MARKER)
        end = base_source.find(_END_MARKER)
        if begin < 0 or end < 0 or base_source.find(_BEGIN_MARKER, begin + 1) >= 0:
            raise V3DCorpusError("golden source must contain one mutation marker pair")
        if base_source.find(_END_MARKER, end + 1) >= 0 or end <= begin:
            raise V3DCorpusError("golden source has an invalid mutation marker pair")
        begin_line_end = base_source.find("\n", begin)
        if begin_line_end < 0:
            raise V3DCorpusError("mutation begin marker must end with a newline")
        begin_line_start = base_source.rfind("\n", 0, begin) + 1
        end_line_start = base_source.rfind("\n", 0, end) + 1
        if base_source[begin_line_start:begin].strip() or base_source[end_line_start:end].strip():
            raise V3DCorpusError("mutation markers must be alone on their lines")
        region_start = begin_line_end + 1
        region = base_source[region_start:end_line_start]
        selected = _OPERATORS[operator]
        if region != selected.expected_region:
            raise V3DCorpusError(
                f"golden mutation region does not match operator {operator}"
            )
        end_line_end = base_source.find("\n", end)
        suffix_start = len(base_source) if end_line_end < 0 else end_line_end + 1
        prefix = base_source[:begin_line_start]
        suffix = base_source[suffix_start:]
        replacement = selected.render(random.Random(seed), seed)
        if not replacement or not replacement.endswith("\n"):
            raise V3DCorpusError("mutation operator must render newline-terminated source")
        mutated = prefix + replacement + suffix
        base_start_line = base_source.count("\n", 0, region_start) + 1
        base_end_line = base_start_line + region.count("\n") - 1
        mutated_start_line = prefix.count("\n") + 1
        mutated_end_line = mutated_start_line + replacement.count("\n") - 1
        return MutationResult(
            source=mutated,
            base_sha256=_sha256(base_source.encode("utf-8")),
            mutated_sha256=_sha256(mutated.encode("utf-8")),
            base_range_sha256=_sha256(region.encode("utf-8")),
            base_start_line=base_start_line,
            base_end_line=base_end_line,
            mutated_start_line=mutated_start_line,
            mutated_end_line=mutated_end_line,
        )


_COMMON_HEADER = """#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_SIZE = 16;

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]);

#endif
"""

_STRUCTURAL_HEADER = """#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_SIZE = 16;

#ifdef __SYNTHESIS__
#include <hls_stream.h>
#else
#include <queue>
namespace hls {
template <typename T>
class stream {
  public:
    explicit stream(const char* = "") {}
    void write(const T& value) { values_.push(value); }
    T read() {
        T value = values_.front();
        values_.pop();
        return value;
    }

  private:
    std::queue<T> values_;
};
}  // namespace hls
#endif

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]);

#endif
"""


def _golden_source(family: str) -> str:
    if family == "map":
        return (
            '#include "kernel.h"\n\n'
            "void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {\n"
            f"    {_BEGIN_MARKER}\n"
            + _COMMON_REGION
            + f"    {_END_MARKER}\n"
            "}\n"
        )
    if family == "synth_map":
        return (
            '#include "kernel.h"\n\n'
            f"{_BEGIN_MARKER}\n"
            + _SYNTH_REGION
            + f"{_END_MARKER}\n"
        )
    if family == "dual_stream":
        return (
            '#include "kernel.h"\n\n'
            f"{_BEGIN_MARKER}\n"
            + _STRUCTURAL_REGION
            + f"{_END_MARKER}\n\n"
            "static void v3d_consume(\n"
            "    hls::stream<int>& main_stream,\n"
            "    hls::stream<int>& side_stream,\n"
            "    int output[V3D_SIZE]) {\n"
            "#pragma HLS INLINE off\n"
            "    for (int i = 0; i < V3D_SIZE; ++i) {\n"
            "#pragma HLS PIPELINE II=1\n"
            "        output[i] = main_stream.read() + side_stream.read();\n"
            "    }\n"
            "}\n\n"
            "void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {\n"
            "#pragma HLS DATAFLOW\n"
            '    hls::stream<int> main_stream("main_stream");\n'
            '    hls::stream<int> side_stream("side_stream");\n'
            "#pragma HLS STREAM variable=main_stream depth=1\n"
            "#pragma HLS STREAM variable=side_stream depth=1\n"
            "    v3d_produce(input, main_stream, side_stream);\n"
            "    v3d_consume(main_stream, side_stream, output);\n"
            "}\n"
        )
    if family == "opt_map":
        return (
            '#include "kernel.h"\n\n'
            "void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {\n"
            f"    {_BEGIN_MARKER}\n"
            + _OPTIMIZE_REGION
            + f"    {_END_MARKER}\n"
            "}\n"
        )
    raise V3DCorpusError(f"unknown task family: {family}")


def _header(family: str) -> str:
    return _STRUCTURAL_HEADER if family == "dual_stream" else _COMMON_HEADER


def _testbench(*, structural: bool, hidden_like: bool) -> str:
    if hidden_like:
        values = (
            "-31, 17, 0, 8, -9, 42, 3, -5, 11, -2, 29, 6, -15, 1, 23, -7"
        )
    else:
        values = "-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10"
    expression = "input[i] * 2 + 1" if structural else "input[i] * 3 + 7"
    label = "hidden-like" if hidden_like else "public"
    return f"""#include "kernel.h"

#include <iostream>

int main() {{
    const int input[V3D_SIZE] = {{{values}}};
    int output[V3D_SIZE] = {{}};
    kernel(input, output);
    for (int i = 0; i < V3D_SIZE; ++i) {{
        const int expected = {expression};
        if (output[i] != expected) {{
            std::cerr << "{label} mismatch at " << i
                      << ": expected " << expected
                      << ", got " << output[i] << "\\n";
            return 1;
        }}
    }}
    return 0;
}}
"""


def _baseline_validation(mode: str, *, requires_cosim: bool) -> dict[str, str]:
    return {
        "REPAIR": {"csim": "FAIL", "synth": "NOT_RUN", "cosim": "NOT_RUN"},
        "SYNTH_FIX": {"csim": "PASS", "synth": "FAIL", "cosim": "NOT_RUN"},
        "STRUCTURAL_FIX": {"csim": "PASS", "synth": "PASS", "cosim": "FAIL"},
        "OPTIMIZE": {
            "csim": "PASS",
            "synth": "PASS",
            "cosim": "PASS" if requires_cosim else "NOT_RUN",
        },
    }[mode]


def _functional_formula(spec: TaskSpec) -> str:
    return (
        "output[i] = input[i] * 2 + 1"
        if spec.family == "dual_stream"
        else "output[i] = input[i] * 3 + 7"
    )


def _public_contract(spec: TaskSpec) -> str:
    return (
        f"对每个 0 <= i < V3D_SIZE，必须满足 {_functional_formula(spec)}；"
        "V3D_SIZE 固定为 16，合法输入元素范围为 [-1000000, 1000000]。"
    )


def _task_toml(spec: TaskSpec) -> str:
    budget = 100 if spec.requires_cosim or spec.mode == "OPTIMIZE" else 60
    requires_cosim = "true" if spec.requires_cosim else "false"
    initial_condition = _public_contract(spec)
    return f"""task_id = "{spec.task_id}"
task_type = "{spec.task_type}"
difficulty = {spec.difficulty}
top = "kernel"
kernel_file = "kernel.cpp"
header_files = ["kernel.h"]
public_tb = "kernel_tb.cpp"
budget = {budget}
requires_cosim = {requires_cosim}
initial_condition = "{initial_condition}"

[target]
part = "xcu55c-fsvh2892-2L-e"
clock_ns = 5.0
"""


def _description(spec: TaskSpec) -> str:
    return f"""# V3-D 公开 kernel 任务

## 功能语义

{_public_contract(spec)}

## 接口与约束

- 顶层函数必须保持为 `kernel(const int input[16], int output[16])`。
- 必须处理全部 16 个元素，不得越界访问输入或输出数组。
- 只允许修改 `kernel.cpp`；header、公开 testbench 和任务元数据均为只读。
- 公开 testbench 只给出示例，提交实现必须满足上述全部合法输入。
"""


def _validate_public_metadata(spec: TaskSpec, task_toml: str, description: str) -> None:
    """Fail generation if evaluator-only labels enter Planner-visible text."""

    exposed = (task_toml + "\n" + description).casefold()
    evaluator_only = {
        *(mode.casefold() for mode in PHASE_MODES),
        *(item.operator.casefold() for item in TASK_SPECS),
        *(item.mutation_summary_zh.casefold() for item in TASK_SPECS),
        "expected_failing_gate",
        "expected_mode",
        "mutation_summary",
        "失败门",
        "基线",
        "缺陷",
        "修复提示",
    }
    leaked = sorted(label for label in evaluator_only if label and label in exposed)
    if leaked:
        raise V3DCorpusError(
            f"task {spec.task_id} public metadata leaks evaluator labels: {leaked}"
        )
    opaque_suffix = spec.task_id.removeprefix("v3d_fast_")
    if (
        spec.task_type != "generate"
        or len(opaque_suffix) != 3
        or not opaque_suffix.isdigit()
    ):
        raise V3DCorpusError(
            f"task {spec.task_id} must use opaque, mode-neutral public metadata"
        )


def _mutation_manifest(
    spec: TaskSpec, operator: _MutationOperator, result: MutationResult
) -> dict[str, object]:
    return {
        "schema_version": MUTATION_SCHEMA_VERSION,
        "version": 1,
        "engine_version": MUTATION_ENGINE_VERSION,
        "task_id": spec.task_id,
        "base_sha256": result.base_sha256,
        "mutated_sha256": result.mutated_sha256,
        "operator": operator.name,
        "operator_version": operator.version,
        "seed": spec.seed,
        "source_range": {
            "file": "kernel.cpp",
            "base_start_line": result.base_start_line,
            "base_end_line": result.base_end_line,
            "mutated_start_line": result.mutated_start_line,
            "mutated_end_line": result.mutated_end_line,
            "base_range_sha256": result.base_range_sha256,
        },
        "expected_mode": spec.mode,
        "expected_failing_gate": spec.failing_gate,
        "mutation_summary": spec.mutation_summary_zh,
    }


def _acceptance(spec: TaskSpec, result: MutationResult) -> dict[str, object]:
    return {
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "task_id": spec.task_id,
        "expected_mode": spec.mode,
        "expected_failing_gate": spec.failing_gate,
        "requires_cosim": spec.requires_cosim,
        "baseline_validation": _baseline_validation(
            spec.mode, requires_cosim=spec.requires_cosim
        ),
        "golden_validation": {"csim": "PASS", "synth": "PASS", "cosim": "PASS"},
        "golden_kernel_sha256": result.base_sha256,
        "mutated_kernel_sha256": result.mutated_sha256,
        "planner_input_policy": {
            "allowed_paths": list(PLANNER_VISIBLE_PATHS),
            "forbidden_components": list(PLANNER_FORBIDDEN_COMPONENTS),
        },
        "evidence_level": "DETERMINISTIC_FIXTURE_NOT_REAL_VITIS",
    }


def _schema_document(kind: str) -> dict[str, object]:
    common = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
    }
    digest = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    validation = {
        "type": "object",
        "additionalProperties": False,
        "required": ["csim", "synth", "cosim"],
        "properties": {
            gate: {"enum": list(VALIDATION_STATES)}
            for gate in ("csim", "synth", "cosim")
        },
    }
    golden_validation = {
        "type": "object",
        "additionalProperties": False,
        "required": ["csim", "synth", "cosim"],
        "properties": {
            gate: {"const": "PASS"}
            for gate in ("csim", "synth", "cosim")
        },
    }
    planner_policy = {
        "type": "object",
        "additionalProperties": False,
        "required": ["allowed_paths", "forbidden_components"],
        "properties": {
            "allowed_paths": {
                "const": list(PLANNER_VISIBLE_PATHS),
            },
            "forbidden_components": {
                "const": list(PLANNER_FORBIDDEN_COMPONENTS),
            },
        },
    }
    if kind == "mutation":
        return common | {
            "$id": "https://llm4hls.local/schema/v3d-mutation-manifest-v1.json",
            "required": [
                "schema_version", "version", "engine_version", "task_id",
                "base_sha256", "mutated_sha256", "operator", "operator_version",
                "seed", "source_range", "expected_mode", "expected_failing_gate",
                "mutation_summary",
            ],
            "properties": {
                "schema_version": {"const": MUTATION_SCHEMA_VERSION},
                "version": {"const": 1},
                "engine_version": {"const": MUTATION_ENGINE_VERSION},
                "task_id": {"type": "string", "pattern": "^v3d_fast_[0-9]{3}$"},
                "base_sha256": digest,
                "mutated_sha256": digest,
                "operator": {"type": "string", "minLength": 1},
                "operator_version": {"const": 1},
                "seed": {"type": "integer", "minimum": 0},
                "source_range": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "file", "base_start_line", "base_end_line",
                        "mutated_start_line", "mutated_end_line",
                        "base_range_sha256",
                    ],
                    "properties": {
                        "file": {"const": "kernel.cpp"},
                        "base_start_line": {"type": "integer", "minimum": 1},
                        "base_end_line": {"type": "integer", "minimum": 1},
                        "mutated_start_line": {"type": "integer", "minimum": 1},
                        "mutated_end_line": {"type": "integer", "minimum": 1},
                        "base_range_sha256": digest,
                    },
                },
                "expected_mode": {"enum": list(PHASE_MODES)},
                "expected_failing_gate": {"enum": list(FAILING_GATES)},
                "mutation_summary": {"type": "string", "minLength": 1},
            },
        }
    if kind == "acceptance":
        return common | {
            "$id": "https://llm4hls.local/schema/v3d-acceptance-v1.json",
            "required": [
                "schema_version", "task_id", "expected_mode",
                "expected_failing_gate", "requires_cosim", "baseline_validation",
                "golden_validation", "golden_kernel_sha256",
                "mutated_kernel_sha256", "planner_input_policy", "evidence_level",
            ],
            "properties": {
                "schema_version": {"const": ACCEPTANCE_SCHEMA_VERSION},
                "task_id": {"type": "string", "pattern": "^v3d_fast_[0-9]{3}$"},
                "expected_mode": {"enum": list(PHASE_MODES)},
                "expected_failing_gate": {"enum": list(FAILING_GATES)},
                "requires_cosim": {"type": "boolean"},
                "baseline_validation": validation,
                "golden_validation": golden_validation,
                "golden_kernel_sha256": digest,
                "mutated_kernel_sha256": digest,
                "planner_input_policy": planner_policy,
                "evidence_level": {"const": "DETERMINISTIC_FIXTURE_NOT_REAL_VITIS"},
            },
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "expected_mode": {"const": "STRUCTURAL_FIX"}
                        },
                        "required": ["expected_mode"],
                    },
                    "then": {
                        "properties": {"requires_cosim": {"const": True}}
                    },
                },
                {
                    "if": {
                        "properties": {
                            "expected_mode": {"enum": ["REPAIR", "SYNTH_FIX"]}
                        },
                        "required": ["expected_mode"],
                    },
                    "then": {
                        "properties": {"requires_cosim": {"const": False}}
                    },
                },
                {
                    "if": {
                        "properties": {
                            "expected_mode": {"const": "OPTIMIZE"},
                            "requires_cosim": {"const": True},
                        },
                        "required": ["expected_mode", "requires_cosim"],
                    },
                    "then": {
                        "properties": {
                            "baseline_validation": {
                                "properties": {"cosim": {"const": "PASS"}}
                            },
                            "golden_validation": {
                                "properties": {"cosim": {"const": "PASS"}}
                            },
                        }
                    },
                },
            ],
        }
    if kind == "corpus":
        return common | {
            "$id": "https://llm4hls.local/schema/v3d-fast-corpus-v1.json",
            "required": [
                "schema_version", "generator_version", "mutation_engine_version",
                "task_count", "mode_counts", "planner_input_policy", "tasks",
            ],
            "properties": {
                "schema_version": {"const": CORPUS_SCHEMA_VERSION},
                "generator_version": {"const": GENERATOR_VERSION},
                "mutation_engine_version": {"const": MUTATION_ENGINE_VERSION},
                "task_count": {"type": "integer", "minimum": 1},
                "mode_counts": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(PHASE_MODES),
                    "properties": {
                        mode: {"type": "integer", "minimum": 0}
                        for mode in PHASE_MODES
                    },
                },
                "planner_input_policy": planner_policy,
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "task_id", "path", "mode", "operator", "seed",
                            "mutation_manifest_sha256", "acceptance_sha256",
                        ],
                        "properties": {
                            "task_id": {"type": "string", "pattern": "^v3d_fast_[0-9]{3}$"},
                            "path": {"type": "string", "pattern": "^tasks/[A-Za-z0-9_+-]+$"},
                            "mode": {"enum": list(PHASE_MODES)},
                            "operator": {"type": "string", "minLength": 1},
                            "seed": {"type": "integer", "minimum": 0},
                            "mutation_manifest_sha256": digest,
                            "acceptance_sha256": digest,
                        },
                    },
                },
            },
        }
    raise AssertionError(kind)


def validate_mutation_manifest(value: Mapping[str, object]) -> dict[str, object]:
    required = {
        "schema_version", "version", "engine_version", "task_id",
        "base_sha256", "mutated_sha256", "operator", "operator_version", "seed",
        "source_range", "expected_mode", "expected_failing_gate",
        "mutation_summary",
    }
    if set(value) != required:
        raise V3DCorpusError("mutation manifest fields do not match v1 schema")
    if (
        value.get("schema_version") != MUTATION_SCHEMA_VERSION
        or value.get("version") != 1
        or value.get("engine_version") != MUTATION_ENGINE_VERSION
        or value.get("operator_version") != 1
    ):
        raise V3DCorpusError("mutation manifest version is unsupported")
    task_id = value.get("task_id")
    operator_name = value.get("operator")
    seed = value.get("seed")
    if not _is_opaque_task_id(task_id):
        raise V3DCorpusError("mutation task_id must be opaque")
    if not isinstance(operator_name, str) or operator_name not in _OPERATORS:
        raise V3DCorpusError("mutation operator is unsupported")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise V3DCorpusError("mutation seed is invalid")
    if not _is_sha256(value.get("base_sha256")) or not _is_sha256(value.get("mutated_sha256")):
        raise V3DCorpusError("mutation hashes must be SHA-256 digests")
    if value.get("expected_mode") not in PHASE_MODES or value.get("expected_failing_gate") not in FAILING_GATES:
        raise V3DCorpusError("mutation expectation is invalid")
    operator = _OPERATORS[operator_name]
    if value.get("expected_mode") != operator.mode or value.get("expected_failing_gate") != operator.failing_gate:
        raise V3DCorpusError("mutation expectation conflicts with its operator")
    if not isinstance(value.get("mutation_summary"), str) or not value.get(
        "mutation_summary"
    ):
        raise V3DCorpusError("mutation summary must be non-empty")
    source_range = value.get("source_range")
    expected_range_fields = {
        "file", "base_start_line", "base_end_line", "mutated_start_line",
        "mutated_end_line", "base_range_sha256",
    }
    if not isinstance(source_range, Mapping) or set(source_range) != expected_range_fields:
        raise V3DCorpusError("mutation source range is invalid")
    if source_range.get("file") != "kernel.cpp" or not _is_sha256(source_range.get("base_range_sha256")):
        raise V3DCorpusError("mutation source range binding is invalid")
    for field in ("base_start_line", "base_end_line", "mutated_start_line", "mutated_end_line"):
        line = source_range.get(field)
        if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
            raise V3DCorpusError("mutation source range lines must be positive")
    return json.loads(json.dumps(value, sort_keys=True))


def validate_acceptance(value: Mapping[str, object]) -> dict[str, object]:
    required = {
        "schema_version", "task_id", "expected_mode", "expected_failing_gate",
        "requires_cosim", "baseline_validation", "golden_validation",
        "golden_kernel_sha256", "mutated_kernel_sha256", "planner_input_policy",
        "evidence_level",
    }
    if set(value) != required or value.get("schema_version") != ACCEPTANCE_SCHEMA_VERSION:
        raise V3DCorpusError("acceptance fields do not match v1 schema")
    if not _is_opaque_task_id(value.get("task_id")):
        raise V3DCorpusError("acceptance task_id must be opaque")
    mode = value.get("expected_mode")
    if mode not in PHASE_MODES or value.get("expected_failing_gate") not in FAILING_GATES:
        raise V3DCorpusError("acceptance expectation is invalid")
    if value.get("expected_failing_gate") != {
        "REPAIR": "csim", "SYNTH_FIX": "synth",
        "STRUCTURAL_FIX": "cosim", "OPTIMIZE": "ppa",
    }[str(mode)]:
        raise V3DCorpusError("acceptance mode and failing gate conflict")
    requires_cosim = value.get("requires_cosim")
    if not isinstance(requires_cosim, bool):
        raise V3DCorpusError("acceptance requires_cosim must be boolean")
    if mode == "STRUCTURAL_FIX" and not requires_cosim:
        raise V3DCorpusError("structural acceptance requires CoSim")
    if mode in {"REPAIR", "SYNTH_FIX"} and requires_cosim:
        raise V3DCorpusError("repair acceptance cannot require baseline CoSim")
    for name in ("baseline_validation", "golden_validation"):
        validation = value.get(name)
        if not isinstance(validation, Mapping) or set(validation) != {"csim", "synth", "cosim"}:
            raise V3DCorpusError(f"acceptance {name} is invalid")
        if any(status not in VALIDATION_STATES for status in validation.values()):
            raise V3DCorpusError(f"acceptance {name} has an invalid status")
    if value.get("baseline_validation") != _baseline_validation(
        str(mode), requires_cosim=requires_cosim
    ):
        raise V3DCorpusError("acceptance baseline validation conflicts with mode")
    if value.get("golden_validation") != {
        "csim": "PASS",
        "synth": "PASS",
        "cosim": "PASS",
    }:
        raise V3DCorpusError("acceptance golden validation must close all gates")
    if not _is_sha256(value.get("golden_kernel_sha256")) or not _is_sha256(value.get("mutated_kernel_sha256")):
        raise V3DCorpusError("acceptance hashes must be SHA-256 digests")
    policy = value.get("planner_input_policy")
    if not isinstance(policy, Mapping) or set(policy) != {"allowed_paths", "forbidden_components"}:
        raise V3DCorpusError("acceptance planner policy is invalid")
    if policy.get("allowed_paths") != list(PLANNER_VISIBLE_PATHS) or policy.get("forbidden_components") != list(PLANNER_FORBIDDEN_COMPONENTS):
        raise V3DCorpusError("acceptance planner policy diverges from v1")
    if value.get("evidence_level") != "DETERMINISTIC_FIXTURE_NOT_REAL_VITIS":
        raise V3DCorpusError("acceptance evidence level is invalid")
    return json.loads(json.dumps(value, sort_keys=True))


def validate_corpus_manifest(value: Mapping[str, object]) -> dict[str, object]:
    required = {
        "schema_version", "generator_version", "mutation_engine_version",
        "task_count", "mode_counts", "planner_input_policy", "tasks",
    }
    if set(value) != required or value.get("schema_version") != CORPUS_SCHEMA_VERSION:
        raise V3DCorpusError("corpus manifest fields do not match v1 schema")
    if value.get("generator_version") != GENERATOR_VERSION or value.get("mutation_engine_version") != MUTATION_ENGINE_VERSION:
        raise V3DCorpusError("corpus generator version is unsupported")
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise V3DCorpusError("corpus tasks must be a non-empty list")
    if value.get("task_count") != len(tasks):
        raise V3DCorpusError("corpus task_count does not match tasks")
    counts = Counter()
    seen: set[str] = set()
    expected_task_fields = {
        "task_id", "path", "mode", "operator", "seed",
        "mutation_manifest_sha256", "acceptance_sha256",
    }
    for task in tasks:
        if not isinstance(task, Mapping) or set(task) != expected_task_fields:
            raise V3DCorpusError("corpus task entry is invalid")
        task_id = task.get("task_id")
        mode = task.get("mode")
        if not _is_opaque_task_id(task_id) or task_id in seen:
            raise V3DCorpusError("corpus task ids must be unique")
        if task.get("path") != f"tasks/{task_id}" or mode not in PHASE_MODES:
            raise V3DCorpusError("corpus task path or mode is invalid")
        if task.get("operator") not in _OPERATORS or not _is_sha256(task.get("mutation_manifest_sha256")) or not _is_sha256(task.get("acceptance_sha256")):
            raise V3DCorpusError("corpus task binding is invalid")
        seed = task.get("seed")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise V3DCorpusError("corpus task seed is invalid")
        seen.add(task_id)
        counts[str(mode)] += 1
    if value.get("mode_counts") != {mode: counts[mode] for mode in PHASE_MODES}:
        raise V3DCorpusError("corpus mode_counts do not match tasks")
    if value.get("planner_input_policy") != {
        "allowed_paths": list(PLANNER_VISIBLE_PATHS),
        "forbidden_components": list(PLANNER_FORBIDDEN_COMPONENTS),
    }:
        raise V3DCorpusError("corpus planner policy diverges from v1")
    return json.loads(json.dumps(value, sort_keys=True))


def render_corpus_files() -> dict[str, bytes]:
    """Render the complete corpus to an in-memory relative-path mapping."""

    engine = MutationEngine()
    files: dict[str, bytes] = {
        "README.md": (
            "# V3-D FAST CORPUS\n\n"
            "这是由 `llm4hls_agent.v3d_corpus` 确定性生成的 28 道开发题。"
            "它用于快速回归四种 V3-C mode，不是官方 hidden 题库，也不构成真实 Vitis 证据。\n\n"
            "`answer`、`golden`、`hidden`、`hidden_like`、`reference` 均为运行时禁区；"
            "其中生成的 `hidden_like/` 与 `golden/` 仅供离线验收。\n"
        ).encode("utf-8"),
        "schema/mutation-manifest.schema.json": _json_bytes(_schema_document("mutation")),
        "schema/acceptance.schema.json": _json_bytes(_schema_document("acceptance")),
        "schema/corpus-manifest.schema.json": _json_bytes(_schema_document("corpus")),
    }
    task_entries: list[dict[str, object]] = []
    for spec in TASK_SPECS:
        operator = _OPERATORS[spec.operator]
        if operator.mode != spec.mode or operator.failing_gate != spec.failing_gate:
            raise V3DCorpusError(f"task {spec.task_id} conflicts with its operator")
        golden = _golden_source(spec.family)
        result = engine.mutate(golden, operator=spec.operator, seed=spec.seed)
        manifest = _mutation_manifest(spec, operator, result)
        acceptance = _acceptance(spec, result)
        task_toml = _task_toml(spec)
        description = _description(spec)
        _validate_public_metadata(spec, task_toml, description)
        validate_mutation_manifest(manifest)
        validate_acceptance(acceptance)
        prefix = f"tasks/{spec.task_id}"
        manifest_bytes = _json_bytes(manifest)
        acceptance_bytes = _json_bytes(acceptance)
        files.update(
            {
                f"{prefix}/task.toml": task_toml.encode("utf-8"),
                f"{prefix}/description.md": description.encode("utf-8"),
                f"{prefix}/kernel.cpp": result.source.encode("utf-8"),
                f"{prefix}/kernel.h": _header(spec.family).encode("utf-8"),
                f"{prefix}/kernel_tb.cpp": _testbench(
                    structural=spec.family == "dual_stream", hidden_like=False
                ).encode("utf-8"),
                f"{prefix}/hidden_like/kernel_tb.cpp": _testbench(
                    structural=spec.family == "dual_stream", hidden_like=True
                ).encode("utf-8"),
                f"{prefix}/golden/kernel.cpp": golden.encode("utf-8"),
                f"{prefix}/mutation_manifest.json": manifest_bytes,
                f"{prefix}/acceptance.json": acceptance_bytes,
            }
        )
        task_entries.append(
            {
                "task_id": spec.task_id,
                "path": prefix,
                "mode": spec.mode,
                "operator": spec.operator,
                "seed": spec.seed,
                "mutation_manifest_sha256": _sha256(manifest_bytes),
                "acceptance_sha256": _sha256(acceptance_bytes),
            }
        )
    counts = Counter(spec.mode for spec in TASK_SPECS)
    corpus_manifest = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "mutation_engine_version": MUTATION_ENGINE_VERSION,
        "task_count": len(TASK_SPECS),
        "mode_counts": {mode: counts[mode] for mode in PHASE_MODES},
        "planner_input_policy": {
            "allowed_paths": list(PLANNER_VISIBLE_PATHS),
            "forbidden_components": list(PLANNER_FORBIDDEN_COMPONENTS),
        },
        "tasks": task_entries,
    }
    validate_corpus_manifest(corpus_manifest)
    files["corpus_manifest.json"] = _json_bytes(corpus_manifest)
    return dict(sorted(files.items()))


def write_corpus(root: str | Path) -> tuple[Path, ...]:
    """Materialize the deterministic corpus without deleting unrelated files."""

    destination = Path(root)
    written: list[Path] = []
    for relative, content in render_corpus_files().items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        written.append(path)
    return tuple(written)


def corpus_drift(root: str | Path) -> tuple[str, ...]:
    """Return missing, changed, and unexpected file diagnostics."""

    destination = Path(root)
    expected = render_corpus_files()
    actual_paths = {
        str(path.relative_to(destination))
        for path in destination.rglob("*")
        if path.is_file()
    } if destination.is_dir() else set()
    diagnostics: list[str] = []
    for relative, content in expected.items():
        path = destination / relative
        if not path.is_file():
            diagnostics.append(f"missing:{relative}")
        elif path.read_bytes() != content:
            diagnostics.append(f"changed:{relative}")
    for relative in sorted(actual_paths - set(expected)):
        diagnostics.append(f"unexpected:{relative}")
    return tuple(diagnostics)


def task_directories(root: str | Path) -> tuple[Path, ...]:
    """List manifest-declared task directories in stable order."""

    destination = Path(root)
    try:
        value = json.loads((destination / "corpus_manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise V3DCorpusError("cannot read V3-D corpus manifest") from exc
    if not isinstance(value, Mapping):
        raise V3DCorpusError("V3-D corpus manifest must be an object")
    manifest = validate_corpus_manifest(value)
    return tuple(destination / str(task["path"]) for task in manifest["tasks"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate or check the V3-D fast corpus")
    parser.add_argument("output", type=Path, help="corpus output directory")
    parser.add_argument("--check", action="store_true", help="fail if output differs")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.check:
        drift = corpus_drift(args.output)
        if drift:
            for diagnostic in drift:
                print(diagnostic)
            return 1
        return 0
    written = write_corpus(args.output)
    print(f"generated {len(TASK_SPECS)} tasks and {len(written)} files in {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
