# 测试结果

## 本次直接相关测试

命令：

```bash
../../track-A/.venv/bin/python3 -m unittest \
  tests.test_v3_task_aware_smoke \
  tests.test_v3_terminal_latency_fix \
  tests.test_v3_prototype \
  tests.test_v3_search_control -q
```

历史阶段曾有一组 147 项核心测试通过；那是本次继续接线前的
阶段性结果，不能替代当前工作树的验证。

当前新增/改动后已重新执行并通过的最新框架相关集合为：

```bash
../../track-A/.venv/bin/python3 -m unittest \
  tests.test_v3_search_control \
  tests.test_v3_failure_evidence \
  tests.test_openai_provider \
  tests.test_v3_openai_planner \
  tests.test_v3_batch_benchmark \
  tests.test_v3_continuation \
  tests.test_v3_continuation_admission \
  tests.test_v3_experience_v2_runtime \
  tests.test_v3_planner_action \
  tests.test_v3_prototype -q
```

结果：**130 passed**。覆盖 structured Proposal、Obligation/Candidate/Portfolio
投影、failure stage 归一化、CoSim 进度、紧凑代码切片、严格 Provider schema、
Candidate 回退、完整原型路径及 A2/A3 当前 Admission/ABSTAIN 边界。未调用
DeepSeek，未启动 Vitis HLS。

## 完整发现

当前已执行：

```bash
../../track-A/.venv/bin/python3 -m unittest discover -s tests -t . -f -v
```

第 140 项 `test_runtime_fingerprint_excludes_report_only_tools` 报错后停止；
此前测试均通过，另有 1 项因历史不完整 020 Artifact 不可用而跳过。错误是
`release_tools/full_agent_release_metadata.py` 故意 fail-closed：RC2 工作树没有
发布快照所引用的历史 A2/A3 Gate、Admission 和 Store 文件。这不属于本次
搜索控制代码的回归，也不会通过伪造 Artifact 或放宽验证来消除。

仍有已知独立环境/历史问题：

1. RC1 A2/A3 artifact 路径在 Release metadata 测试中不存在；
2. token-policy ABC matrix 声明 `search_closeout_reserve_credits=0`，校验器仍要求正数；
3. Full Agent manifest 引用的 A2 Admission 绑定 RC1 commit，当前 HEAD 为 RC2，故正确 fail-closed。

这些问题均未通过放宽验证、修改历史 Artifact 或伪造 Admission 处理。
