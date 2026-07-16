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
| 统一验收必须逐个打开子目录才能人工判断 | 旧报告只展示场景 PASS 和外部链接，关键错误、Patch、Token、工具、Candidate 和一致性证据分散 | 新增只读 `review-v1`，从四组现有证据构建同一 ReviewEvidence，并在 `runs/` 根目录平铺中英文单文件报告 | 人工报告只能读取机器证据；recorded/recomputed 不一致即 FAIL；所有链接相对；生成前后机器文件 hash/size/mtime 不变；不再维护重复的 HTML 展示层 |

历史 V1 证据摘要：`runs/v1-deepseek-final` 使用 `deepseek-v4-pro`，输入/输出 Token 为 `407/229`，缓存命中输入为 `384`，总 Token 为 `636`，estimated period 为 1.482 ns，总 credits 为 26。由于底层 Candidate action 绑定错误和缺少新版 Manifest，该运行必须重做，不能直接计入最终三类统一验收。

## 3. V2 按设计文档的实现状态

根据 `2026-07-14-budget-aware-langgraph-llm4hls-agent-design.md`，V2 不是再次修复单个
kernel，而是候选/PPA 循环。下列能力已于 2026-07-16 在真实 DeepSeek/Vitis 闭环中完成：

1. Candidate tree：支持多个候选和 parent-child 关系，而不是只生成一个 `candidate_001`。
2. 候选比较器：以 correctness 为第一优先级，再比较时钟、latency/II、资源和预算。
3. 多轮候选循环：每轮都必须通过对应的 Vitis 阶段，失败候选不得污染 best。
4. PPA 证据：从 Vitis synthesis report 读取 latency、II、LUT、FF、BRAM、DSP、URAM、estimated clock，并绑定 `code_hash + tool_config_hash`。
5. 约束优先排序：先排除 csim/cosim 错误和时钟不达标候选，再比较 PPA。
6. 探索与最终验证分离：探索候选不能消耗最终验证预留；最终候选必须再次执行 `csim/synth/cosim`。
7. 停止策略：同时受最大候选数、LLM 次数、credits、token、运行时间和无改进轮数限制。
8. 恢复测试：中断在每个 Vitis 阶段和 LLM 阶段后，恢复不得重复计费或覆盖候选。
9. 报告打包：生成面向提交的 Markdown 报告，同时保留 JSON、ledger、日志、Tcl、XML 和 synthesis report 原件。
10. 多任务泛化边界：正式 V2 不允许 fallback；未知任务由严格 Provider/规则边界处理，
    不得用 vector-add 确定性修复冒充 LLM 优化。

## 4. V2 完成验收

- [x] 两个以上候选可以并存并记录 parent。
- [x] 每个候选有独立 source、patch、验证结果和 hash。
- [x] 至少一个错误候选被拒绝且 best 不被污染。
- [x] 至少两个正确候选可以按 PPA 排序。
- [x] 排序前所有候选满足 csim，最终候选满足 synth、cosim 和时钟约束。
- [x] 中断恢复测试证明 credits 不重复扣除。
- [x] `experimental_report.md` 同时列出 Vitis 三阶段结果、PPA、tokens、调用次数和 credits。
- [x] 全部测试通过，并有一次真实 Vitis 2025.2 运行证据。

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
- `review-v1` 不调用 LLM/Vitis，不改机器文件，直接生成 `runs/V1_ACCEPTANCE_REPORT.md` 和 `runs/V1_ACCEPTANCE_REPORT_CN.md`；
- 平铺人工报告重新计算出真实 LLM 3 次、input/output/cached/total Tokens 为 2700/676/384/3376、csim/synth/cosim 为 7/4/3、HLS tools 为 14、Credits used/remaining 为 83/237，并与机器验收一致；
- 相关快速测试、Python compileall 和 `git diff --check` 必须全部通过后才能提交。

V1 现已满足“至少三类 HLS 错误能由 LLM 修复或安全回滚”的阶段目标。`PATCH_INVALID` 仍只作为独立安全负例；`COSIM_FAILURE` 仍留到 V2。V2 开发不得覆盖或删除上述四个正式证据目录及统一验收目录。

## 7. 人工验收经验与后续版本约束

本次 V1 暴露的核心问题不是缺少实验数据，而是证据分散：审核者需要在多个运行目录、
报告、Manifest、Ledger、Trace、Candidate Registry 和 Vitis action 之间反复跳转。
后续 V2/V3/V4 必须继承本次形成的“平铺、双语、单文件优先”格式：

- 在 `runs/` 根目录直接生成 `V<N>_ACCEPTANCE_REPORT.md` 和
  `V<N>_ACCEPTANCE_REPORT_CN.md`，不嵌套报告目录；
- 不再生成 HTML；Markdown 同时适合人工阅读、版本控制和比赛提交，避免维护重复展示层；
- 报告顶部先给总体 PASS/FAIL、核心统计、总 Token/工具/credits/剩余预算和总审核清单；
- 每个场景直接给 Baseline 错误、模型与 fallback、Token、Patch、三级 Vitis/clock、
  Candidate 提升或回滚、Acceptance 条件和关键 Trace；
- 小 Patch 直接完整展示，大型日志和 JSON/JSONL 只提供相对于 `runs/` 的追溯链接；
- 中英文必须读取同一个证据模型，不能分别手工填写，也不能把调用次数误写成 credits；
- `acceptance_result.json`、Manifest、Ledger、Trace、Candidate、action 和 Vitis 报告仍是
  机器权威证据，人工报告不得修改它们；
- 生成过程不得调用 LLM/Vitis，生成前后核对机器文件 hash/size/mtime；
- recorded/recomputed、Ledger/Trace/action、Token 或 Candidate 状态任一不一致时，报告
  必须明确 FAIL，不能为了方便人工验收而降级检查。

版本特有内容只做追加：V2 补 Candidate tree/PPA 比较和优化收益成本，V3 补图路由、
checkpoint/resume、预算 reserve 和 stop reason，V4 补 hidden-like、Docker、多任务及
多模型证据。核心统计、逐场景审核卡、逐项 Acceptance 和原始证据索引保持稳定，便于
不同版本使用同一套人工审核习惯。

## 8. 2026-07-16 V2 开发中的问题与约束更新

V2 已于 2026-07-16 完成真实 DeepSeek/Vitis 2025.2 验收。代码边界包括
CandidateManager、探索/最终验证作用域、PPA 评分与字典序比较、单优化类选择、严格
DeepSeek 优化提案、四轮 Candidate 循环、最终复验、候选回退和完成轮次恢复。

| 问题 | 根因 | 解决方法 | 防复发规则 |
|---|---|---|---|
| V2 中断后可能重复第一轮 | 只看 Trace 或内存状态不能证明一轮已经完整落盘 | 每轮以 `optimization_rounds/round_NNN.json` 为 durable completion record；恢复时重建 attempted、score、metrics、best 和 no-improvement | 不从 Trace 推断轮次完成；round、Registry、Score、Metrics 任一不一致即 fail closed |
| 最优 Candidate 最终复验失败后没有安全替代 | 探索期 PASS 不等于最终作用域 PASS | 对已完整验证且满足硬约束的历史 Candidate 重新按同一比较器排序，只在预算允许时依次执行 final 验证 | `best_candidate_id` 保留探索最优，`final_candidate_id` 单独记录实际最终通过者，并记录 fallback 原因 |
| 安全回归 fixture 的 unified diff 首次被拒绝 | hunk 声明从第 10 行开始，但 hunk body 实际对应第 11 行；严格 parser 因 context 偏移拒绝 | 将 hunk 修正为 `@@ -11,4 +11,4 @@`，不放宽 Patch 校验 | 静态安全 Patch 也必须走与 LLM Patch 相同的严格 path/count/context/dry-run 校验；测试数据错误不得通过降低策略解决 |
| 安全拒绝 credits 一度预期为 27 | 把 V1 的 26 credits 误当成 baseline 成本；实际 baseline 完整验证是 `1+4+20=25`，拒绝 Candidate 只追加一次 CSim `1` | 测试按 Ledger 重算为 26，并同时断言调用次数 `csim=2,synth=1,cosim=1,llm=0` | 所有报告必须分别显示 calls、unit cost 和 credits；预期数字也必须由阶段成本公式与 Ledger 复核 |
| 重建 Manifest 可能掩盖 final action 引用被替换 | 旧验收只检查 `v2_result.json` 内嵌的 PASS/scope，没有重新打开 action 核对 Candidate、code hash、action ID 和工具配置 | 新增“篡改 final CSim ref 后重建 Manifest”回归测试；Acceptance 对探索、最终和安全验证逐 action 重绑 Candidate/code/scope/backend/tool-config | Manifest 只能证明当前文件集合未再变化，不能替代语义重算；关键引用即使 hash 自洽也必须与 action/ledger 重新绑定 |
| 可配置工具 cost 高于固定 final reserve | `final_reserve_credits=25` 只在默认 `1+4+20` 成本下足够，调整 cost 后可能欠保留 | `run_v2` 启动前要求 reserve 不低于实际 CSim+Synth+CoSim 配置成本 | 所有预算默认值都只是配置；安全不变量必须按本次 run config 重算，不能把 25 写成普适常量 |
| 安全拒绝目录可能与优化目录混用 | 已完成安全运行的幂等检查未比较 tool config，且复用含优化 Candidate 的目录可能覆盖 best/final 语义 | 完成运行同时核对完整 `run_config.json`；拒绝流程只允许 baseline 或同一 safety Patch 的可恢复 Candidate | 优化、拒绝、验收必须使用三个独立目录；目录身份与配置不匹配立即报错，不做隐式复用 |
| Codex 沙箱内 XSIM 在 snapshot 后进入 `xsim%` | 沙箱的 PID/系统隔离使 XSIM Tcl 命令出现 `unexpected exception`；相同 kernel/Tcl 在沙箱外正常 | 终止安静挂起的沙箱运行并保留失败证据；经批准在沙箱外重跑同一 safety 命令，真实 Vitis 三级通过且回归 CSim 按预期失败 | Codex 内启动真实 Vitis/XSIM 必须使用获批的沙箱外执行；先读 Trace/XSIM 日志确认阶段，不能把基础设施异常写成 Candidate 错误 |
| 正式 V2 DeepSeek 调用被第三方数据传输审查拦截 | 优化 Prompt 会发送 kernel 局部源码、PPA 指标、约束和失败记录；旧的通用批准未被当前执行层视为本次 V2 的充分知情批准 | 不绕过审查、不改用未审计通道；保持正式目录未创建，并请求用户对本次 V2 数据范围再次明确批准 | 每个需要向外部模型发送新类型上下文的阶段都应在执行前列明数据范围并取得明确批准；API Key 仍只通过环境认证且不得进入 Prompt/日志 |
| Provider 返回的 `required_validation` 形态不稳定，失败响应缺少可诊断证据 | OpenAI-compatible 模型可能返回 bool/字符串等非预期形态；旧异常路径丢失 response excerpt 和 usage | 失败结果保留 request ID、响应摘录和 input/output/cached Token；只对可安全映射的 validation 形态归一化，其余 fail closed | Provider 失败也是正式 action，必须保留真实 usage 和响应证据，不得写成 0 Token 或静默 fallback |
| 正确的小型 Patch 因 hunk 起始行偏移 1 行被拒绝 | 模型 Patch body 与源码唯一匹配，但 unified diff location metadata 有轻微偏移 | 仅当 old body 在源码中唯一匹配且偏移不超过 8 行时，确定性修正 hunk 位置；路径和 Patch body 不变，之后仍走严格 dry-run | 位置修复只处理元数据；多重匹配、超限偏移、非法路径或 body 不符一律拒绝 |
| V2 最初机器验收误报 `PUBLIC_INPUT_BINDING_INVALID` | 严格 Patch validator 接受等价的 `kernel.cpp` 和 `a/kernel.cpp` 路径，但 Acceptance 额外硬编码必须带 `a/`、`b/` 前缀 | 增加真实形态回归测试，并让 Acceptance 与 Patch validator 共用同一目标路径语义；重新计算后为 `REAL/PASS` | 机器验收不得发明比生产验证器更窄且无安全收益的文本格式；安全判断应复用同一解析语义 |
| Acceptance 可独立校验 Patch/source hash，但未证明 parent→Patch→child | Manifest 重建后，攻击者可同时替换 Patch 及其 hash，却保留无关 child source | 验收逐个对 parent source 严格应用 Patch，结果必须逐字节等于 child source；同时把 Provider 原始 Patch 经确定性 count/location/path 规范化后绑定 Candidate Patch 与 optimization class | Candidate tree 的 hash、路径和 parent 字段不是血缘证明；每条边都必须重新执行并绑定 Provider→Patch→child |
| Token 只检查 `remaining > 0`，极端情况下 API 返回后 Ledger 才发现超限 | 调用前没有为输入 Prompt 和最大输出建立上界，完成事件可能因超 Token 被拒绝并遗留 STARTED action | Provider 必须公开含 `max_tokens` 的 HTTP body；调用前按序列化 body UTF-8 字节数、512 协议余量和最大输出做保守预留，不足时以 `TOKEN_RESERVE_REACHED` 在 API 前停止 | 外部调用的预算闸门必须发生在副作用前；不可把超限处理推迟到 usage 返回后 |
| durable round 恢复曾信任已落盘的 PROMOTED/score/metrics | round 顺序和字符串 decision 不能证明 Context、action、validation、score、comparison 仍一致 | 恢复时重算 Selector 与稳定 Context digest，重绑 request/provider action 和 Ledger，再校验 Provider/Patch、三级 action、score 与 comparison；任一不一致 fail closed 且不继续计费 | Trace 不是恢复权威；durable record 也必须语义重算，不能只检查 JSON 字段存在 |
| Final fallback 仅由剩余预算间接限制 | 候选较多时可能尝试所有 eligible Candidate，没有独立的最大次数上限 | 新增 `max_final_attempts`/`--max-final-attempts`，默认总计 2 次（最佳候选加最多一个替代）；失败时清空 `final_candidate_id` | Final closure 必须同时受 affordability 和明确 attempt count 约束 |

V2 的专用公开 fixture 固定为 256 元素 U55C `vector_add`、10 ns、预算 160，基线功能
正确但使用保守的 `PIPELINE II=16`。确定性安全负例只修改 `kernel.cpp` 的加法为减法，
必须先验证 baseline 的 CSim/Synth/CoSim/Clock 全部通过，再物化
`kind=safety_regression` 子 Candidate；对子 Candidate 只运行 CSim，并要求其失败、
Synth/CoSim 保持 `NOT_RUN`、best/final/active 全部安全回到 `candidate_000`、LLM 调用为 0。
该安全负例仅证明拒绝边界，不得进入正式 PPA 最优候选集合。

## 9. 2026-07-16 V2 最终真实验收

正式优化目录为 `runs/v2-optimize-final`，安全拒绝目录为
`runs/v2-safety-rejection-final`，统一机器验收目录为 `runs/v2-acceptance`。根目录平铺的
人工报告为 `runs/V2_ACCEPTANCE_REPORT.md` 和
`runs/V2_ACCEPTANCE_REPORT_CN.md`。`acceptance_result.json` 的证据等级为 `REAL`，
总体状态为 `PASS`，14 项确定性检查全部通过且没有 reason code。

| Candidate | Parent | 优化类 | CSim/Synth/CoSim | 决策 | Latency worst | II max | Clock ns | LUT/FF | Tokens |
|---|---|---|---|---|---:|---:|---:|---:|---:|
| `candidate_000` | — | baseline | PASS/PASS/PASS | VERIFIED | 513 | 512 | 1.479 | 110/20 | 0 |
| `candidate_001` | `candidate_000` | LOOP_PIPELINE | PASS/PASS/PASS | PROMOTED | 258 | 256 | 1.479 | 109/20 | 2179 |
| `candidate_002` | `candidate_001` | MEMORY_LAYOUT | PASS/PASS/PASS | REJECTED_NOT_BETTER | 258 | 256 | 1.479 | 173/20 | 2183 |
| `candidate_003` | `candidate_001` | LOOP_UNROLL | PASS/PASS/PASS | PROMOTED | 130 | 128 | 1.489 | 134/29 | 2129 |
| `candidate_004` | `candidate_003` | LOOP_RESTRUCTURE | PASS/PASS/PASS | FINAL | 128 | 129 | 0.880 | 4829/129 | 2191 |

最终 `candidate_004` 又以独立 `final` scope 重跑 CSim、Synth、CoSim，三阶段均 PASS，
10 ns/100 MHz 时钟约束通过。安全负例从 baseline 派生 `candidate_001`，其回归 CSim
真实失败，Synth/CoSim 未运行，best/final/active 均保持 `candidate_000`，无 LLM 调用。

正式优化共调用 DeepSeek 4 次，无 fallback；input/output/cached/total Tokens 为
7491/1191/1792/8682。优化闭环调用 CSim/Synth/CoSim 各 6 次并消耗 150 credits，
剩余 10；安全拒绝调用 CSim/Synth/CoSim 为 2/1/1 并消耗 26 credits，剩余 134。
两场景合计 26 次 action（CSim 8、Synth 7、CoSim 7、LLM 4），总消耗 176 credits，
合计剩余预算 144。cached input Token 是 input Token 的子集，不重复计入 total。

每次 Provider 请求只发送 `kernel.cpp` 的局部范围、结构化 PPA/验证状态、时钟/资源/接口/
功能约束、相关失败和两条优化类规则。请求审计明确排除完整仓库、完整日志、testbench、
hidden/reference、无关源码、本机绝对路径、API Key 和认证头。四轮请求证据均由 Manifest
覆盖，Token 与 Provider action、Ledger、Trace 一致。

重新生成平铺中英文报告前后，优化、安全拒绝和机器验收三个目录（排除锁文件）的合并
SHA-256 均为
`cc2a087ed419bfdd8cdf733dcf0bb68de301e2b647dfb65f943af6f542e78a83`，证明人工报告生成
未改动机器权威证据。报告扫描未发现 `/home/`、`file://`、HTML 或外部 URL。独立审查
修复后，全套 134 项单元测试、Python compileall、`git diff --check`、强化后的 `accept-v2`
和 `review-v2` 均重新通过；真实 LLM/Vitis 原始运行无需重跑。
