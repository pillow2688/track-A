# 框架稳定性检查点

> 更新：2026-07-30，本检查点记录本地冻结前的代码与测试证据。

## 已完成

- 历史 012/016/017/020 轨迹审计完成，根因与原始 Artifact 一一对应；
- 统一失败事实、义务、Proposal 实验与 parent 选择记录已加入；
- task-aware Provider 现在要求真实的 `target_obligation`、`action_family`、
  `action_parameters`、`validation_plan`、`failure_criteria` 与 `fallback`；
- Candidate Portfolio 显式保存 Verified Best、Active Probe 与 Fallback Parent；
  默认不会从失败 Candidate 继续分支；
- Planner context 对大 kernel 使用确定性原始行切片，不默认发送整个源文件；
- CoSim Evidence 新增 xsim 启动、编译/展开/运行阶段、transaction progress、
  log/output 增长等保守事实；
- 机械 patch 失败不再触发固定两次语义无改进停止；
- STRUCTURAL_FIX 获得保守 stream/DATAFLOW 事实与 CoSim 进度证据；
- 020 的 `UNKNOWN` 汇总归类通用修复完成；
- A2/A3 关闭隔离、Candidate 安全与 B2 边界未改动；
- 当前框架相关 130 项本地测试通过；完整发现测试已执行至第 140 项，
  因 RC2 工作树缺失发布快照引用的历史 A2/A3 Artifact 而 fail-closed，
  具体见 `test_results.md`。

## 未完成 / 不可伪造

- 真实定向 fresh HLS 回归尚未运行；
- 现有 RC1 Admission 不授权 RC2 / 当前改动；本地冻结后仍须重新签发
  与新 commit 绑定的 A2 Admission；
- 因此不能声称 012/016/017/020 已通过，不能声称 24/28 已提升，也不能声称 A2/A3 有因果收益。

## 下一入口

先由人工决定代码冻结策略；冻结后使用新的 commit 生成独立 A2 Gate/Admission，再按已固定的 4 个失败任务（最多两次）+4 哨兵协议执行真正 Vitis 回归。
