# V3-D fail-closed Vitis probe 后的 anchor 重跑

证据等级：`REAL_VITIS_2025_2_NO_LLM`。这不是 Agent、LLM 或 hidden grader
成绩，只是 corpus baseline/golden 门链的真实 Vitis 重跑。

## 结果

| Attempt | Tasks | Accepted | Rejected | Wall time |
| --- | ---: | ---: | ---: | ---: |
| `v3d-oracle-vitis-probed-anchors-a01` | 12 | 8 | 4 | 498.944 s |
| `v3d-oracle-vitis-probed-anchors-a02` | 4 | 0 | 4 | 314.963 s |

A01 接受：`v3d_fast_001/002/003/009/010/011/021/022`，覆盖 REPAIR、
SYNTH_FIX 和 OPTIMIZE。它们均由新版 backend v4 实际执行
`vitis-run --version`，确认 2025.2，并把 Vitis 可执行文件 SHA-256
`4d1bf955...91fb3` 纳入 backend fingerprint。

## 重复失败

`v3d_fast_015/016/017`（STRUCTURAL_FIX）和 `v3d_fast_028`（需要 CoSim
的 OPTIMIZE）在 A01 失败后又用全新 A02 目录重跑。两次都表现为：

- baseline/golden CSim、Synth 到达预期；
- XSIM 启动 RTL 仿真时报告 `unexpected exception when evaluating tcl command`；
- Vitis 随后报告 `COSIM 212-4 ... FAIL`；
- Oracle fail-closed，将任务标记为 REJECTED。

因此本次不能把这 4 题计作新版探针的有效 anchor，也不能把异常写成
kernel 功能错误。旧的 12-anchor receipt bundle 继续作为独立历史证据，
其 backend fingerprint 仍是旧版；本文件没有追溯篡改旧证据。

脱敏 snapshots：

- `docs/submission/oracle_snapshots/v3d-oracle-vitis-probed-anchors-a01.json`
- `docs/submission/oracle_snapshots/v3d-oracle-vitis-probed-anchors-a02.json`

原始 run 保留在本机 ignored `runs/`，未进入提交 staging；JSON release
保存 source summary 与两次失败 record 的 SHA-256。
