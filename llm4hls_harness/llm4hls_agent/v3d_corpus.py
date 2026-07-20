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
_ANCHOR_DUAL_STREAM_FAMILIES = {
    "dual_stream",
    "producer_burst_deadlock",
    "stream_order_or_interface_mismatch",
    "insufficient_fifo_depth",
}
_STREAM_FAMILIES = _ANCHOR_DUAL_STREAM_FAMILIES | {
    "stream_count_mismatch",
    "producer_consumer_rate_mismatch",
    "dependency_cycle",
}
_OPT_MAP_FAMILIES = {
    "opt_map",
    "missing_array_partition",
    "low_parallel_factor",
    "missing_unroll",
    "suboptimal_loop_structure",
}

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

_STREAM_COUNT_REGION = """static void v3d_count_produce(
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

_STREAM_RATE_REGION = """static void v3d_rate_produce(
    const int input[V3D_SIZE],
    hls::stream<int>& data_stream,
    hls::stream<int>& pace_stream) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        data_stream.write(input[i]);
        pace_stream.write(i);
    }
}
"""

_DEPENDENCY_CYCLE_REGION = """static void v3d_cycle_forward(
    const int input[V3D_SIZE],
    hls::stream<int>& forward_stream) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        forward_stream.write(input[i]);
    }
}

static void v3d_cycle_consume(
    hls::stream<int>& forward_stream,
    int output[V3D_SIZE]) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        output[i] = forward_stream.read() * 2 + 1;
    }
}

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {
#pragma HLS DATAFLOW
    hls::stream<int> forward_stream("forward_stream");
#pragma HLS STREAM variable=forward_stream depth=1
    v3d_cycle_forward(input, forward_stream);
    v3d_cycle_consume(forward_stream, output);
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

_PROJECTION_REGION = """    for (int i = 0; i < V3D_POINTS; ++i) {
        output[i].x = input[i].x;
        output[i].y = input[i].y;
    }
"""

_PREFIX_SUM_REGION = """    int running_sum = 0;
    for (int i = 0; i < V3D_PREFIX_SIZE; ++i) {
        running_sum += input[i];
        output[i] = running_sum;
    }
"""

_HISTOGRAM_REGION = """    for (int bin = 0; bin < V3D_HIST_BINS; ++bin) {
        bins[bin] = 0;
    }
    for (int i = 0; i < V3D_HIST_INPUT_SIZE; ++i) {
        if (input[i] < V3D_HIST_BINS) {
            ++bins[input[i]];
        }
    }
"""

_VECTOR_ADD_REGION = """    for (int i = 0; i < V3D_VECTOR_SIZE; ++i) {
        output[i] = lhs[i] + rhs[i];
    }
"""

_FIR_REGION = """    for (int i = 0; i < V3D_FIR_SIZE; ++i) {
        int value = input[i];
        if (i >= 1) {
            value += 2 * input[i - 1];
        }
        if (i >= 2) {
            value += input[i - 2];
        }
        output[i] = value;
    }
"""

_MATMUL_REGION = """void kernel(
    const int lhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM],
    const int rhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM],
    int output[V3D_MATMUL_DIM][V3D_MATMUL_DIM]) {
    for (int row = 0; row < V3D_MATMUL_DIM; ++row) {
        for (int col = 0; col < V3D_MATMUL_DIM; ++col) {
            int sum = 0;
            for (int inner = 0; inner < V3D_MATMUL_DIM; ++inner) {
                sum += lhs[row][inner] * rhs[inner][col];
            }
            output[row][col] = sum;
        }
    }
}
"""

_STENCIL_REGION = """void kernel(
    const int input[V3D_STENCIL_MAX_ROWS][V3D_STENCIL_MAX_COLS],
    int output[V3D_STENCIL_MAX_ROWS][V3D_STENCIL_MAX_COLS],
    int rows,
    int cols) {
    for (int row = 0; row < V3D_STENCIL_MAX_ROWS; ++row) {
        for (int col = 0; col < V3D_STENCIL_MAX_COLS; ++col) {
            output[row][col] = 0;
        }
    }
    for (int row = 1; row < V3D_STENCIL_MAX_ROWS - 1; ++row) {
        for (int col = 1; col < V3D_STENCIL_MAX_COLS - 1; ++col) {
            if (row < rows - 1 && col < cols - 1) {
                output[row][col] = input[row][col]
                    + input[row - 1][col]
                    + input[row + 1][col]
                    + input[row][col - 1]
                    + input[row][col + 1];
            }
        }
    }
}
"""

_DOT_REDUCTION_REGION = """int kernel(
    const int lhs[V3D_DOT_SIZE],
    const int rhs[V3D_DOT_SIZE]) {
    int sum = 0;
    for (int i = 0; i < V3D_DOT_SIZE; ++i) {
        sum += lhs[i] * rhs[i];
    }
    return sum;
}
"""

_OPT_SERIAL_REDUCTION_REGION = """int kernel(
    const int lhs[V3D_REDUCTION_SIZE],
    const int rhs[V3D_REDUCTION_SIZE]) {
#pragma HLS ARRAY_PARTITION variable=lhs cyclic factor=8 dim=1
#pragma HLS ARRAY_PARTITION variable=rhs cyclic factor=8 dim=1
    int partial[8] = {};
#pragma HLS ARRAY_PARTITION variable=partial complete dim=1
    for (int lane = 0; lane < 8; ++lane) {
#pragma HLS UNROLL
        for (int i = lane; i < V3D_REDUCTION_SIZE; i += 8) {
#pragma HLS PIPELINE II=1
            partial[lane] += lhs[i] * rhs[i];
        }
    }
    int sum = 0;
    for (int lane = 0; lane < 8; ++lane) {
#pragma HLS UNROLL
        sum += partial[lane];
    }
    return sum;
}
"""

_OPT_DATAFLOW_REGION = """static void v3d_dataflow_scale(
    const int input[V3D_DATAFLOW_SIZE],
    int scaled[V3D_DATAFLOW_SIZE]) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_DATAFLOW_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        scaled[i] = input[i] * 5;
    }
}

static void v3d_dataflow_bias(
    const int scaled[V3D_DATAFLOW_SIZE],
    int output[V3D_DATAFLOW_SIZE]) {
#pragma HLS INLINE off
    for (int i = 0; i < V3D_DATAFLOW_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        output[i] = scaled[i] - 3;
    }
}

void kernel(
    const int input[V3D_DATAFLOW_SIZE],
    int output[V3D_DATAFLOW_SIZE]) {
#pragma HLS DATAFLOW
    int scaled[V3D_DATAFLOW_SIZE];
    v3d_dataflow_scale(input, scaled);
    v3d_dataflow_bias(scaled, output);
}
"""

_OPT_MEMORY_BANKING_REGION = """void kernel(
    const int input[V3D_BANK_INPUT_SIZE],
    int output[V3D_BANK_OUTPUT_SIZE]) {
    int banks[4][V3D_BANK_OUTPUT_SIZE];
#pragma HLS ARRAY_PARTITION variable=banks complete dim=1
    for (int group = 0; group < V3D_BANK_OUTPUT_SIZE; ++group) {
#pragma HLS PIPELINE II=1
        for (int lane = 0; lane < 4; ++lane) {
#pragma HLS UNROLL
            banks[lane][group] = input[group * 4 + lane];
        }
    }
    for (int group = 0; group < V3D_BANK_OUTPUT_SIZE; ++group) {
#pragma HLS PIPELINE II=1
        output[group] = banks[0][group] + banks[1][group]
            + banks[2][group] + banks[3][group];
    }
}
"""

_OPT_TRANSACTION_REGION = """void kernel(
    const int input[V3D_TRANSACTION_SIZE],
    int output[V3D_TRANSACTION_SIZE]) {
#pragma HLS INTERFACE m_axi port=input offset=slave bundle=gmem0 max_read_burst_length=64 num_read_outstanding=16
#pragma HLS INTERFACE m_axi port=output offset=slave bundle=gmem1 max_write_burst_length=64 num_write_outstanding=16
v3d_transaction_loop:
    for (int i = 0; i < V3D_TRANSACTION_SIZE; ++i) {
#pragma HLS PIPELINE II=1
        output[i] = input[i] * 3 + 1;
    }
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


def _render_projection_wrong_index(rng: random.Random, seed: int) -> str:
    index = f"v3d_point_{_tag(rng, seed)}"
    return (
        f"    for (int {index} = 0; {index} < V3D_POINTS; ++{index}) {{\n"
        f"        const int source_index = ({index} + 1) % V3D_POINTS;\n"
        f"        output[{index}].x = input[source_index].x;\n"
        f"        output[{index}].y = input[source_index].y;\n"
        "    }\n"
    )


def _render_prefix_omitted_term(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""    int v3d_running_{tag} = 0;
    for (int i = 0; i < V3D_PREFIX_SIZE; ++i) {{
        output[i] = v3d_running_{tag};
        v3d_running_{tag} += input[i];
    }}
"""


def _render_histogram_boundary_condition(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""    for (int bin = 0; bin < V3D_HIST_BINS; ++bin) {{
        bins[bin] = 0;
    }}
    for (int v3d_sample_{tag} = 0;
         v3d_sample_{tag} < V3D_HIST_INPUT_SIZE;
         ++v3d_sample_{tag}) {{
        if (input[v3d_sample_{tag}] < V3D_HIST_BINS - 1) {{
            ++bins[input[v3d_sample_{tag}]];
        }}
    }}
"""


def _render_vector_add_numeric_truncation(rng: random.Random, seed: int) -> str:
    index = f"v3d_lane_{_tag(rng, seed)}"
    return (
        f"    for (int {index} = 0; {index} < V3D_VECTOR_SIZE; ++{index}) {{\n"
        f"        output[{index}] = (lhs[{index}] + rhs[{index}]) & 0xff;\n"
        "    }\n"
    )


def _render_fir_wrong_branch(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""    for (int i = 0; i < V3D_FIR_SIZE; ++i) {{
        int v3d_value_{tag} = input[i];
        if (i > 1) {{
            v3d_value_{tag} += 2 * input[i - 1];
        }}
        if (i >= 2) {{
            v3d_value_{tag} += input[i - 2];
        }}
        output[i] = v3d_value_{tag};
    }}
"""


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


def _render_std_function(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""#include <functional>

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {{
    std::function<int(int)> v3d_callable_{tag} = [](int value) {{
        return value * 3 + 7;
    }};
    for (int i = 0; i < V3D_SIZE; ++i) {{
        output[i] = v3d_callable_{tag}(input[i]);
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


def _render_matmul_unsupported_stl(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""#include <vector>

void kernel(
    const int lhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM],
    const int rhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM],
    int output[V3D_MATMUL_DIM][V3D_MATMUL_DIM]) {{
    std::vector<int> v3d_scratch_{tag}(
        V3D_MATMUL_DIM * V3D_MATMUL_DIM, 0);
    for (int row = 0; row < V3D_MATMUL_DIM; ++row) {{
        for (int col = 0; col < V3D_MATMUL_DIM; ++col) {{
            for (int inner = 0; inner < V3D_MATMUL_DIM; ++inner) {{
                v3d_scratch_{tag}[row * V3D_MATMUL_DIM + col]
                    += lhs[row][inner] * rhs[inner][col];
            }}
        }}
    }}
    for (int row = 0; row < V3D_MATMUL_DIM; ++row) {{
        for (int col = 0; col < V3D_MATMUL_DIM; ++col) {{
            output[row][col]
                = v3d_scratch_{tag}[row * V3D_MATMUL_DIM + col];
        }}
    }}
}}
"""


def _render_stencil_non_static_unbounded_loop(
    rng: random.Random, seed: int
) -> str:
    tag = _tag(rng, seed)
    return f"""void kernel(
    const int input[V3D_STENCIL_MAX_ROWS][V3D_STENCIL_MAX_COLS],
    int output[V3D_STENCIL_MAX_ROWS][V3D_STENCIL_MAX_COLS],
    int rows,
    int cols) {{
    for (int row = 0; row < V3D_STENCIL_MAX_ROWS; ++row) {{
        for (int col = 0; col < V3D_STENCIL_MAX_COLS; ++col) {{
            output[row][col] = 0;
        }}
    }}

    int v3d_row_order_{tag}[rows];
    int v3d_initialized_{tag} = 0;
    while (v3d_initialized_{tag} < rows) {{
        v3d_row_order_{tag}[v3d_initialized_{tag}] = v3d_initialized_{tag};
        ++v3d_initialized_{tag};
    }}

    int v3d_position_{tag} = 1;
    while (v3d_position_{tag} < rows - 1) {{
        const int row = v3d_row_order_{tag}[v3d_position_{tag}];
        int col = 1;
        while (col < cols - 1) {{
            output[row][col] = input[row][col]
                + input[row - 1][col]
                + input[row + 1][col]
                + input[row][col - 1]
                + input[row][col + 1];
            ++col;
        }}
        ++v3d_position_{tag};
    }}
}}
"""


def _render_dot_hard_resource_constraint(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""int kernel(
    const int lhs[V3D_DOT_SIZE],
    const int rhs[V3D_DOT_SIZE]) {{
    int v3d_sum_{tag} = 0;
    for (int i = 0; i < V3D_DOT_SIZE; ++i) {{
        int v3d_product_{tag} = lhs[i] * rhs[i];
#pragma HLS BIND_OP variable=v3d_product_{tag} op=mul impl=uram latency=1
        v3d_sum_{tag} += v3d_product_{tag};
    }}
    return v3d_sum_{tag};
}}
"""


def _render_stream_count_mismatch(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""static void v3d_count_produce(
    const int input[V3D_SIZE],
    hls::stream<int>& main_stream,
    hls::stream<int>& side_stream) {{
#pragma HLS INLINE off
    constexpr int v3d_extra_{tag} = {seed};
    for (int i = 0; i < V3D_SIZE; ++i) {{
#pragma HLS PIPELINE II=1
        main_stream.write(input[i]);
        side_stream.write(input[i] + 1);
    }}
    side_stream.write(v3d_extra_{tag});
    side_stream.write(v3d_extra_{tag} + 1);
}}
"""


def _render_stream_rate_mismatch(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""static void v3d_rate_produce(
    const int input[V3D_SIZE],
    hls::stream<int>& data_stream,
    hls::stream<int>& pace_stream) {{
#pragma HLS INLINE off
    constexpr int v3d_rate_bias_{tag} = {seed};
    for (int i = 0; i < V3D_SIZE; ++i) {{
#pragma HLS PIPELINE II=1
        data_stream.write(input[i]);
        pace_stream.write(i);
        pace_stream.write(i + v3d_rate_bias_{tag});
        pace_stream.write(i - v3d_rate_bias_{tag});
    }}
}}
"""


def _render_dependency_cycle(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""static void v3d_cycle_seed(
    hls::stream<int>& feedback_stream) {{
#pragma HLS INLINE off
    constexpr int v3d_cycle_token_{tag} = 0;
    for (int i = 0; i < V3D_SIZE; ++i) {{
        feedback_stream.write(v3d_cycle_token_{tag});
    }}
}}

static void v3d_cycle_forward(
    const int input[V3D_SIZE],
    hls::stream<int>& feedback_stream,
    hls::stream<int>& forward_stream) {{
#pragma HLS INLINE off
    for (int i = 0; i < V3D_SIZE; ++i) {{
#pragma HLS PIPELINE II=1
        const int dependency = feedback_stream.read();
        forward_stream.write(input[i] + dependency);
    }}
}}

static void v3d_cycle_consume(
    hls::stream<int>& forward_stream,
    hls::stream<int>& feedback_stream,
    int output[V3D_SIZE]) {{
#pragma HLS INLINE off
    for (int i = 0; i < V3D_SIZE; ++i) {{
#pragma HLS PIPELINE II=1
        output[i] = forward_stream.read() * 2 + 1;
        feedback_stream.write(0);
    }}
}}

void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {{
#pragma HLS DATAFLOW
    hls::stream<int> feedback_stream("feedback_stream");
    hls::stream<int> forward_stream("forward_stream");
#pragma HLS STREAM variable=feedback_stream depth=1
#pragma HLS STREAM variable=forward_stream depth=1
    v3d_cycle_seed(feedback_stream);
    v3d_cycle_forward(input, feedback_stream, forward_stream);
    v3d_cycle_consume(forward_stream, feedback_stream, output);
}}
"""


def _render_serial_reduction(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""int kernel(
    const int lhs[V3D_REDUCTION_SIZE],
    const int rhs[V3D_REDUCTION_SIZE]) {{
    int v3d_serial_sum_{tag} = 0;
    for (int i = 0; i < V3D_REDUCTION_SIZE; ++i) {{
        v3d_serial_sum_{tag} += lhs[i] * rhs[i];
    }}
    return v3d_serial_sum_{tag};
}}
"""


def _render_missing_dataflow(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return _OPT_DATAFLOW_REGION.replace(
        "#pragma HLS DATAFLOW\n", f"v3d_missing_dataflow_{tag}:\n"
    )


def _render_memory_banking_bottleneck(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""void kernel(
    const int input[V3D_BANK_INPUT_SIZE],
    int output[V3D_BANK_OUTPUT_SIZE]) {{
    int v3d_single_bank_{tag}[V3D_BANK_INPUT_SIZE];
#pragma HLS BIND_STORAGE variable=v3d_single_bank_{tag} type=ram_1p impl=bram
    for (int i = 0; i < V3D_BANK_INPUT_SIZE; ++i) {{
#pragma HLS PIPELINE II=1
        v3d_single_bank_{tag}[i] = input[i];
    }}
    for (int group = 0; group < V3D_BANK_OUTPUT_SIZE; ++group) {{
#pragma HLS PIPELINE II=1
        int sum = 0;
        for (int lane = 0; lane < 4; ++lane) {{
#pragma HLS UNROLL
            sum += v3d_single_bank_{tag}[group * 4 + lane];
        }}
        output[group] = sum;
    }}
}}
"""


def _render_transaction_latency_high(rng: random.Random, seed: int) -> str:
    tag = _tag(rng, seed)
    return f"""void kernel(
    const int input[V3D_TRANSACTION_SIZE],
    int output[V3D_TRANSACTION_SIZE]) {{
#pragma HLS INTERFACE m_axi port=input offset=slave bundle=gmem0 max_read_burst_length=1 num_read_outstanding=1
#pragma HLS INTERFACE m_axi port=output offset=slave bundle=gmem1 max_write_burst_length=1 num_write_outstanding=1
v3d_transaction_loop_{tag}:
    for (int i = 0; i < V3D_TRANSACTION_SIZE; ++i) {{
#pragma HLS PIPELINE II=1
        output[i] = input[i] * 3 + 1;
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
        _operator("repair_projection_wrong_index", "REPAIR", "csim", _PROJECTION_REGION, _render_projection_wrong_index),
        _operator("repair_prefix_omitted_term", "REPAIR", "csim", _PREFIX_SUM_REGION, _render_prefix_omitted_term),
        _operator("repair_histogram_boundary_condition", "REPAIR", "csim", _HISTOGRAM_REGION, _render_histogram_boundary_condition),
        _operator("repair_vector_add_numeric_truncation", "REPAIR", "csim", _VECTOR_ADD_REGION, _render_vector_add_numeric_truncation),
        _operator("repair_fir_wrong_branch", "REPAIR", "csim", _FIR_REGION, _render_fir_wrong_branch),
        _operator("synth_dynamic_allocation", "SYNTH_FIX", "synth", _SYNTH_REGION, _render_dynamic_allocation),
        _operator("synth_recursion", "SYNTH_FIX", "synth", _SYNTH_REGION, _render_recursion),
        _operator("synth_std_function", "SYNTH_FIX", "synth", _SYNTH_REGION, _render_std_function),
        _operator("synth_matmul_unsupported_stl", "SYNTH_FIX", "synth", _MATMUL_REGION, _render_matmul_unsupported_stl),
        _operator("synth_stencil_non_static_unbounded_loop", "SYNTH_FIX", "synth", _STENCIL_REGION, _render_stencil_non_static_unbounded_loop),
        _operator("synth_dot_hard_resource_constraint", "SYNTH_FIX", "synth", _DOT_REDUCTION_REGION, _render_dot_hard_resource_constraint),
        _operator("struct_main_burst", "STRUCTURAL_FIX", "cosim", _STRUCTURAL_REGION, _render_structural("main")),
        _operator("struct_side_burst", "STRUCTURAL_FIX", "cosim", _STRUCTURAL_REGION, _render_structural("side")),
        _operator("struct_main_chunk_3", "STRUCTURAL_FIX", "cosim", _STRUCTURAL_REGION, _render_structural("main", 3)),
        _operator("struct_stream_count_mismatch", "STRUCTURAL_FIX", "cosim", _STREAM_COUNT_REGION, _render_stream_count_mismatch),
        _operator("struct_producer_consumer_rate_mismatch", "STRUCTURAL_FIX", "cosim", _STREAM_RATE_REGION, _render_stream_rate_mismatch),
        _operator("struct_dependency_cycle", "STRUCTURAL_FIX", "cosim", _DEPENDENCY_CYCLE_REGION, _render_dependency_cycle),
        _operator("opt_serial_reduction", "OPTIMIZE", "ppa", _OPT_SERIAL_REDUCTION_REGION, _render_serial_reduction),
        _operator("opt_missing_dataflow", "OPTIMIZE", "ppa", _OPT_DATAFLOW_REGION, _render_missing_dataflow),
        _operator("opt_memory_banking_bottleneck", "OPTIMIZE", "ppa", _OPT_MEMORY_BANKING_REGION, _render_memory_banking_bottleneck),
        _operator("opt_transaction_latency_high_with_loop_ii_one", "OPTIMIZE", "ppa", _OPT_TRANSACTION_REGION, _render_transaction_latency_high),
        _operator("opt_serial_loop", "OPTIMIZE", "ppa", _OPTIMIZE_REGION, _render_optimize(pipeline_ii=None, unroll=None, partition=False)),
        _operator("opt_pipeline_ii_2", "OPTIMIZE", "ppa", _OPTIMIZE_REGION, _render_optimize(pipeline_ii=2)),
        _operator("opt_remove_unroll", "OPTIMIZE", "ppa", _OPTIMIZE_REGION, _render_optimize(unroll=None)),
        _operator("opt_serial_two_pass", "OPTIMIZE", "ppa", _OPTIMIZE_REGION, _render_serial_two_pass),
    )
}


TASK_SPECS: tuple[TaskSpec, ...] = (
    TaskSpec("v3d_fast_001", "REPAIR", "repair_off_by_one", 101, "map", 1, "循环上界遗漏最后一个元素。"),
    TaskSpec("v3d_fast_002", "REPAIR", "repair_wrong_multiplier", 102, "map", 1, "逐元素缩放系数不符合公开语义。"),
    TaskSpec("v3d_fast_003", "REPAIR", "repair_bias_sign", 103, "map", 1, "逐元素偏置方向错误。"),
    TaskSpec("v3d_fast_004", "REPAIR", "repair_projection_wrong_index", 104, "coordinate_projection", 2, "坐标投影读取了相邻点的索引。"),
    TaskSpec("v3d_fast_005", "REPAIR", "repair_prefix_omitted_term", 105, "prefix_sum", 2, "前缀和在写回后才累加当前元素。"),
    TaskSpec("v3d_fast_006", "REPAIR", "repair_histogram_boundary_condition", 106, "histogram", 2, "直方图边界条件遗漏最高合法 bin。"),
    TaskSpec("v3d_fast_007", "REPAIR", "repair_vector_add_numeric_truncation", 107, "vector_add", 2, "向量加法结果被截断为八位数值。"),
    TaskSpec("v3d_fast_008", "REPAIR", "repair_fir_wrong_branch", 108, "fir_1d", 2, "FIR 第二抽头在首个有效位置采用错误分支。"),
    TaskSpec("v3d_fast_009", "SYNTH_FIX", "synth_dynamic_allocation", 201, "synth_map", 2, "C 仿真正确，但核心路径使用动态内存。"),
    TaskSpec("v3d_fast_010", "SYNTH_FIX", "synth_recursion", 202, "synth_map", 3, "C 仿真正确，但实现依赖递归调用。"),
    TaskSpec("v3d_fast_011", "SYNTH_FIX", "synth_std_function", 203, "synth_map", 3, "C 仿真正确，但数据路径使用运行时 std::function callable。"),
    TaskSpec("v3d_fast_012", "SYNTH_FIX", "synth_matmul_unsupported_stl", 204, "matmul_4x4", 2, "矩阵乘法数据路径使用动态 std::vector 暂存。"),
    TaskSpec("v3d_fast_013", "SYNTH_FIX", "synth_stencil_non_static_unbounded_loop", 205, "stencil_2d", 3, "二维模板实现使用运行时数组维度及无静态上界 while 循环。"),
    TaskSpec("v3d_fast_014", "SYNTH_FIX", "synth_dot_hard_resource_constraint", 206, "dot_reduction", 3, "点积乘法被硬绑定到不支持的 URAM 运算实现。"),
    TaskSpec("v3d_fast_015", "STRUCTURAL_FIX", "struct_main_burst", 301, "producer_burst_deadlock", 4, "双流生产者先突发写主流，RTL 有界 FIFO 形成背压环。"),
    TaskSpec("v3d_fast_016", "STRUCTURAL_FIX", "struct_side_burst", 302, "stream_order_or_interface_mismatch", 4, "双流生产者先突发写旁路流，RTL 有界 FIFO 形成背压环。"),
    TaskSpec("v3d_fast_017", "STRUCTURAL_FIX", "struct_main_chunk_3", 303, "insufficient_fifo_depth", 4, "主流按三元素分块领先旁路流，超过 FIFO 深度。"),
    TaskSpec("v3d_fast_018", "STRUCTURAL_FIX", "struct_stream_count_mismatch", 304, "stream_count_mismatch", 4, "生产者比消费者多写两个内部流 token。"),
    TaskSpec("v3d_fast_019", "STRUCTURAL_FIX", "struct_producer_consumer_rate_mismatch", 305, "producer_consumer_rate_mismatch", 4, "生产者每周期写入三个节拍 token，而消费者只读取一个。"),
    TaskSpec("v3d_fast_020", "STRUCTURAL_FIX", "struct_dependency_cycle", 306, "dependency_cycle", 4, "数据流级之间形成有界反馈依赖环。"),
    TaskSpec("v3d_fast_021", "OPTIMIZE", "opt_serial_loop", 401, "missing_array_partition", 2, "功能正确，但核心循环没有流水、展开或数组分区并行性。"),
    TaskSpec("v3d_fast_022", "OPTIMIZE", "opt_pipeline_ii_2", 402, "low_parallel_factor", 2, "功能正确，但请求的流水间隔仍有收紧空间。"),
    TaskSpec("v3d_fast_023", "OPTIMIZE", "opt_serial_reduction", 403, "serial_reduction", 2, "点积归约被串行累加器限制。"),
    TaskSpec("v3d_fast_024", "OPTIMIZE", "opt_remove_unroll", 404, "missing_unroll", 2, "映射循环缺少展开并行性。"),
    TaskSpec("v3d_fast_025", "OPTIMIZE", "opt_missing_dataflow", 405, "missing_dataflow", 2, "两个流式计算级未启用 DATAFLOW 重叠执行。"),
    TaskSpec("v3d_fast_026", "OPTIMIZE", "opt_memory_banking_bottleneck", 406, "memory_banking_bottleneck", 3, "四路并行读取被单端口本地存储限制。"),
    TaskSpec("v3d_fast_027", "OPTIMIZE", "opt_transaction_latency_high_with_loop_ii_one", 407, "transaction_latency_high_with_loop_ii_one", 3, "循环 II 为一，但 AXI 事务被限制为单拍单 outstanding。"),
    TaskSpec("v3d_fast_028", "OPTIMIZE", "opt_serial_two_pass", 408, "suboptimal_loop_structure", 3, "功能正确，但可融合的数据路径被串行拆成两遍。", True),
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

_PROJECTION_HEADER = """#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_POINTS = 8;

struct V3DPoint3D {
    int x;
    int y;
    int z;
};

struct V3DPoint2D {
    int x;
    int y;
};

void kernel(
    const V3DPoint3D input[V3D_POINTS],
    V3DPoint2D output[V3D_POINTS]);

#endif
"""

_PREFIX_SUM_HEADER = """#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_PREFIX_SIZE = 12;

void kernel(
    const int input[V3D_PREFIX_SIZE],
    int output[V3D_PREFIX_SIZE]);

#endif
"""

_HISTOGRAM_HEADER = """#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_HIST_INPUT_SIZE = 16;
constexpr int V3D_HIST_BINS = 8;

void kernel(
    const unsigned char input[V3D_HIST_INPUT_SIZE],
    unsigned short bins[V3D_HIST_BINS]);

#endif
"""

_VECTOR_ADD_HEADER = """#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_VECTOR_SIZE = 16;

void kernel(
    const int lhs[V3D_VECTOR_SIZE],
    const int rhs[V3D_VECTOR_SIZE],
    int output[V3D_VECTOR_SIZE]);

#endif
"""

_FIR_HEADER = """#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_FIR_SIZE = 16;

void kernel(
    const int input[V3D_FIR_SIZE],
    int output[V3D_FIR_SIZE]);

#endif
"""

_MATMUL_HEADER = """#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_MATMUL_DIM = 4;

void kernel(
    const int lhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM],
    const int rhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM],
    int output[V3D_MATMUL_DIM][V3D_MATMUL_DIM]);

#endif
"""

_STENCIL_HEADER = """#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_STENCIL_MAX_ROWS = 6;
constexpr int V3D_STENCIL_MAX_COLS = 6;

void kernel(
    const int input[V3D_STENCIL_MAX_ROWS][V3D_STENCIL_MAX_COLS],
    int output[V3D_STENCIL_MAX_ROWS][V3D_STENCIL_MAX_COLS],
    int rows,
    int cols);

#endif
"""

_DOT_REDUCTION_HEADER = """#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_DOT_SIZE = 32;

int kernel(
    const int lhs[V3D_DOT_SIZE],
    const int rhs[V3D_DOT_SIZE]);

#endif
"""

_OPT_SERIAL_REDUCTION_HEADER = """#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_REDUCTION_SIZE = 64;

int kernel(
    const int lhs[V3D_REDUCTION_SIZE],
    const int rhs[V3D_REDUCTION_SIZE]);

#endif
"""

_OPT_DATAFLOW_HEADER = """#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_DATAFLOW_SIZE = 32;

void kernel(
    const int input[V3D_DATAFLOW_SIZE],
    int output[V3D_DATAFLOW_SIZE]);

#endif
"""

_OPT_MEMORY_BANKING_HEADER = """#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_BANK_INPUT_SIZE = 64;
constexpr int V3D_BANK_OUTPUT_SIZE = 16;

void kernel(
    const int input[V3D_BANK_INPUT_SIZE],
    int output[V3D_BANK_OUTPUT_SIZE]);

#endif
"""

_OPT_TRANSACTION_HEADER = """#ifndef V3D_KERNEL_H
#define V3D_KERNEL_H

constexpr int V3D_TRANSACTION_SIZE = 64;

void kernel(
    const int input[V3D_TRANSACTION_SIZE],
    int output[V3D_TRANSACTION_SIZE]);

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
    if family in _ANCHOR_DUAL_STREAM_FAMILIES:
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
    if family == "stream_count_mismatch":
        return (
            '#include "kernel.h"\n\n'
            f"{_BEGIN_MARKER}\n"
            + _STREAM_COUNT_REGION
            + f"{_END_MARKER}\n\n"
            "static void v3d_count_consume(\n"
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
            "    v3d_count_produce(input, main_stream, side_stream);\n"
            "    v3d_count_consume(main_stream, side_stream, output);\n"
            "}\n"
        )
    if family == "producer_consumer_rate_mismatch":
        return (
            '#include "kernel.h"\n\n'
            f"{_BEGIN_MARKER}\n"
            + _STREAM_RATE_REGION
            + f"{_END_MARKER}\n\n"
            "static void v3d_rate_consume(\n"
            "    hls::stream<int>& data_stream,\n"
            "    hls::stream<int>& pace_stream,\n"
            "    int output[V3D_SIZE]) {\n"
            "#pragma HLS INLINE off\n"
            "    for (int i = 0; i < V3D_SIZE; ++i) {\n"
            "#pragma HLS PIPELINE II=1\n"
            "        const int value = data_stream.read();\n"
            "        (void)pace_stream.read();\n"
            "        output[i] = value * 2 + 1;\n"
            "    }\n"
            "}\n\n"
            "void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {\n"
            "#pragma HLS DATAFLOW\n"
            '    hls::stream<int> data_stream("data_stream");\n'
            '    hls::stream<int> pace_stream("pace_stream");\n'
            "#pragma HLS STREAM variable=data_stream depth=2\n"
            "#pragma HLS STREAM variable=pace_stream depth=2\n"
            "    v3d_rate_produce(input, data_stream, pace_stream);\n"
            "    v3d_rate_consume(data_stream, pace_stream, output);\n"
            "}\n"
        )
    if family == "dependency_cycle":
        return (
            '#include "kernel.h"\n\n'
            f"{_BEGIN_MARKER}\n"
            + _DEPENDENCY_CYCLE_REGION
            + f"{_END_MARKER}\n"
        )
    if family in _OPT_MAP_FAMILIES:
        return (
            '#include "kernel.h"\n\n'
            "void kernel(const int input[V3D_SIZE], int output[V3D_SIZE]) {\n"
            f"    {_BEGIN_MARKER}\n"
            + _OPTIMIZE_REGION
            + f"    {_END_MARKER}\n"
            "}\n"
        )
    body_families = {
        "coordinate_projection": (
            "void kernel(\n"
            "    const V3DPoint3D input[V3D_POINTS],\n"
            "    V3DPoint2D output[V3D_POINTS]) {\n",
            _PROJECTION_REGION,
        ),
        "prefix_sum": (
            "void kernel(\n"
            "    const int input[V3D_PREFIX_SIZE],\n"
            "    int output[V3D_PREFIX_SIZE]) {\n",
            _PREFIX_SUM_REGION,
        ),
        "histogram": (
            "void kernel(\n"
            "    const unsigned char input[V3D_HIST_INPUT_SIZE],\n"
            "    unsigned short bins[V3D_HIST_BINS]) {\n",
            _HISTOGRAM_REGION,
        ),
        "vector_add": (
            "void kernel(\n"
            "    const int lhs[V3D_VECTOR_SIZE],\n"
            "    const int rhs[V3D_VECTOR_SIZE],\n"
            "    int output[V3D_VECTOR_SIZE]) {\n",
            _VECTOR_ADD_REGION,
        ),
        "fir_1d": (
            "void kernel(\n"
            "    const int input[V3D_FIR_SIZE],\n"
            "    int output[V3D_FIR_SIZE]) {\n",
            _FIR_REGION,
        ),
    }
    if family in body_families:
        declaration, region = body_families[family]
        return (
            '#include "kernel.h"\n\n'
            + declaration
            + f"    {_BEGIN_MARKER}\n"
            + region
            + f"    {_END_MARKER}\n"
            "}\n"
        )
    whole_function_families = {
        "matmul_4x4": _MATMUL_REGION,
        "stencil_2d": _STENCIL_REGION,
        "dot_reduction": _DOT_REDUCTION_REGION,
        "serial_reduction": _OPT_SERIAL_REDUCTION_REGION,
        "missing_dataflow": _OPT_DATAFLOW_REGION,
        "memory_banking_bottleneck": _OPT_MEMORY_BANKING_REGION,
        "transaction_latency_high_with_loop_ii_one": _OPT_TRANSACTION_REGION,
    }
    if family in whole_function_families:
        return (
            '#include "kernel.h"\n\n'
            f"{_BEGIN_MARKER}\n"
            + whole_function_families[family]
            + f"{_END_MARKER}\n"
        )
    raise V3DCorpusError(f"unknown task family: {family}")


def _header(family: str) -> str:
    headers = {
        "coordinate_projection": _PROJECTION_HEADER,
        "prefix_sum": _PREFIX_SUM_HEADER,
        "histogram": _HISTOGRAM_HEADER,
        "vector_add": _VECTOR_ADD_HEADER,
        "fir_1d": _FIR_HEADER,
        "matmul_4x4": _MATMUL_HEADER,
        "stencil_2d": _STENCIL_HEADER,
        "dot_reduction": _DOT_REDUCTION_HEADER,
        "serial_reduction": _OPT_SERIAL_REDUCTION_HEADER,
        "missing_dataflow": _OPT_DATAFLOW_HEADER,
        "memory_banking_bottleneck": _OPT_MEMORY_BANKING_HEADER,
        "transaction_latency_high_with_loop_ii_one": _OPT_TRANSACTION_HEADER,
    }
    if family in headers:
        return headers[family]
    return _STRUCTURAL_HEADER if family in _STREAM_FAMILIES else _COMMON_HEADER


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


def _family_testbench(family: str, *, hidden_like: bool) -> str:
    label = "hidden-like" if hidden_like else "public"
    if family == "coordinate_projection":
        values = (
            "{17, -9, 4}, {-6, 31, -2}, {8, 5, 99}, {0, -12, 7}, "
            "{43, 2, -8}, {-21, -17, 3}, {9, 28, 6}, {3, -4, 11}"
            if hidden_like
            else "{2, -3, 9}, {-5, 7, 4}, {11, 13, -8}, {0, 4, 6}, "
            "{-9, -2, 5}, {8, 1, 3}, {6, -7, 2}, {15, 10, -1}"
        )
        return f"""#include "kernel.h"

#include <iostream>

int main() {{
    const V3DPoint3D input[V3D_POINTS] = {{{values}}};
    V3DPoint2D output[V3D_POINTS] = {{}};
    kernel(input, output);
    for (int i = 0; i < V3D_POINTS; ++i) {{
        if (output[i].x != input[i].x || output[i].y != input[i].y) {{
            std::cerr << "{label} projection mismatch at " << i << "\\n";
            return 1;
        }}
    }}
    return 0;
}}
"""
    if family == "prefix_sum":
        values = (
            "-7, 4, 12, -3, 9, 1, -8, 6, 5, -2, 11, -4"
            if hidden_like
            else "3, -1, 4, 2, -2, 5, 0, 7, -3, 1, 6, -4"
        )
        return f"""#include "kernel.h"

#include <iostream>

int main() {{
    const int input[V3D_PREFIX_SIZE] = {{{values}}};
    int output[V3D_PREFIX_SIZE] = {{}};
    kernel(input, output);
    int expected = 0;
    for (int i = 0; i < V3D_PREFIX_SIZE; ++i) {{
        expected += input[i];
        if (output[i] != expected) {{
            std::cerr << "{label} prefix mismatch at " << i << "\\n";
            return 1;
        }}
    }}
    return 0;
}}
"""
    if family == "histogram":
        values = (
            "7, 6, 5, 4, 3, 2, 1, 0, 7, 7, 4, 4, 2, 6, 1, 7"
            if hidden_like
            else "0, 7, 1, 7, 2, 6, 3, 5, 4, 7, 0, 2, 4, 6, 7, 1"
        )
        return f"""#include "kernel.h"

#include <iostream>

int main() {{
    const unsigned char input[V3D_HIST_INPUT_SIZE] = {{{values}}};
    unsigned short bins[V3D_HIST_BINS] = {{}};
    unsigned short expected[V3D_HIST_BINS] = {{}};
    for (int i = 0; i < V3D_HIST_INPUT_SIZE; ++i) {{
        ++expected[input[i]];
    }}
    kernel(input, bins);
    for (int bin = 0; bin < V3D_HIST_BINS; ++bin) {{
        if (bins[bin] != expected[bin]) {{
            std::cerr << "{label} histogram mismatch at " << bin << "\\n";
            return 1;
        }}
    }}
    return 0;
}}
"""
    if family == "vector_add":
        lhs = (
            "400, -90, 17, 999, -301, 88, 240, 1, 73, -44, 512, 6, 27, 360, -8, 19"
            if hidden_like
            else "250, -20, 100, 1000, -300, 7, 128, 42, 15, -80, 511, 3, 64, 201, -5, 9"
        )
        rhs = (
            "30, 12, -8, 2, 44, 190, 31, -5, 184, 90, -12, 300, -40, 7, 260, -22"
            if hidden_like
            else "10, 5, 200, -3, 45, 255, 130, -8, 260, 100, 2, 300, -70, 80, 270, -12"
        )
        return f"""#include "kernel.h"

#include <iostream>

int main() {{
    const int lhs[V3D_VECTOR_SIZE] = {{{lhs}}};
    const int rhs[V3D_VECTOR_SIZE] = {{{rhs}}};
    int output[V3D_VECTOR_SIZE] = {{}};
    kernel(lhs, rhs, output);
    for (int i = 0; i < V3D_VECTOR_SIZE; ++i) {{
        const int expected = lhs[i] + rhs[i];
        if (output[i] != expected) {{
            std::cerr << "{label} vector-add mismatch at " << i << "\\n";
            return 1;
        }}
    }}
    return 0;
}}
"""
    if family == "fir_1d":
        values = (
            "-4, 9, 1, -7, 5, 3, 12, -2, 8, 6, -5, 10, 2, -1, 7, 4"
            if hidden_like
            else "3, 1, -2, 4, 0, 5, -1, 2, 7, -3, 6, 8, -4, 9, 2, -5"
        )
        return f"""#include "kernel.h"

#include <iostream>

int main() {{
    const int input[V3D_FIR_SIZE] = {{{values}}};
    int output[V3D_FIR_SIZE] = {{}};
    kernel(input, output);
    for (int i = 0; i < V3D_FIR_SIZE; ++i) {{
        int expected = input[i];
        if (i >= 1) expected += 2 * input[i - 1];
        if (i >= 2) expected += input[i - 2];
        if (output[i] != expected) {{
            std::cerr << "{label} FIR mismatch at " << i << "\\n";
            return 1;
        }}
    }}
    return 0;
}}
"""
    if family == "matmul_4x4":
        lhs = (
            "{2, -1, 4, 3}, {0, 5, -2, 1}, {7, 2, 1, -3}, {4, 0, 6, 2}"
            if hidden_like
            else "{1, 2, 3, 4}, {-2, 0, 5, 1}, {3, -1, 2, 6}, {4, 2, 0, -3}"
        )
        rhs = (
            "{1, 3, 0, -2}, {4, -1, 2, 5}, {-3, 6, 1, 0}, {2, 4, -5, 3}"
            if hidden_like
            else "{2, -1, 0, 3}, {4, 5, -2, 1}, {1, 0, 6, -4}, {-3, 2, 1, 5}"
        )
        return f"""#include "kernel.h"

#include <iostream>

int main() {{
    const int lhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM] = {{{lhs}}};
    const int rhs[V3D_MATMUL_DIM][V3D_MATMUL_DIM] = {{{rhs}}};
    int output[V3D_MATMUL_DIM][V3D_MATMUL_DIM] = {{}};
    kernel(lhs, rhs, output);
    for (int row = 0; row < V3D_MATMUL_DIM; ++row) {{
        for (int col = 0; col < V3D_MATMUL_DIM; ++col) {{
            int expected = 0;
            for (int inner = 0; inner < V3D_MATMUL_DIM; ++inner) {{
                expected += lhs[row][inner] * rhs[inner][col];
            }}
            if (output[row][col] != expected) {{
                std::cerr << "{label} matmul mismatch at "
                          << row << "," << col << "\\n";
                return 1;
            }}
        }}
    }}
    return 0;
}}
"""
    if family == "stencil_2d":
        rows, cols, row_scale, col_scale, bias = (
            (4, 6, -5, 3, 11) if hidden_like else (6, 5, 7, -3, 2)
        )
        return f"""#include "kernel.h"

#include <iostream>

int main() {{
    int input[V3D_STENCIL_MAX_ROWS][V3D_STENCIL_MAX_COLS] = {{}};
    int output[V3D_STENCIL_MAX_ROWS][V3D_STENCIL_MAX_COLS] = {{}};
    for (int row = 0; row < V3D_STENCIL_MAX_ROWS; ++row) {{
        for (int col = 0; col < V3D_STENCIL_MAX_COLS; ++col) {{
            input[row][col] = row * {row_scale} + col * {col_scale} + {bias};
        }}
    }}
    constexpr int rows = {rows};
    constexpr int cols = {cols};
    kernel(input, output, rows, cols);
    for (int row = 0; row < V3D_STENCIL_MAX_ROWS; ++row) {{
        for (int col = 0; col < V3D_STENCIL_MAX_COLS; ++col) {{
            int expected = 0;
            if (row > 0 && row < rows - 1 && col > 0 && col < cols - 1) {{
                expected = input[row][col]
                    + input[row - 1][col]
                    + input[row + 1][col]
                    + input[row][col - 1]
                    + input[row][col + 1];
            }}
            if (output[row][col] != expected) {{
                std::cerr << "{label} stencil mismatch at "
                          << row << "," << col << "\\n";
                return 1;
            }}
        }}
    }}
    return 0;
}}
"""
    if family == "dot_reduction":
        lhs_formula, rhs_formula = (
            ("(i % 9) - 4", "((i * 5) % 13) - 6")
            if hidden_like
            else ("(i % 7) - 3", "((i * 3) % 11) - 5")
        )
        return f"""#include "kernel.h"

#include <iostream>

int main() {{
    int lhs[V3D_DOT_SIZE] = {{}};
    int rhs[V3D_DOT_SIZE] = {{}};
    int expected = 0;
    for (int i = 0; i < V3D_DOT_SIZE; ++i) {{
        lhs[i] = {lhs_formula};
        rhs[i] = {rhs_formula};
        expected += lhs[i] * rhs[i];
    }}
    const int actual = kernel(lhs, rhs);
    if (actual != expected) {{
        std::cerr << "{label} dot-product mismatch: expected " << expected
                  << ", got " << actual << "\\n";
        return 1;
    }}
    return 0;
}}
"""
    if family == "serial_reduction":
        lhs_formula, rhs_formula = (
            ("(i % 11) - 5", "((i * 7) % 17) - 8")
            if hidden_like
            else ("(i % 9) - 4", "((i * 5) % 13) - 6")
        )
        return f"""#include "kernel.h"

#include <iostream>

int main() {{
    int lhs[V3D_REDUCTION_SIZE] = {{}};
    int rhs[V3D_REDUCTION_SIZE] = {{}};
    int expected = 0;
    for (int i = 0; i < V3D_REDUCTION_SIZE; ++i) {{
        lhs[i] = {lhs_formula};
        rhs[i] = {rhs_formula};
        expected += lhs[i] * rhs[i];
    }}
    const int actual = kernel(lhs, rhs);
    if (actual != expected) {{
        std::cerr << "{label} reduction mismatch: expected " << expected
                  << ", got " << actual << "\\n";
        return 1;
    }}
    return 0;
}}
"""
    if family == "missing_dataflow":
        formula = "i * 7 - 19" if hidden_like else "i * 3 - 11"
        return f"""#include "kernel.h"

#include <iostream>

int main() {{
    int input[V3D_DATAFLOW_SIZE] = {{}};
    int output[V3D_DATAFLOW_SIZE] = {{}};
    for (int i = 0; i < V3D_DATAFLOW_SIZE; ++i) input[i] = {formula};
    kernel(input, output);
    for (int i = 0; i < V3D_DATAFLOW_SIZE; ++i) {{
        const int expected = input[i] * 5 - 3;
        if (output[i] != expected) {{
            std::cerr << "{label} dataflow mismatch at " << i << "\\n";
            return 1;
        }}
    }}
    return 0;
}}
"""
    if family == "memory_banking_bottleneck":
        formula = "(i * 11) % 37 - 18" if hidden_like else "(i * 7) % 29 - 14"
        return f"""#include "kernel.h"

#include <iostream>

int main() {{
    int input[V3D_BANK_INPUT_SIZE] = {{}};
    int output[V3D_BANK_OUTPUT_SIZE] = {{}};
    for (int i = 0; i < V3D_BANK_INPUT_SIZE; ++i) input[i] = {formula};
    kernel(input, output);
    for (int group = 0; group < V3D_BANK_OUTPUT_SIZE; ++group) {{
        int expected = 0;
        for (int lane = 0; lane < 4; ++lane) {{
            expected += input[group * 4 + lane];
        }}
        if (output[group] != expected) {{
            std::cerr << "{label} banking mismatch at " << group << "\\n";
            return 1;
        }}
    }}
    return 0;
}}
"""
    if family == "transaction_latency_high_with_loop_ii_one":
        formula = "i * 13 - 101" if hidden_like else "i * 5 - 47"
        return f"""#include "kernel.h"

#include <iostream>

int main() {{
    int input[V3D_TRANSACTION_SIZE] = {{}};
    int output[V3D_TRANSACTION_SIZE] = {{}};
    for (int i = 0; i < V3D_TRANSACTION_SIZE; ++i) input[i] = {formula};
    kernel(input, output);
    for (int i = 0; i < V3D_TRANSACTION_SIZE; ++i) {{
        const int expected = input[i] * 3 + 1;
        if (output[i] != expected) {{
            std::cerr << "{label} transaction mismatch at " << i << "\\n";
            return 1;
        }}
    }}
    return 0;
}}
"""
    return _testbench(
        structural=family in _STREAM_FAMILIES, hidden_like=hidden_like
    )


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
        if spec.family in _STREAM_FAMILIES
        else "output[i] = input[i] * 3 + 7"
    )


def _public_contract(spec: TaskSpec) -> str:
    contracts = {
        "coordinate_projection": (
            "对每个 0 <= i < V3D_POINTS，输出二维点必须满足 "
            "output[i].x = input[i].x 且 output[i].y = input[i].y；"
            "V3D_POINTS 固定为 8，z 坐标不参与结果，合法输入坐标范围为 "
            "[-1000000, 1000000]。"
        ),
        "prefix_sum": (
            "对每个 0 <= i < V3D_PREFIX_SIZE，output[i] 必须等于 "
            "input[0] 到 input[i] 的包含当前元素的前缀和；"
            "V3D_PREFIX_SIZE 固定为 12，合法输入元素范围为 [-1000000, 1000000]。"
        ),
        "histogram": (
            "先清零全部输出 bin，再对每个 0 <= b < V3D_HIST_BINS 令 bins[b] "
            "等于输入中数值 b 的出现次数；V3D_HIST_INPUT_SIZE 固定为 16，"
            "V3D_HIST_BINS 固定为 8，合法输入元素范围为 [0, 7]。"
        ),
        "vector_add": (
            "对每个 0 <= i < V3D_VECTOR_SIZE，必须满足 "
            "output[i] = lhs[i] + rhs[i]；V3D_VECTOR_SIZE 固定为 16，"
            "两路合法输入元素范围均为 [-1000000, 1000000]。"
        ),
        "fir_1d": (
            "计算因果三抽头一维卷积，系数依次为 [1, 2, 1]，数组左侧按零填充；"
            "即 output[i] = input[i] + 2*input[i-1] + input[i-2]，"
            "负下标项取 0。V3D_FIR_SIZE 固定为 16，合法输入元素范围为 "
            "[-1000000, 1000000]。"
        ),
        "matmul_4x4": (
            "计算两个 4x4 整数矩阵的乘积；对每个 row、col，output[row][col] "
            "必须等于 lhs[row][k] * rhs[k][col] 在 k=0..3 上的总和。"
            "合法输入元素范围为 [-10000, 10000]。"
        ),
        "stencil_2d": (
            "rows 和 cols 的合法范围均为 [3, 6]；活动矩形内部元素输出自身及上下左右"
            "五点之和，活动矩形边界及矩形外的全部输出必须为 0。"
            "合法输入元素范围为 [-1000000, 1000000]。"
        ),
        "dot_reduction": (
            "返回两路长度为 V3D_DOT_SIZE 的整数向量点积，即 lhs[i] * rhs[i] "
            "在全部 i 上的总和；V3D_DOT_SIZE 固定为 32，合法输入元素范围为 "
            "[-10000, 10000]。"
        ),
        "serial_reduction": (
            "返回两路长度为 V3D_REDUCTION_SIZE 的整数向量点积，即 lhs[i] * rhs[i] "
            "在全部 i 上的总和；V3D_REDUCTION_SIZE 固定为 64，合法输入元素范围为 "
            "[-10000, 10000]。"
        ),
        "missing_dataflow": (
            "对每个 0 <= i < V3D_DATAFLOW_SIZE，必须满足 "
            "output[i] = input[i] * 5 - 3；V3D_DATAFLOW_SIZE 固定为 32，"
            "合法输入元素范围为 [-1000000, 1000000]。"
        ),
        "memory_banking_bottleneck": (
            "把长度 64 的输入按连续四元素分组；对每个 0 <= group < 16，"
            "output[group] 必须等于 input[4*group] 到 input[4*group+3] 的总和。"
            "合法输入元素范围为 [-1000000, 1000000]。"
        ),
        "transaction_latency_high_with_loop_ii_one": (
            "对每个 0 <= i < V3D_TRANSACTION_SIZE，必须满足 "
            "output[i] = input[i] * 3 + 1；V3D_TRANSACTION_SIZE 固定为 64，"
            "合法输入元素范围为 [-1000000, 1000000]。"
        ),
    }
    if spec.family in contracts:
        return contracts[spec.family]
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
    interfaces = {
        "coordinate_projection": (
            "`kernel(const V3DPoint3D input[8], V3DPoint2D output[8])`",
            "必须处理全部 8 个点；不得改变点的顺序或读写数组边界之外的数据。",
        ),
        "prefix_sum": (
            "`kernel(const int input[12], int output[12])`",
            "必须生成包含当前元素的全部 12 个前缀结果。",
        ),
        "histogram": (
            "`kernel(const unsigned char input[16], unsigned short bins[8])`",
            "每次调用都必须覆盖全部 8 个 bin，不能依赖输出数组的初值。",
        ),
        "vector_add": (
            "`kernel(const int lhs[16], const int rhs[16], int output[16])`",
            "必须以整数精度处理全部 16 个元素，不得缩窄中间结果。",
        ),
        "fir_1d": (
            "`kernel(const int input[16], int output[16])`",
            "必须处理全部 16 个输出，并按公开零填充规则处理左边界。",
        ),
        "matmul_4x4": (
            "`kernel(const int lhs[4][4], const int rhs[4][4], int output[4][4])`",
            "必须写出全部 16 个矩阵元素，矩阵维度固定为 4。",
        ),
        "stencil_2d": (
            "`kernel(const int input[6][6], int output[6][6], int rows, int cols)`",
            "数组容量固定为 6x6；必须定义活动矩形外以及边界位置的输出。",
        ),
        "dot_reduction": (
            "`int kernel(const int lhs[32], const int rhs[32])`",
            "必须归约全部 32 对输入元素并返回一个整数结果。",
        ),
        "serial_reduction": (
            "`int kernel(const int lhs[64], const int rhs[64])`",
            "必须归约全部 64 对输入元素并返回一个整数结果。",
        ),
        "missing_dataflow": (
            "`kernel(const int input[32], int output[32])`",
            "必须处理并覆盖全部 32 个输出元素。",
        ),
        "memory_banking_bottleneck": (
            "`kernel(const int input[64], int output[16])`",
            "必须处理全部 16 组输入并覆盖全部输出元素。",
        ),
        "transaction_latency_high_with_loop_ii_one": (
            "`kernel(const int input[64], int output[64])`",
            "必须处理并覆盖全部 64 个输出元素。",
        ),
    }
    if spec.family in interfaces:
        signature, constraint = interfaces[spec.family]
        return f"""# V3-D 公开 kernel 任务

## 功能语义

{_public_contract(spec)}

## 接口与约束

- 顶层函数必须保持为 {signature}。
- {constraint}
- 只允许修改 `kernel.cpp`；header、公开 testbench 和任务元数据均为只读。
- 公开 testbench 只给出示例，提交实现必须满足上述全部合法输入。
"""
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
                            "family", "difficulty",
                            "mutation_manifest_sha256", "acceptance_sha256",
                        ],
                        "properties": {
                            "task_id": {"type": "string", "pattern": "^v3d_fast_[0-9]{3}$"},
                            "path": {"type": "string", "pattern": "^tasks/[A-Za-z0-9_+-]+$"},
                            "mode": {"enum": list(PHASE_MODES)},
                            "operator": {"type": "string", "minLength": 1},
                            "seed": {"type": "integer", "minimum": 0},
                            "family": {
                                "type": "string",
                                "pattern": "^[a-z][a-z0-9_]*$",
                            },
                            "difficulty": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 5,
                            },
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
        "family", "difficulty",
        "mutation_manifest_sha256", "acceptance_sha256",
    }
    specs_by_id = {spec.task_id: spec for spec in TASK_SPECS}
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
        family = task.get("family")
        difficulty = task.get("difficulty")
        if (
            not isinstance(family, str)
            or not family
            or isinstance(difficulty, bool)
            or not isinstance(difficulty, int)
            or not 1 <= difficulty <= 5
        ):
            raise V3DCorpusError("corpus task family or difficulty is invalid")
        spec = specs_by_id.get(str(task_id))
        if (
            spec is None
            or family != spec.family
            or difficulty != spec.difficulty
        ):
            raise V3DCorpusError(
                "corpus task family or difficulty conflicts with its task spec"
            )
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
                f"{prefix}/kernel_tb.cpp": _family_testbench(
                    spec.family, hidden_like=False
                ).encode("utf-8"),
                f"{prefix}/hidden_like/kernel_tb.cpp": _family_testbench(
                    spec.family, hidden_like=True
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
                "family": spec.family,
                "difficulty": spec.difficulty,
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
