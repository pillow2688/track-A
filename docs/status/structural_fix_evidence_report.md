# STRUCTURAL_FIX 证据增强记录

新增的静态分析只读取当前公开 kernel 源码，输出保守的 `SOURCE_PATTERN` 事实：DATAFLOW pragma、stream 名称、读/写次数、producer/consumer process 数量、声明 depth、可能的多生产者/多消费者、简单依赖边和环、固定上界循环计数。无法可靠解析时写 `UNKNOWN`，不伪造完整 C++/RTL 图。

CoSim failure evidence 现在额外记录 `cosim_progress`、`no_progress_seconds`、
`xsim_started`、`runtime_stage`、`transaction_progress`、`log_growth` 与
`output_growth`。只有日志明确出现 no-progress 时才填时长；普通 timeout 不会被
误报为 deadlock。结构 Evidence 还将这些运行时事实与静态 stream 拓扑分别保存，
避免把“启动了 xsim”错误等同于“发现了死锁”。

同时修正批次汇总：若终态有 `v3c.cosim-failure-evidence.v1`，即使随后 stop reason 是 `TASK_REPAIR_NO_IMPROVEMENT_LIMIT`、最后通用 phase 是 `timeout`，失败阶段仍归类为 `COSIM`。这正是历史 020 被误写为 `UNKNOWN` 的通用根因修复。
