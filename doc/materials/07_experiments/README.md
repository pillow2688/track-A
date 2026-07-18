# Experiments

放模型、agent、prompt、budget policy、HLS 优化策略的实验记录。

每次实验必须记录：

- Date
- Task
- Agent version
- Model exact ID / provider
- Budget
- Tool call counts
- Functional result
- Synth result
- Cosim result
- Latency / score
- 失败原因或改进结论

## 问题复盘要求

每次项目问题定位或修复完成后，必须同步更新对应的 readiness/复盘文档，至少包含：

- 问题现象和明确错误信息；
- 根因与受影响范围；
- 采用的修复和未采用方案；
- 单元测试、真实 Vitis 或 API 验证证据；
- Token、credits 和工具调用影响；
- 防止同类问题再次发生的测试或规则；
- 当前里程碑验收状态是否因此变化。

当前持续维护的复盘文档：

- [V1 问题复盘与 V2 开发清单](2026-07-15-v1-errors-and-v2-readiness.md)
- [V2 核心报告阅读与更新指南](2026-07-18-v2-core-report-guide.md)

不适合放：

- 没有上下文的零散截图
- 大量原始日志
- 私密 API 返回内容

命名建议：

```text
YYYY-MM-DD-dotproduct-model-comparison.md
YYYY-MM-DD-cosim-policy-ablation.md
YYYY-MM-DD-prompt-template-test.md
```
