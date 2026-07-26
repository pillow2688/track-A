# FPT 2026 Track A — Phase V1 Preflight

结论：`PASS`。7/7 Slot 均有唯一终态，随后才启动正式 3×10。

- 目标成功：6/7 (85.7%)
- 流程成功：7/7 (100.0%)
- LLM：7 次请求，usage known/unknown =
  7/0，Token 下界
  24295
- JSON：TRUNCATED_JSON=1，其余三类均为 0；失败响应的
  envelope、raw content、request ID、finish reason 和 usage 均保留
- Patch：raw=2，
  normalized=0，
  exact-context=3，
  unique-delete=1，
  zero=1，ambiguous=0，
  fuzzy=0，最终失败=0
- Budget：Ledger↔result 7/7、Ledger↔state 7/7、state=result 7/7，
  pending credits/tokens 均为 0
- Residual：1/1 目标成功；任务文件哈希未变；横向组件关闭
