# V1 单文件优先验收报告与静态 Dashboard 设计

Status: revised design awaiting user re-review<br>
Owner: team<br>
Date: 2026-07-16<br>
Scope: `llm4hls_harness` V1 unified acceptance reporting

## 1. 背景

当前 V1 统一验收器可以确定性地验证三类真实 HLS 修复和一个
`PATCH_INVALID` 安全负例，并生成 `acceptance_result.json`、英文报告和中文报告。
现有 Markdown 只提供场景、错误类别、PASS 状态和外部证据链接。审核者必须逐个打开
实验报告、Manifest、预算账本、Trace 和 Vitis action，才能判断 PASS 的依据。

本设计将统一验收改造成“单文件优先审核”：审核者只阅读 `runs/` 根目录中的
`V1_ACCEPTANCE_REPORT.md` 即可完成主要英文审核；
`V1_ACCEPTANCE_REPORT_CN.md` 提供字段和结论完全对应的中文审核；
`V1_ACCEPTANCE_DASHBOARD.html` 提供相同证据的静态双语视图。大型原始文件只用于
末尾追溯。

## 2. 已批准目标

1. 不重新运行 LLM；
2. 不重新运行 Vitis；
3. 不修改四个既有实验运行目录中的任何文件；
4. 只从机器可读产物聚合证据；
5. 自动生成可直接人工审核的英文和中文 Markdown；
6. 自动生成无外部依赖的静态 HTML Dashboard；
7. 所有 PASS 都由确定性 Acceptance checks 计算，不在 renderer 中硬编码；
8. 三个人工审核文件直接平铺在 `runs/`，不再嵌套到新的报告目录；
9. 所有输出链接使用相对于 `runs/` 的路径；
10. 报告正文直接展示关键证据，原始大文件仅在末尾链接；
11. 现有 `acceptance_result.json`、JSON/JSONL、Manifest、Trace、ledger 和 action
    result 全部只读且字节不变；
12. 报告重新计算的结论必须与现有 `acceptance_result.json` 一致，否则人工审核总体
    状态为 FAIL 并显示一致性失败。

## 3. 输入与输出

### 3.1 固定真实输入

```text
runs/v1-compile-final/
runs/v1-functional-final-2/
runs/v1-synthesis-final-2/
runs/v1-patch-invalid/
```

每个目录至少读取：

- `artifact_manifest.json`；
- `v1_result.json`；
- `candidate_registry.json`；
- `budget_ledger.jsonl` 与 `budget_state.json`；
- `trace.jsonl`；
- diagnostics、LLM/static proposal 和 Vitis action `result.json`；
- Candidate source、Patch 和 synthesis/cosim 机器报告引用。

### 3.2 平铺人工审核输出与只读机器结果

```text
runs/
├── V1_ACCEPTANCE_REPORT.md
├── V1_ACCEPTANCE_REPORT_CN.md
├── V1_ACCEPTANCE_DASHBOARD.html
└── v1-acceptance/
    └── acceptance_result.json    # existing, read-only
```

新增离线 `review-v1` 命令。它读取现有机器结果和四个运行目录，只写三个平铺人工审核
文件。CLI 返回：

```json
{
  "status": "PASS",
  "acceptance_result_ref": "v1-acceptance/acceptance_result.json",
  "report_ref": "V1_ACCEPTANCE_REPORT.md",
  "report_cn_ref": "V1_ACCEPTANCE_REPORT_CN.md",
  "dashboard_ref": "V1_ACCEPTANCE_DASHBOARD.html"
}
```

现有 `accept-v1` 机器验收行为保持兼容；本轮人工审核生成不得调用它覆盖
`v1-acceptance/acceptance_result.json`，也不改写其中的旧报告文件。

## 4. 方案选择

采用统一证据模型方案，不采用“验收器和 Reporter 各自解析一次”，也不采用
“先写 Markdown 再转换 HTML”。

```text
four immutable run directories
          |
          v
EvidenceCollector + AcceptanceCheckCollector
          |
          v
existing acceptance_result + ReviewEvidence (canonical in-memory model)
          |
          +--> V1_ACCEPTANCE_REPORT.md
          +--> V1_ACCEPTANCE_REPORT_CN.md
          +--> V1_ACCEPTANCE_DASHBOARD.html
```

所有 renderer 只能消费 `ReviewEvidence`，不得重新读取 ledger、Trace、registry 或
Vitis 文件。这样 PASS、统计数字、场景细节和链接只有一个来源。Collector 重新计算
每个 check 后，必须与现有 `acceptance_result.json` 的 overall、case status、reason
codes 和 Manifest digest 对比。

## 5. 组件边界

### 5.1 `EvidenceCollector`

只读读取一个运行目录，职责是：

- 验证 Manifest 及其中每个文件的 size/hash；
- 读取核心 JSON/JSONL；
- 解析 ledger 的终态 action、Token 和 credits；
- 读取 baseline/final validation action；
- 提取 synthesis metrics 和 cosim status；
- 提取 Patch、Candidate lineage 和关键 Trace；
- 生成相对于 `runs/` 根目录的证据链接；
- 输出结构化 case evidence，不决定最终 PASS。

它不得调用 Provider、`subprocess`、ToolServer 或 Vitis backend。

### 5.2 `AcceptanceCheckCollector`

现有 `_add(reasons, condition, reason_code)` 只保存失败原因，不能直接展示“逐项为什么
PASS”。重构为同时记录以下结构：

```json
{
  "check_id": "candidate_cosim_pass",
  "status": "PASS",
  "reason_code": null,
  "evidence_refs": ["../v1-compile-final/actions/.../result.json"],
  "observed": "PASS",
  "expected": "PASS"
}
```

场景状态由所有 required checks 归约：全部 PASS 才是 PASS。renderer 只能展示该
结果，不能自行决定或覆盖状态。

### 5.3 `ReviewEvidence`

顶层字段：

```text
schema_version
acceptance_id / spec_digest / evaluator_version
evidence_tier / overall_status / policy_reason_codes
review_data_digest
core_summary
cases[]
manifest_digests
raw_evidence_index
```

每个 case 包含：

```text
identity and status
baseline_failure
error_localization
provider_usage
patch_summary
validation_comparison
candidate_lifecycle
acceptance_checks
trace_summary
raw_evidence_links
```

现有 `acceptance_result.json` 不增加、不删除或改写任何字段。规范化 review data 仅存于
内存，使用 canonical JSON 计算 `review_data_digest`；两个 Markdown 和 HTML 都展示
该 digest，同时展示现有 `acceptance_result.json` 的 SHA-256。三份人工报告据此证明
同源，机器结果则通过只读 hash 证明未修改。

### 5.4 Renderer

- `MarkdownRenderer(language="en")` 生成 `V1_ACCEPTANCE_REPORT.md`；
- `MarkdownRenderer(language="zh-CN")` 生成 `V1_ACCEPTANCE_REPORT_CN.md`；
- `HtmlDashboardRenderer` 生成单文件双语 `V1_ACCEPTANCE_DASHBOARD.html`；
- `RelativeLinkBuilder` 是三者唯一链接生成入口；
- `AtomicOutputWriter` 用临时文件、`fsync` 和 `os.replace` 写输出。

## 6. 核心统计

两个 Markdown 和 Dashboard 首屏必须包含：

| 指标 | 当前真实证据预期值 | 机器计算规则 |
|---|---:|---|
| 验收场景数 | 4 | acceptance spec 的 case 数 |
| 真实 HLS 修复成功数 | 3 | `kind=hls` 且 case status PASS |
| 安全拒绝成功数 | 1 | `kind=safety` 且 case status PASS |
| 实际 LLM 调用总次数 | 3 | openai-compatible proposal 的已完成真实调用 |
| 输入 Token 总数 | 2700 | 三个真实 LLM action 的 provider input usage |
| 输出 Token 总数 | 676 | 三个真实 LLM action 的 provider output usage |
| 缓存输入 Token 总数 | 384 | provider cached-input usage；它是 input 子集 |
| Token 总数 | 3376 | input + output，不重复加入 cached input |
| CSim 调用总数 | 7 | ledger 中 csim 的终态 action 数 |
| Synth 调用总数 | 4 | ledger 中 synth 的终态 action 数 |
| CoSim 调用总数 | 3 | ledger 中 cosim 的终态 action 数 |
| HLS 工具调用总数 | 14 | csim + synth + cosim；LLM 单独统计 |
| Final CSim PASS | 3 / 3 | 三个 HLS final Candidate 的 csim check |
| Final Synth PASS | 3 / 3 | 三个 HLS final Candidate 的 synth check |
| Final CoSim PASS | 3 / 3 | 三个 HLS final Candidate 的 cosim check |
| 时钟约束 PASS | 3 / 3 | 三个 HLS final Candidate 的 clock check |
| Credits 总数 | 83 | 四个 ledger 终态 action 的 actual cost 总和 |
| Credits 总预算 | 320 | 四个独立 run 的 credit limit 总和 |
| Credits 剩余预算 | 237 | 四个独立 run 的 credits remaining 总和 |
| Token 总预算 | 131072 | 四个独立 run 的 token limit 总和 |
| Token 剩余预算 | 127696 | 四个独立 run 的 tokens remaining 总和 |

`PATCH_INVALID` 的 `static-patch-file` action 即使记在 `kind=llm` 的预算槽，也不计入
“实际 LLM 调用”。它的 provider、result reference 和 0 Token 必须共同证明是静态输入。

任何数据不可对账时，该指标为 `INVALID`，相关 check 为 FAIL；不得用 0 或预期值填充。

### 6.1 各场景预算与调用明细

报告正文同时显示：

| 场景 | 实际 LLM | Input | Output | Cached | Total | CSim | Synth | CoSim | HLS tools | Credits used | Credits remaining | Tokens remaining |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| compile | 1 | 1127 | 193 | 0 | 1320 | 2 | 1 | 1 | 4 | 26 | 54 | 31448 |
| functional | 1 | 407 | 226 | 384 | 633 | 2 | 1 | 1 | 4 | 26 | 54 | 32135 |
| synthesis | 1 | 1166 | 257 | 0 | 1423 | 2 | 2 | 1 | 5 | 30 | 50 | 31345 |
| patch_invalid | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 1 | 1 | 79 | 32768 |
| 合计 | 3 | 2700 | 676 | 384 | 3376 | 7 | 4 | 3 | 14 | 83 | 237 | 127696 |

这些数值是当前真实证据的预期断言，不是 renderer 常量。每个单元格必须由相应 run 的
provider result、ledger 和 budget snapshot 计算并交叉核对。

### 6.2 首屏人工审核总表

核心统计之后立即显示以下总体结论，不能要求审核者进入场景章节才看到：

| 审核项 | 机器判定来源 | 当前预期 |
|---|---|---|
| 总体状态 | recorded acceptance 与 recomputed checks 一致 | PASS |
| 三类 HLS 修复 | compile/functional/synthesis case checks | 3 / 3 PASS |
| 安全验收 | patch_invalid safety invariants | 1 / 1 PASS |
| Baseline 错误真实 | baseline action result、Vitis artifacts、phase/error 定位 | PASS |
| 真实模型且无 fallback | provider/model、LLM result ref、Manifest 中无 fallback artifacts | 3 / 3 PASS |
| Patch 最小且未改 testbench/接口 | changed paths、hunks、line counts、signature comparison、policy | 3 / 3 PASS |
| Final gates | Candidate csim/synth/cosim/clock action | 3 / 3 PASS |
| Token 与工具调用完整 | provider usage、ledger、budget snapshot、action records | 4 / 4 PASS |
| Candidate 提升或安全回滚 | registry lineage/best/final 或 safety rollback | 4 / 4 PASS |
| Manifest/Trace/Ledger/实际调用一致 | hashes、event ordering、action/result binding、accounting | 4 / 4 PASS |

“Patch 最小”采用可审计的结构定义：只修改允许的单个 kernel source、hunk/changed-line
数量在策略上限内、不做完整文件替换、顶层函数 signature/interface 不变。它不使用主观
文本判断。`PATCH_INVALID` 故意修改 testbench，应显示“拒绝行为 PASS”，不能混入
三个合法 Patch 的 3/3 统计。

## 7. 单场景审核内容

### 7.1 Baseline 失败

直接显示：

- failure stage 与 phase；
- error category 与 deterministic subtype；
- 精简关键错误日志；
- error file、line、column 和 symbol；
- 顶层函数；
- 诊断摘要和证据来源。

Subtype 和定位规则按一般模式解析，不按 run name 硬编码：

- compiler `file:line:column: error`；
- HLS `ERROR: [...] ... (file:line:column)`；
- public test mismatch；
- Patch policy violation；
- 无法结构化时使用 `UNKNOWN` 并使必需定位 check FAIL，而不是猜测。

错误日志只保留匹配到的 ERROR、编译定位、mismatch 和返回码摘要。绝对 run path、帮助
链接和重复 INFO 不进入报告。输出前执行 path scrubber。

### 7.2 Provider、Model 与 Token

直接显示：

- Provider、Model、revision；
- 是否真实 LLM-based；
- fallback 是否启用、触发及类型；
- LLM/static proposal 调用次数；
- input、output、cached-input 和 total Token；
- usage completeness；
- request/result 的相对 evidence ref。

### 7.3 Patch

直接显示：

- 修改文件；
- additions、deletions、hunks 和 change class；
- hypothesis、expected effect、risk；
- original/patched SHA-256；
- normalization 是否发生；
- Patch 展示策略。

展示策略按 `splitlines()` 后的总行数计算：

- 不超过 30 行：完整 unified diff；
- 超过 30 行：显示总行数、统计、前 30 行和明确的截断标记；
- renderer 使用 `diff` fenced block/HTML escaped `<pre>`；
- 报告展示的 Patch 必须与 machine result 中已验证的 applied Patch 一致；若存在
  `applied_patch`，优先展示它并同时披露 normalization。

### 7.4 Baseline 与 Final 对比

每个 HLS case 直接显示：

| Gate | Baseline | Final | Acceptance |
|---|---|---|---|
| CSim | status / phase | status / phase | PASS/FAIL |
| Synth | status / phase | status / phase | PASS/FAIL |
| CoSim | status / phase | status / phase | PASS/FAIL |
| Clock | target/estimated | target/estimated | PASS/FAIL |

完成 synthesis 时同时显示 latency、II、LUT、FF、DSP、BRAM、URAM 和 estimated
clock。不存在的 baseline 阶段显示 `NOT_RUN`，不得伪造成 FAIL 或 PASS。

### 7.5 Candidate 生命周期

直接显示：

- baseline ID 与 final Candidate ID；
- parent ID；
- immutable、VERIFIED、promoted、best 和 final 状态；
- code hash；
- 每个 Vitis action 绑定的 Candidate ID/code hash 是否一致；
- rollback 状态。

### 7.6 Acceptance checklist

每个 HLS case 至少展示：

- Manifest 完整性；
- task/error/phase 边界；
- baseline 不变；
- Provider/Model 允许；
- LLM usage 完整且非零；
- Patch policy 与 Candidate 物化合法；
- csim/synth/cosim PASS；
- clock PASS；
- Candidate/action/hash 绑定；
- registry best/final/promoted；
- ledger Token/credits/tool calls 对账。

每项显示 PASS/FAIL、observed、expected 和必要的相对 evidence ref。

## 8. `PATCH_INVALID` 安全审核卡

安全 case 独立于三类 HLS 修复，必须逐项显示：

1. workflow 为 `FAILED`；
2. stop reason 为 `PATCH_INVALID`；
3. 越权文件由 Patch 解析为 `kernel_tb.cpp`；
4. policy error 明确拒绝非 kernel source；
5. Candidate ID 未分配；
6. Candidate 目录未物化；
7. registry 只包含 `candidate_000`；
8. best/final 未被污染；
9. baseline hash 未变化；
10. Manifest 没有 Candidate source/Patch/metadata；
11. 没有 Candidate csim/synth/cosim STARTED 或 credits；
12. rollback 为 `candidate_000 -> candidate_000`；
13. 被拒绝 Patch 按 30 行规则展示。

任一不变量失败，安全 case 不能 PASS。

## 9. Trace 摘要

不嵌入 `trace.jsonl`。按顺序读取并只保留关键事件：

```text
RUN_STARTED
BASELINE_READY
baseline TOOL_STARTED/TOOL_COMPLETED
V1_DIAGNOSTIC_READY
LLM_COMPLETED or static proposal completed
CANDIDATE_MATERIALIZED or PATCH_REJECTED
candidate CSim/Synth/CoSim completion
V1_COMPLETED / rollback / terminal failure
```

连续 tool events 合并成 `stage + candidate + terminal status`。摘要保留原始事件顺序和
evidence ref，不复制完整 payload、action ID 列表或时间戳噪声。

## 10. 相对路径与输入只读保证

### 10.1 链接

所有 Markdown `href`、HTML `href` 和报告中的 evidence path 都通过：

```python
os.path.relpath(target.resolve(), runs_root.resolve())
```

再转换为 POSIX 路径。绝对路径、`file://`、反斜杠、path traversal 输出全部拒绝。
例如：

```text
v1-compile-final/v1_result.json
v1-synthesis-final-2/actions/<action-id>/result.json
v1-acceptance/acceptance_result.json
```

### 10.2 日志 path scrubber

日志摘要不得直接复制包含 `/home/ying/...` 的原始行。解析器只重建：

```text
kernel.cpp:5:23: use of undeclared identifier 'rhs'
```

生成后扫描两个 Markdown、HTML 和内存 review fields；出现输入绝对根或
`/home/` 前缀即失败。

### 10.3 只读输入

- 三个输出目标必须是 `runs/` 根目录中的普通文件，不得位于四个 evidence root 内；
- collector 只用 read API；
- 生成前记录四个 Manifest digest、现有 `acceptance_result.json` hash 及全部机器文件
  的 hash/size/mtime 快照；
- 生成后重新验证 Manifest 和 digest；
- 任一机器文件变化则整体 FAIL，并报告 `EVIDENCE_CHANGED_DURING_RENDER`；
- 生成器不得写入 `.json`、`.jsonl`、Manifest、action、Candidate 或实验子目录。

## 11. Markdown 信息架构

两个 Markdown 内容顺序一致：

1. 语言互链、Dashboard 链接；
2. overall status 与 review digest；
3. 核心统计；
4. 三类 HLS + safety 总矩阵；
5. 三个 HLS 场景审核卡；
6. `PATCH_INVALID` 安全审核卡；
7. 总预算/Token 分解；
8. 跨场景 Trace 摘要；
9. raw evidence index；
10. 离线复现命令与“未调用 LLM/Vitis”声明。

审核所需的主要字段不能放入折叠区。只有大型原始证据索引可以折叠或放在文末。

## 12. 静态 HTML Dashboard

`V1_ACCEPTANCE_DASHBOARD.html` 为单文件：

- CSS、少量语言切换 JavaScript 和 review data 全部内嵌；
- 不加载 CDN、网络字体、图片或外部脚本；
- 中英文切换只切换 label/说明，不改变证据值；
- 顶部展示 overall、case counts、Manifest integrity 和 digest；
- 核心统计使用响应式 cards；
- 场景锚点导航连接四个审核卡；
- baseline/final 和 checklist 使用表格；
- Patch 使用 escaped `<pre><code>`；
- raw evidence 使用相对 `<a href>`；
- PASS/FAIL 同时使用文本、符号和颜色；
- 提供 print CSS，打印时保留全部主要审核信息；
- 使用 `html.escape` 和安全 JSON 序列化防止 evidence 注入 HTML。

## 13. Fail-closed 行为

以下任一情况必须产生 check FAIL，不能凭报告模板继续 PASS：

- Manifest 缺失、损坏或 hash/size 不匹配；
- core JSON/JSONL 无法解析；
- error category、phase 或 task 不匹配；
- Provider/Model 不允许或真实 LLM usage 为 0/不完整；
- Patch policy、Candidate 物化或 lineage 异常；
- action 的 Candidate ID/code hash/backend 不一致；
- csim/synth/cosim/clock 不通过；
- registry best/final 不合法；
- ledger 的 calls、Tokens、credits 或 result digest 不一致；
- safety invariants 被破坏；
- 生成期间输入证据变化；
- 输出包含绝对路径。

证据不足时仍尽可能生成两个 Markdown 和 HTML，将未知字段显示为 `INVALID`，并列出失败
check。只有 invocation/spec/output 本身无效时才不生成报告。

## 14. 测试设计

### 14.1 单元测试

- `ReviewEvidence` 由 synthetic fixtures 聚合，renderer 不读取原始文件；
- 每个 acceptance check 同时覆盖 PASS 和 FAIL；
- compile/HLS/mismatch/policy 日志定位与 path scrubber；
- static provider 不计真实 LLM，openai-compatible 计数；
- cached input 是 input 子集，不重复计总量；
- ledger calls/credits 与 budget snapshot 对账；
- Patch 30 行完整、31 行前 30 行加截断标记；
- relative link 正常化与 traversal/absolute path 拒绝；
- 两个 Markdown 中英文关键字段一一对应；
- HTML escaping、语言区、锚点、表格和相对链接；
- 两次生成输出字节一致。

### 14.2 安全与失败测试

- 篡改 Manifest 文件后整体 FAIL；
- 篡改 ledger 后 accounting check FAIL；
- 篡改 Candidate binding 或 validation 后 HLS case FAIL；
- 创建 safety Candidate 目录/registry 条目/tool action 后 safety case FAIL；
- evidence 中注入 HTML、绝对路径或 Markdown 控制字符时安全转义/清洗；
- monkeypatch 网络与 subprocess 为立即失败，验收报告仍能生成，证明无 LLM/Vitis 调用；
- 所有 JSON、JSONL、Manifest、ledger、Trace、registry、action 和 Candidate 文件生成
  前后 hash/size/mtime 不变。

### 14.3 当前真实运行离线验收

在本机四个现有真实运行目录执行一次 `accept-v1`，断言：

```text
overall_status = PASS
cases = 4
real_hls_repairs = 3
safety_rejections = 1
real_llm_calls = 3
input_tokens = 2700
output_tokens = 676
cached_input_tokens = 384
total_tokens = 3376
csim_calls = 7
synth_calls = 4
cosim_calls = 3
credits = 83
final csim/synth/cosim/clock = 3/3
```

同时检查三个平铺人工审核文件存在、链接目标存在、无绝对路径，并再次验证四个
Manifest 和现有 `acceptance_result.json` hash。
该步骤只读已有证据，不调用 LLM/Vitis。

## 15. 文档与兼容性

- 同步生成英文 `V1_ACCEPTANCE_REPORT.md` 和中文 `V1_ACCEPTANCE_REPORT_CN.md`；
- CLI 帮助说明报告是离线证据聚合；
- readiness 记录本轮问题、方案、统计口径和无重跑保证；
- `acceptance_result.json` 及所有机器文件保持字节不变；
- 新增 `review-v1` 人工审核命令，与现有 `accept-v1` 机器验收命令分离。

## 16. 完成条件

只有同时满足以下条件才完成本轮：

1. `runs/V1_ACCEPTANCE_REPORT.md` 单文件包含全部批准的英文人工审核字段；
2. `runs/V1_ACCEPTANCE_REPORT_CN.md` 与英文版证据值和 PASS 结论一致；
3. `runs/V1_ACCEPTANCE_DASHBOARD.html` 静态、双语、无外部依赖；
4. 三份人工审核结果的 recorded/recomputed 状态与只读 `acceptance_result.json` 一致；
5. 当前真实统计与第 14.3 节一致；
6. 四个实验目录和 `v1-acceptance` 中全部机器文件未变化；
7. 输出没有绝对路径，链接均为存在的相对目标；
8. 全部快速测试、compileall 和 `git diff --check` 通过；
9. 没有 LLM、Vitis 或网络调用。
