# 代码修改清单

- `llm4hls_agent/v3_search_control.py`：新增结构义务、静态 Guard、策略族前沿和
  STRUCTURAL_FIX 语义进展判定；不含 task-id 特例。
- `llm4hls_agent/v3_prototype.py`：Guard artifact、Candidate 工具前拒绝、实际
  failure evidence 归档、进展计数和“未尝试族可继续”的预算门。
- `llm4hls_agent/openai_provider.py`、`repair.py`、`v3_planner.py`：Planner 增加
  hypothesis 集、选中 hypothesis/family、完整义务要求、预期拓扑差的严格 JSON
  契约和持久化。
- `tests/test_structural_liveness_guard.py`、现有 OpenAI Planner/provider 测试：覆盖
  新契约与回归。

未修改：A1、A2、A3、开关语义、Token/Credit 上限、CoSim timeout、baseline probe、
B2、final reserve、requires_cosim、verifier、Corpus、成功条件。
