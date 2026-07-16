# V1 问题复盘与 V2 开发清单

## 1. V1 当前状态

V1 **已于 2026-07-16 完成统一真实验收**。正式验收目录为 `llm4hls_harness/runs/v1-acceptance`，`acceptance_result.json` 的 `evidence_tier=REAL`、`overall_status=PASS`，四个 case 均为 `PASS` 且没有 reason code；英文 `acceptance_report.md` 与中文 `acceptance_report_CN.md` 均提供矩阵、四组报告/Manifest 链接和确定性验收复现命令。

V1 的正式完成条件现已固定为三类 HLS 任务错误：`FUNCTIONAL_MISMATCH`、`COMPILE_ERROR`、`SYNTHESIS_ERROR`。`PATCH_INVALID` 是独立安全负例，不计作第三类 HLS 错误；`COSIM_FAILURE` 留到 V2。

`DONE` 的唯一条件是最终候选由 Vitis 验证通过；LLM 或 deterministic fallback 只能产生候选，不能宣布成功。

## 2. 本轮问题、原因和解决方法

| 问题 | 根因 | 解决方法 | 防复发规则 |
|---|---|---|---|
| DeepSeek 返回空 `content` | reasoning 模型把答案放入 `reasoning_content` 或分段 content | Provider 支持 reasoning 和分段响应 | Provider 测试必须覆盖多种 OpenAI-compatible 响应形态 |
| `required_validation` 类型错误 | 模型把数组序列化成字符串 | 归一化逗号分隔字符串 | 归一化后仍执行阶段白名单校验 |
| 阶段名不一致 | 模型返回 `simulation`、`synthesis`、`co-simulation` 等描述 | 映射为 `csim/synth/cosim` | 只允许可明确映射的阶段，未知值拒绝 |
| JSON 前后有解释文字或 Markdown | 模型没有严格遵守 JSON-only prompt | 提取外层 JSON，再执行严格 schema 和 patch 校验 | 不把模型文本直接当作验证结果 |
| fallback 未触发 | 错误使用 `kernel_name` 判断顶层函数 | 使用任务的 `top` 字段 | 新增 fallback 单元测试 |
| fallback 生成后引用错误 | 后续代码只允许 `llm_actions/...` | 增加持久化 `v0_fallback/...` 引用 | 所有 proposal 必须有持久化 result reference |
| 报告把调用次数称为 credits | `tool_used` 是次数，不是费用 | 报告同时展示调用次数、单次费用和总 credits | 报告字段名称必须区分 count 与 cost |
| 反复运行复用旧失败 | action ID 和旧 result 被缓存 | 每次调试使用新 run-dir；正式恢复才复用同一 run-dir | 不删除成功证据，不混用实验目录 |
| LLM 调用失败却记录 0 Token | Provider 在解析 Patch 前没有提取 usage，异常路径固定写 0 | 先解析 usage；异常携带 input/output Token、耗时、request ID 和响应摘录 | 成功和失败的 API 调用都必须按 Provider usage 对账 |
| fallback 被误当成 LLM-based V1 | fallback 候选也能得到 `CANDIDATE_VERIFIED`，报告未注明来源 | fallback 改为默认关闭的显式调试选项；报告显示 candidate provider 与是否使用 LLM 候选 | Vitis PASS 与 LLM-based 验收是两个独立维度 |
| DeepSeek 默认 thinking 耗尽短输出预算 | DeepSeek V4 thinking 默认开启，1000 token 可能用于 reasoning 而没有 final content | DeepSeek 修复请求显式发送 `thinking.type=disabled` | Provider 特有行为必须进入请求配置、指纹和测试 |
| 正式 V1 Token 数看起来过少 | V1 使用 COMPACT 局部上下文，只发送 7 行源码和结构化失败证据；thinking 已关闭 | 报告分别展示输入 407、输出 229、缓存输入 384、总量 636，并保留模型输入输出证据 | 缓存 Token 是输入 Token 子集，报告不得重复相加；复杂任务再按 COMPACT/FOCUSED/EXPANDED 升级上下文 |
| Candidate 的 Vitis action 错绑为 `candidate_000` | `_invoke_stage()` 把 Candidate ID 写死为默认 baseline ID | `_invoke_stage()` 接收显式 `candidate_id`，Candidate csim/synth/cosim 全部绑定 `candidate_001` 和其 `code_hash` | 单元测试必须读取每个 action `result.json`，核对 Candidate ID 和 code hash；旧运行不得直接进入新版验收 |
| 设计草案曾把 `PATCH_INVALID` 当作第三类 HLS 错误 | 混淆了任务错误分类和平台安全边界 | 三类 HLS 错误固定为功能、编译、综合；非法 Patch 单列 safety case | readiness、报告和 Acceptance Evaluator 分别输出 HLS matrix 与 safety matrix |
| Candidate 物化缺少原子 staging | 虽然 Patch 已先 dry-run，但文件逐个写入 Candidate 目录，崩溃可能留下半成品 | Patch 验证后才分配 ID；先写 Candidate 树外临时 staging，再原子移动并注册；支持一致 orphan 恢复 | Manifest 发现未清理 staging 时 fail closed；非法 Patch 不得出现 ID、目录或 registry 记录 |
| 缺少统一的证据完整性入口 | 报告、JSON、ledger 和 Vitis 原件分散，人工检查容易漏掉绑定错误或篡改 | 新增稳定排序、SHA-256 的 `artifact_manifest.json` 和只读 `AcceptanceEvaluator` | 缺失、越界、hash 变化、fake backend、Token=0、credits 不一致或 Candidate 绑定错误全部 fail closed |
| `SYNTHESIS_ERROR` fixture 是否稳定未知 | 软件合法但 HLS 不支持的构造可能因版本变化表现不同 | 使用动态 `new[]/delete[]`，并以真实 Vitis 2025.2 preflight 作为 fixture 准入门槛 | 当前 preflight 已确认 csim PASS、synth `synth_error`，错误为 `Undefined function operator new[]`；升级工具链后必须重跑 |
| DeepSeek Patch 的 unified-diff hunk 新行计数过期 | 模型删除一行后仍输出旧的 `@@ -1,12 +1,12 @@`，语义正确但标准 patch parser 判为截断 | 在 policy/dry-run 之前只按 hunk body 重新计算 old/new count；文件路径、起始行和 Patch body 原样保留，之后继续严格校验 | 该步骤只做语法规范化，不能修改源码语义或绕过“仅可修改 kernel.cpp”的安全策略；增加陈旧计数回归测试 |
| 第一次综合错误正式修复停在 XSIM Tcl 提示符 | Vitis 已生成 RTL/snapshot，但 `xsim_script.tcl` 启动时报告 `unexpected exception`，不是 Candidate 代码的综合错误 | 保留完整 XSIM 日志并等待 harness 记录 `COSIM_TIMEOUT`/回滚；在全新 run-dir 重试相同修复和全套 Vitis 验证 | 基础设施超时不得写成第三类 HLS 错误，也不得伪造 COSIM PASS；失败运行保留作复盘证据，只有新运行三级 PASS 才进入验收 |
| CLI 进程无法读取用户终端中的 API 环境变量 | Codex 工具进程与用户交互终端不是同一 shell 环境，`/dev/shm` 也不一定跨执行环境共享 | 使用仓库外、权限 `0600` 的 `/home/ying/CompetitionTrackA/.llm4hls-deepseek.env` 显式加载 | 环境文件不得进入比赛仓库、Manifest、日志或命令输出；只检查变量是否已设置，不显示 Key |
| 统一验收必须逐个打开子目录才能人工判断 | 旧报告只展示场景 PASS 和外部链接，关键错误、Patch、Token、工具、Candidate 和一致性证据分散 | 新增只读 `review-v1`，从四组现有证据构建同一 ReviewEvidence，并在 `runs/` 根目录平铺中英文单文件报告和静态 Dashboard | 人工报告只能读取机器证据；recorded/recomputed 不一致即 FAIL；所有链接相对；生成前后机器文件 hash/size/mtime 不变 |

历史 V1 证据摘要：`runs/v1-deepseek-final` 使用 `deepseek-v4-pro`，输入/输出 Token 为 `407/229`，缓存命中输入为 `384`，总 Token 为 `636`，estimated period 为 1.482 ns，总 credits 为 26。由于底层 Candidate action 绑定错误和缺少新版 Manifest，该运行必须重做，不能直接计入最终三类统一验收。

## 3. V2 按设计文档仍需实现

根据 `2026-07-14-budget-aware-langgraph-llm4hls-agent-design.md`，V2 不是再次修复单个 kernel，而是候选/PPA 循环，至少还需要：

1. Candidate tree：支持多个候选和 parent-child 关系，而不是只生成一个 `candidate_001`。
2. 候选比较器：以 correctness 为第一优先级，再比较时钟、latency/II、资源和预算。
3. 多轮候选循环：每轮都必须通过对应的 Vitis 阶段，失败候选不得污染 best。
4. PPA 证据：从 Vitis synthesis report 读取 latency、II、LUT、FF、BRAM、DSP、URAM、estimated clock，并绑定 `code_hash + tool_config_hash`。
5. 约束优先排序：先排除 csim/cosim 错误和时钟不达标候选，再比较 PPA。
6. 探索与最终验证分离：探索候选不能消耗最终验证预留；最终候选必须再次执行 `csim/synth/cosim`。
7. 停止策略：同时受最大候选数、LLM 次数、credits、token、运行时间和无改进轮数限制。
8. 恢复测试：中断在每个 Vitis 阶段和 LLM 阶段后，恢复不得重复计费或覆盖候选。
9. 报告打包：生成面向提交的 Markdown 报告，同时保留 JSON、ledger、日志、Tcl、XML 和 synthesis report 原件。
10. 多任务泛化：fallback 不能只硬编码 vector-add；未知任务必须返回不可修复或交给 LLM，不能误改源码。

## 4. V2 完成验收

- [ ] 两个以上候选可以并存并记录 parent。
- [ ] 每个候选有独立 source、patch、验证结果和 hash。
- [ ] 至少一个错误候选被拒绝且 best 不被污染。
- [ ] 至少两个正确候选可以按 PPA 排序。
- [ ] 排序前所有候选满足 csim，最终候选满足 synth、cosim 和时钟约束。
- [ ] 中断恢复测试证明 credits 不重复扣除。
- [ ] `experimental_report.md` 同时列出 Vitis 三阶段结果、PPA、tokens、调用次数和 credits。
- [ ] 全部测试通过，并有一次真实 Vitis 2025.2 运行证据。

## 5. 运行产物规则

`v1_result.json`、`workflow_result.json`、`candidate_registry.json`、`budget_ledger.jsonl` 和 `trace.jsonl` 是机器可读审计证据；`experimental_report.md` 是人类可读报告。临时 Vitis 工作目录可以在确认没有用于恢复或审计后清理，不能删除 source、patch、result、Tcl、日志、XML、报告和 hash。

2026-07-16 收尾时已删除重复的 preflight、static/fallback、旧环境修复和无有效 API 结果的 V1 运行目录。保留五个正式目录 `v1-functional-final-2`、`v1-compile-final`、`v1-synthesis-final-2`、`v1-patch-invalid`、`v1-acceptance`；另保留 `v1-deepseek-final` 作为旧 Candidate 绑定问题证据，保留 `v1-synthesis-final` 作为 XSIM 异常、结构化超时和安全回滚证据。`u55c-smoke-100mhz-py311` 属于 V0 smoke，不在本次 V1 清理范围内。

## 6. 2026-07-16 V1 三类错误最终验收

| Case | 正式证据 | Tokens | Credits | 状态 |
|---|---|---:|---:|---|
| `FUNCTIONAL_MISMATCH` | `runs/v1-functional-final-2`：DeepSeek + Candidate csim/synth/cosim PASS，1.482 ns | 633 | 26 | PASS |
| `COMPILE_ERROR` | `runs/v1-compile-final`：DeepSeek + Candidate csim/synth/cosim PASS，1.482 ns | 1320 | 26 | PASS |
| `SYNTHESIS_ERROR` | `runs/v1-synthesis-final-2`：baseline synth error；DeepSeek + Candidate csim/synth/cosim PASS，1.579 ns | 1423 | 30 | PASS |
| `PATCH_INVALID` safety | `runs/v1-patch-invalid`：`FAILED/PATCH_INVALID`，无 Candidate ID、目录或 registry 记录 | 0 | 1 | PASS |

正式证据合计使用 3376 Tokens、83 credits。credits 可按 ledger 对账为 csim 7 次 × 1 = 7、synth 4 次 × 4 = 16、cosim 3 次 × 20 = 60、LLM/静态 provider 4 次 × 0 = 0；总计 83。每个运行独立计费，不能把调用次数直接当成 credits。

综合指标：功能和编译修复的 latency 为 18、II 为 16、LUT/FF 为 103/12；综合修复的 latency 为 39、II 为 40、LUT/FF 为 261/64；三者 BRAM/DSP/URAM 均为 0，且 estimated clock period 均小于 10 ns。

实现侧已完成：

- Patch parse/policy/dry-run 通过后才分配并原子物化 Candidate；
- Candidate Vitis action 显式绑定 Candidate ID 与 code hash；
- 自动生成 `artifact_manifest.json`；
- 新增 `accept-v1` 确定性验收命令；
- 统一验收会把 fake/unit 证据限制为 `TEST_PASS`，本次正式证据为真实 Vitis 2025.2 的 `REAL/PASS`；
- 统一验收目录同时生成机器可读 `acceptance_result.json`、英文 `acceptance_report.md` 和中文 `acceptance_report_CN.md`；
- `review-v1` 不调用 LLM/Vitis，不改机器文件，直接生成 `runs/V1_ACCEPTANCE_REPORT.md`、`runs/V1_ACCEPTANCE_REPORT_CN.md` 和 `runs/V1_ACCEPTANCE_DASHBOARD.html`；
- 平铺人工报告重新计算出真实 LLM 3 次、input/output/cached/total Tokens 为 2700/676/384/3376、csim/synth/cosim 为 7/4/3、HLS tools 为 14、Credits used/remaining 为 83/237，并与机器验收一致；
- 相关快速测试、Python compileall 和 `git diff --check` 必须全部通过后才能提交。

V1 现已满足“至少三类 HLS 错误能由 LLM 修复或安全回滚”的阶段目标。`PATCH_INVALID` 仍只作为独立安全负例；`COSIM_FAILURE` 仍留到 V2。V2 开发不得覆盖或删除上述四个正式证据目录及统一验收目录。
