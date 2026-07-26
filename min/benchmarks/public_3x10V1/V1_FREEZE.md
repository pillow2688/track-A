# Phase V1 无组件基线冻结

- 冻结日期：2026-07-26
- 分支：`feat/track-a-empty-stub-generation-smoke`
- 冻结 commit：`public-3x10-v1^{commit}`
  - 这是冻结 commit 的规范 Git revision；可用
    `git rev-parse public-3x10-v1^{commit}` 得到完整 SHA。
  - Git commit 不能在自身树中保存自己的最终字面 SHA，因为修改该文件会再次
    改变 commit SHA。annotated tag 是本冻结点的规范身份。
- Annotated tag：`public-3x10-v1`
- Tag 描述：`Minimal vertical baseline: public 3x10 V1`

## Benchmark 结果

- 正式 Slot：30/30，严格串行、每 Slot 唯一终态、无 retry、无失败替换
- 流程成功：30/30
- 目标成功：24/30
  - Projection：10/10
  - DotProduct：4/10
  - Residual：10/10
- 官方 Score Proxy：
  - 30 个 Slot 合计：69.0160
  - 每个 repetition 的三任务合计平均：6.9016 / 9
  - 每 Slot 平均：2.3005
- Token：90,919
- Usage unknown：0
- Credits：650

官方 Score Proxy 仅以公开验证作为 correctness proxy。正式服务器若 hidden
correctness 失败，实际官方分数仍为 0。Clock 与资源作为工程指标保留，不进入
官方公式。

## 测试与静态检查

- `PYTHONPATH=llm4hls_harness:. .venv/bin/python -m unittest discover -s llm4hls_harness/tests -t . -q`
  - PASS：648 tests，122.705 s
- `PYTHONPATH=min:llm4hls_harness:. .venv/bin/python -m unittest discover -s min -p 'test_*.py' -q`
  - PASS：34 tests，0.822 s
- `.venv/bin/python -m compileall -q llm4hls_harness min`
  - PASS
- `git diff --check`
  - PASS
- 上述时长来自冻结提交前的最终复跑；`checks/final/` 保存的是更早一次正式
  验收的日志，因此其中记录的运行时长可能不同，但两次结果均为 PASS。

## 证据位置

- 验收目录：
  `/home/ying/CompetitionTrackA/track-A/min/benchmarks/public_3x10V1`
- 正式 run 实际路径：
  `/home/ying/CompetitionTrackA/track-A/min/benchmarks/public_3x10V1/formal/runs`
- 大型 Vitis run/work 目录不进入 Git。
- 30 个 formal run 与 Vitis 结果/关键 artifact 的路径和 SHA-256 见
  `run_artifact_index.json`。该文件是路径/哈希索引，不是完整离线内容归档；
  原始 formal 与 preflight run 必须保留在上述本地验收目录，供原始证据复核。
- Preflight 汇总结论收录在 `preflight/` 的 records、summary 与 gate 文件中；
  其大型原始 run 同样不进入 Git，也不由 `run_artifact_index.json` 完整覆盖。
- 正式汇总见 `formal/formal_summary.json` 和 `benchmark_summary.json`。
- 最终验收见 `v1_final_acceptance.json`。
- 官方 Score Proxy 重算见 `official_score_proxy_recalculation.json`。

## 组件边界

Phase V1 的横向组件全部关闭。未启用 Continuation、Experience、Ranker 或
其他横向决策组件；Planner 每 Slot 仅调用一次。

## V2 分支规则

后续 V2 必须从本 Tag 创建新分支，例如：

```bash
git switch -c <v2-branch-name> public-3x10-v1
```

不得直接在本冻结点继续接入或调优横向组件。
