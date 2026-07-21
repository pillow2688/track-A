# V3-D XSIM 专项恢复：A03

证据等级：`REAL_VITIS_2025_2_NO_LLM`。本次只验证 corpus Oracle 的
baseline/golden 门链，不是 Agent、LLM 或 hidden grader 成绩。

## 结论

使用全新 `v3d-oracle-vitis-probed-anchors-a03` 目录、从零生成每题的
`xsim.dir`，并在非沙箱环境串行重跑后，`015/016/017/028` 全部
`ACCEPTED`：4 个真实 Vitis anchors，0 rejected，总耗时 328.602 秒。

历史成功批与 A01/A02 失败批的 kernel、testbench、生成 RTL、HLS Tcl、
XSIM Tcl、工具版本和调用命令均一致。A01/A02 的异常发生在 xelab 已构建
snapshot 之后、XSIM 执行第一条 Tcl 时，RTL 仿真尚未开始。A03 中相同
snapshot 能进入 `Time resolution is 1 ps`、UVM 和 RTL transaction。

因此现有证据排除了 kernel、testbench、RTL 和 Tcl 差异；最合理的边界是
XSIM 运行时/执行隔离的瞬态异常。A03 证明“全新本地 simulator work +
非沙箱串行执行”能够恢复，但不能仅凭本次结果把根因进一步断言为某一个
具体缓存文件。

## 逐题结果

| Task | Mode | Baseline | Golden | PPA | Status | Wall time |
| --- | --- | --- | --- | --- | --- | ---: |
| `v3d_fast_015` | STRUCTURAL_FIX | 真实 FIFO deadlock | CoSim PASS | N/A | ACCEPTED | 82.012 s |
| `v3d_fast_016` | STRUCTURAL_FIX | 真实 FIFO deadlock | CoSim PASS | N/A | ACCEPTED | 82.577 s |
| `v3d_fast_017` | STRUCTURAL_FIX | 真实 FIFO deadlock | CoSim PASS | N/A | ACCEPTED | 82.488 s |
| `v3d_fast_028` | OPTIMIZE | CSim/Synth/CoSim PASS | CSim/Synth/CoSim PASS | 39 → 6 cycles，6.5× | ACCEPTED | 81.459 s |

三道 structural baseline 的日志都包含真实 `DEADLOCK DETECTED` 和 FIFO
阻塞关系；它们不再把 XSIM 启动异常误记为期望中的 CoSim FAIL。028 的
baseline PPA gate 为 FAIL、golden PPA gate 为 PASS，与任务定义一致。

## 缓存与证据处理

- `~/.Xilinx/xsim` 和 `~/.Xilinx/vitis_hls` 在运行前均为空，没有全局文件可清。
- A01/A02 的本地 `xsim.dir` 没有删除，因为它们属于失败证据。
- A03 使用全新 output/attempt/work 目录，所有 simulator 中间文件重新生成。
- A03 日志中 `unexpected exception when evaluating tcl command` 命中数为 0。

脱敏快照：
`docs/submission/oracle_snapshots/v3d-oracle-vitis-probed-anchors-a03.json`。
原始 run 位于 ignored `llm4hls_harness/runs/`，不进入提交 staging。
