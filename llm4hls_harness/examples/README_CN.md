[English](README.md)

# HLS 示例

`u55c_smoke_task/` 是一个很小的公开任务包，用于检查本机 Vitis 2025.2
是否能以 Alveo U55C 器件 part 完成 HLS 验证。它只是环境 smoke test，不是比赛性能基准。

请在 `vitis-2025-2` Distrobox 容器中运行：

```bash
cd /home/ying/CompetitionTrackA/track-A/llm4hls_harness
export LLM4HLS_VITIS_HLS_ROOT=/home/ying/CompetitionTrackA/vitis/AMD/2025.2/Vitis
export LLM4HLS_PART=xcu55c-fsvh2892-2L-e
python3 -m llm4hls_agent run examples/u55c_smoke_task \
  --run-dir runs/u55c-smoke-100mhz \
  --clock-ns 10.0 \
  --minimum-frequency-mhz 100 \
  --csim-timeout 180 --synth-timeout 900 --cosim-timeout 900
```

命令最终应输出 `status: DONE`。综合报告和日志会保存在 run 目录中；Vitis
生成文件已被 Git 忽略。

V1 公开 fixture 包括：

- `u55c_repair_task/`：baseline public csim 功能 mismatch；
- `u55c_compile_repair_task/`：未声明标识符和 baseline 编译错误；
- `u55c_synthesis_repair_task/`：csim 通过，但动态分配导致 HLS 综合失败；
- `u55c_patch_invalid.diff`：只用于安全验收的确定性越权 testbench Patch。

前三项是三类不同的 HLS 任务错误；`PATCH_INVALID` 是独立的平台安全负例，不能
写成第四类 HLS 错误。OpenAI-compatible 修复和统一验收命令见项目 README。
