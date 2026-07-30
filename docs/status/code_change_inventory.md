# 代码变更清单

- `llm4hls_agent/repair.py`、`v3_planner.py`：扩展 `PatchProposal` 的
  `target_obligation`、`action_family`、`action_parameters`、`validation_plan`、
  `failure_criteria`、`fallback` 序列化合同，并保持历史 Proposal 读取兼容。
- `llm4hls_agent/v3_search_control.py`：新增纯搜索控制、规范化失败签名、
  Obligation/Hypothesis/CandidateState、三引用 Portfolio、五类 continuation
  建议、每轮新证据判定与保守 DATAFLOW/stream 事实。
- `llm4hls_agent/v3_prototype.py`：将搜索控制写入 Planner 输入/Artifact；
  分离机械与语义无改进；记录 Proposal 实验、validation plan、Portfolio 父节点
  理由和统一终态失败字段。
- `llm4hls_agent/v3_openai_planner.py`、`openai_provider.py`：task-aware
  Provider 现在严格要求完整实验 Proposal；大 kernel 只发送确定性的相关原始行
  切片，而不是默认完整代码。
- `llm4hls_agent/v3_failure_evidence.py`：增加 CoSim 进度、no-progress 时长、
  xsim 启动、运行阶段、transaction progress、log/output 增长字段。
- `llm4hls_agent/v3_batch_benchmark.py`：按绑定 failure evidence 归因 CoSim/Synth/CSim，而非被通用终端 reason 覆盖。
- `tests/test_v3_search_control.py`：新增搜索控制、五类 continuation、
  Portfolio、failure stage 和结构事实单测。
- `tests/test_openai_provider.py`、`tests/test_v3_openai_planner.py`：补充严格
  Proposal schema 与 compact context 测试。
- `tests/test_v3_failure_evidence.py`、`tests/test_v3_prototype.py`、
  `tests/test_v3_batch_benchmark.py`：补充 CoSim runtime、机械 hunk、
  Candidate 安全、Admission fail-closed、CoSim 归类测试。

备份位于 `llm4hls_harness/.codex/backups/2026-07-30-framework-search-control-before-change/`。
