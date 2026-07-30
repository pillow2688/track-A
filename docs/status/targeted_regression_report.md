# 定向回归状态

目标任务是 `v3d_fast_012`、`016`、`017`、`020` 以及四个跨 Mode 成功哨兵；协议仍规定每个历史失败任务最多两次 fresh run、串行、无 Resume、无人工中途改代码。

本阶段**没有启动** DeepSeek、Vitis HLS 或任何 fresh 公共任务。原因不是模型失败：当前 A2 Enforce Admission 绑定 RC1，而当前产品已是 RC2，运行前 preflight 必须 fail-closed。强行关闭 A2 或伪造 Admission 会破坏本 Goal 的验收口径。

已完成的本地替代验证包括：hunk 机械恢复路径、任务路由 smoke、STRUCTURAL_FIX 证据、终态封存、Candidate 回退、A2/A3 关闭隔离和批次 CoSim 归类。获得新的冻结 commit 与有效 Admission 后，才可按预定协议执行真实定向回归。
