# V1 OpenAI-Compatible 最小修复闭环设计

状态：已批准设计  
日期：2026-07-15  
范围：`llm4hls_harness` 内部里程碑 V1

## 1. 设计依据

本设计服从以下优先级：

1. 最新 Track A 官方规则与团队收到的提交要求；
2. `doc/materials/04_agent_basics/2026-07-14-budget-aware-langgraph-llm4hls-agent-design.md`；
3. reference harness 的公开接口与行为；
4. 本地实现便利。

V1 只完成“失败诊断 -> 局部上下文 -> 一次 LLM 最小 Patch -> Patch 校验 -> 隔离候选 -> 真实验证 -> 晋级或回滚”的纵向闭环。候选树与 PPA 优化属于 V2；LangGraph、多档上下文升级、完整最终预算预留与动态停止属于 V3；Docker、多 benchmark、多模型实验和比赛材料属于 V4。

## 2. 验收目标

V1 完成时必须满足：

- baseline 保持逐字节不变且只读；
- 确定性规则能分类并定位公开验证失败；
- LLM 只收到结构化失败、局部 kernel、约束和剩余预算；
- LLM 通过 OpenAI-compatible Chat Completions API 返回严格 JSON，其中包含一个 unified diff；
- Patch 只能修改任务指定的 kernel `.cpp`，并受路径、hunk、修改行数和全文件替换限制；
- 有效 Patch 创建新的隔离候选，并将验证状态重置后依次运行 `csim -> synth -> cosim`；
- 通过全部必要验证和最低时钟约束的候选才能晋级；
- 非法 Patch、Provider 失败或验证失败不会污染 baseline 或最佳候选，并产生明确 stop reason；
- LLM 与 Vitis 调用共享权威、追加式预算账本，恢复时不重复调用或计费；
- 记录实际模型、provider、request ID、输入/输出 Token、耗时和结果引用；
- 中英文文档同步，快速测试全部通过，并保留真实 Vitis 2025.2/U55C 验证证据。

## 3. Provider 架构

新增独立的 OpenAI-compatible Provider，实现现有 `RepairProvider` 边界。Provider 不接触文件系统、候选注册表、Vitis 或晋级决策；它只把 `RepairContext` 转换成请求，并把响应解析成 `PatchProposal`。

首选实现使用 Python 标准库 HTTP，避免为 V1 增加运行时 SDK 依赖。配置如下：

- `OPENAI_BASE_URL`：兼容服务根地址；Provider 规范化后调用 `/chat/completions`；
- `OPENAI_API_KEY`：仅从环境读取，不持久化；
- `LLM4HLS_MODEL`：初始默认值为 `deepseek-ai/DeepSeek-V4-Pro`；
- `LLM4HLS_LLM_TIMEOUT_S`：单次请求超时；
- `LLM4HLS_LLM_MAX_OUTPUT_TOKENS`：修复输出上限；
- `LLM4HLS_LLM_TEMPERATURE`：默认采用确定性低温配置。

CLI 可以显式覆盖非秘密配置。API key 不允许出现在 CLI 参数、运行配置、trace、异常消息或 Provider 指纹中。Provider 指纹绑定实现版本、base URL 的非秘密来源标识、模型和生成参数，使缓存不会跨不同模型配置误用。

静态 Provider 保留，只用于单元测试、离线演示和确定性回归，不作为真实模型完成证据。

## 4. Prompt 与响应契约

Repair Prompt 使用总规范第 14.6 节的稳定骨架，包括：

- repair 角色与唯一目标；
- 当前失败 stage、phase 和结构化诊断；
- 带行号的局部 kernel；
- 顶层接口、数值、器件、时钟和禁止修改测试等约束；
- 剩余 Token、credit 和工具预算摘要；
- 只返回一个最小 unified diff 的要求。

V1 使用 COMPACT 上下文。完整源码、完整 testbench、原始日志、hidden、reference 和聊天历史不得进入请求。

期望响应为严格 JSON 对象：

```json
{
  "hypothesis": "public output mismatch is caused by the arithmetic operator",
  "change_class": "FUNCTIONAL_REPAIR",
  "expected_effect": "restore specified vector addition semantics",
  "risk": "low",
  "required_validation": ["csim", "synth", "cosim"],
  "patch": "--- a/kernel.cpp\n+++ b/kernel.cpp\n@@ ..."
}
```

解析器必须拒绝空响应、多 Patch、Markdown fence、缺失字段、非 JSON 内容和非字符串 Patch。V1 不增加格式修复第二次 LLM 调用；解析失败以 `PROVIDER_RESPONSE_INVALID` 明确终止并回滚。有限格式重试留到 V3 的重试和上下文升级策略实现。

## 5. 预算、持久化与恢复

LLM 调用沿用 V0 的计费事务：

```text
estimate -> reserve -> STARTED -> API call -> persist result
  -> reconcile actual tokens/runtime -> COMPLETED
```

动作 ID 绑定 run、候选、诊断上下文摘要、Provider 指纹和生成配置。恢复规则：

- 已完成且摘要匹配：复用响应，不再次请求和计费；
- STARTED 且没有可信结果：记录 AMBIGUOUS，并保守处理；
- 结果或摘要被篡改：拒绝缓存命中。

Provider 持久化结果保存响应所需的最小审计字段，不保存 API key。原始响应若保存，必须先经过秘密信息检查；默认只保存解析后的结构化字段和用量。

V1 在发起 LLM 前必须确认一次 LLM 调用及其后 `csim + synth + 必需 cosim` 闭环可承担。若任务 `requires_cosim=true`，cosim 费用属于闭环必要费用。

## 6. 候选与错误处理

V1 每次运行最多提出一个修复候选：

1. V0 验证 baseline；
2. 找到首个代码类失败并生成结构化诊断；
3. 调用 Provider；
4. 在物化前校验并 dry-run Patch；
5. 从 baseline 创建 `candidate_001`；
6. 对候选依次运行必要验证；
7. 全部通过且满足最低频率后标记 `VERIFIED` 并晋级；
8. 任一级失败则标记 `REJECTED`，最佳候选保持不变。

基础设施错误、超时和不可修复错误不得交给 LLM。终止结果必须区分：不可修复失败、预算不足、Provider 调用失败、Provider 响应无效、Patch 非法、候选验证失败和候选验证成功。

## 7. 测试与证据

快速测试不访问网络或 Vitis，使用 fake backend 和本地 HTTP fixture 覆盖：

- 请求 URL、headers、模型和 Prompt 结构；
- API key 不进入持久化数据和错误消息；
- 正常 JSON 响应及 Token usage 解析；
- HTTP、超时、非 JSON、错误 schema 和缺失 usage；
- Provider 动作缓存、幂等恢复和 Token 对账；
- compile、csim、synth、cosim 分类或安全拒绝路径；
- Patch 路径穿越、测试修改、hunk/行数超限和全文件替换；
- 候选成功晋级、失败拒绝和 baseline 不变。

真实验收使用 Vitis 2025.2、U55C part `xcu55c-fsvh2892-2L-e`。至少保存三类 V1 结果：

1. 一个功能错误由真实模型生成 Patch，并通过 `csim + synth + cosim`；
2. 一个非法或错误 Patch 被拒绝或验证失败，且安全回滚；
3. 一个非修复型/基础设施错误不调用模型并明确终止。

若真实 API 凭据尚未配置，代码、测试和静态演示可以完成，但 V1 状态必须标记为“实现完成、真实模型验收待完成”，不得宣称整个 V1 已验收。

## 8. 文档与交付

同步更新 `README.md` 与 `README_CN.md`：

- 将项目阶段更新为 V0 已完成、V1 可运行；
- 说明静态和 OpenAI-compatible 两种 Provider 的用途；
- 给出不包含秘密值的环境变量和命令示例；
- 说明运行产物、Token 统计、真实 Vitis 路径配置和失败语义；
- 明确 V1 不包含 V2–V4 功能。

V1 代码、测试、示例和双语文档在全部快速测试通过后作为一个可审查提交交付。真实模型/Vitis 运行产物继续由 `.gitignore` 排除，但在交付说明中引用其本地证据路径和摘要。
