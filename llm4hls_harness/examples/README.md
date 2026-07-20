[简体中文](README_CN.md)

# HLS examples

`u55c_smoke_task/` is a small public task package for checking the local Vitis
2025.2 HLS path against the Alveo U55C device part. It is an environment smoke
test, not a competition benchmark.

Run it from the `vitis-2025-2` Distrobox container:

```bash
PROJECT_ROOT=/absolute/path/to/track-A
VITIS_ROOT=/absolute/path/to/AMD/2025.2/Vitis
cd "$PROJECT_ROOT/llm4hls_harness"
export LLM4HLS_VITIS_HLS_ROOT="$VITIS_ROOT"
export LLM4HLS_PART=xcu55c-fsvh2892-2L-e
python3 -m llm4hls_agent run examples/u55c_smoke_task \
  --run-dir runs/u55c-smoke-100mhz \
  --clock-ns 10.0 \
  --minimum-frequency-mhz 100 \
  --csim-timeout 180 --synth-timeout 900 --cosim-timeout 900
```

The command must produce `status: DONE`. Reports and logs are written below
the run directory; generated Vitis files are ignored by Git.

The V1 public fixtures are:

- `u55c_repair_task/`: baseline public csim functional mismatch;
- `u55c_compile_repair_task/`: undeclared identifier and baseline compile error;
- `u55c_synthesis_repair_task/`: csim passes but dynamic allocation fails HLS synthesis;
- `u55c_patch_invalid.diff`: deterministic forbidden testbench patch used only for safety acceptance.

The first three are distinct HLS task errors. `PATCH_INVALID` is an independent
platform safety case, not a fourth HLS error. See the project README for
OpenAI-compatible repair and unified acceptance commands.
