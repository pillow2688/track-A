# 官方 Reference Harness 与内部 V0 对比

状态：已核对<br>
核对日期：2026-07-14<br>
上游来源：<https://anonymous.4open.science/r/fpt26-harness><br>
本地公开快照：`_external/fpt26-harness/`<br>

## 结论

比赛提供的是 **Reference Agent & Evaluation Harness 示例实现**。`V0`、`V1` 至 `V4` 是本项目的内部工程里程碑，不是比赛官方阶段。

当前 `llm4hls_harness/` 不应被官方源码替换。它保留了官方公开 task package、`csim`、`synth`、`cosim`、ToolServer 计费顺序和 Vitis 2025.2 调用语义，同时把 V0 收窄为无 LLM 的确定性验证闭环，并增加持久化 ledger、幂等恢复、不可变 baseline 和可审计产物。

因此，两者的关系是：

```text
官方 reference harness
  ├─ 提供兼容语义、公开样例任务和完整示例 Agent
  └─ 作为开发对照，不作为运行时依赖

本项目内部 V0
  ├─ 复现公开 task + metered ToolServer + Vitis 闭环
  ├─ 增强预算、追踪、恢复和候选安全
  └─ 暂不实现 LLM、修复/优化循环、hidden grading 和评分
```

## 本地快照记录

本地目录由上游公开 ZIP 接口取得：

- 仓库根地址：<https://anonymous.4open.science/r/fpt26-harness>；
- ZIP 接口：<https://anonymous.4open.science/api/repo/fpt26-harness/zip>；
- 上游公开的最后更新时间：`2026-07-05T06:59:16.927Z`；
- 获取日期：`2026-07-14`；
- ZIP 大小：`35,684` 字节；
- 本次 ZIP SHA-256：`4cc583a26596ae44cbdb8b3c1cc631e1f89fca65dc47630663d844f7fa9206d2`；
- 解压后的公开文件数：33；
- 公开文件树 SHA-256：`a5a0ffc49cf0ef14b56a4be645648c8387c3748133de3794e304f7221380f8ff`。

公开文件树摘要的计算输入为：按相对路径排序后，将每个文件写成 `路径<TAB>文件 SHA-256`，再使用 LF 连接并计算 SHA-256。它用于确认当前本地公开快照是否变化。

安全过滤遵循项目既有规则：没有解压、打开或查看任何 `hidden/` 内容；三个 `tasks/*/reference/*.cpp` 黄金答案也没有解压、打开或查看。因此，本地快照可以提供三个公开 task package，但不能运行官方的 `scripted` 黄金答案回放和 hidden grading。这是刻意的安全边界。

匿名镜像没有公开完整 Git commit、稳定归档校验值或 `ETag`。同一内容的 ZIP 容器也可能因打包元数据不同而产生不同摘要，所以本次 ZIP SHA-256 只能校验本地下载文件，不能充当官方 revision。该仓库根目录和 README 也没有提供 harness 许可证；复制、再发布或提交到本仓库前需要比赛方补充许可说明。`_external/` 已被 `.gitignore` 排除，不会随本项目提交。

## 总体范围对比

| 维度 | 官方 reference harness | 本项目内部 V0 |
| --- | --- | --- |
| 定位 | 最小完整示例：Agent、工具、评分 | 第一个可验证工程切片 |
| 是否官方比赛阶段 | 否；官方仓库也只称其为示例实现 | 否；V0 是团队内部名称 |
| Python | 3.12 标准库 | 3.11 及以上标准库 |
| Task package | 加载公开文件，也加载可用的 hidden 与 reference | 只加载公开 metadata、kernel、header、description、public TB |
| Python API | 导出 `Task`、`Budget`、`ToolServer`、`ReferenceAgent`、`grade` 等 | 使用 `PublicTask`、持久化 `BudgetLedger` 和扩展结果结构；不是 drop-in API，需要显式 adapter |
| 配置优先级 | task 中的 `[target]` 可覆盖相关环境默认值 | CLI > 环境变量 > task metadata > 默认值；行为不同，自动化必须显式传参 |
| Agent | LLM repair → synth → optimize 循环 | 无 LLM，只验证不可变 baseline |
| LLM backend | OpenRouter、ScriptedClient | 无，Token 使用量固定为 0 |
| ToolServer | `csim/synth/cosim` 调用前扣 credit，内存 transcript | 调用前持久化 `STARTED`，完成后对账并写结构化 ledger/trace |
| 默认费用 | csim=1、synth=4、cosim=20 | 默认相同，CLI/环境可覆盖 |
| 预算维度 | 统一 credits | credits、每工具次数、Token、总运行时间 |
| 工具结果 | 结构化 phase、截断日志、报告对象 | phase、证据、artifact 引用与摘要、稳定 action ID、缓存标记 |
| 重复动作 | 再次运行并再次收费 | 完成动作按内容寻址复用，不重复调用或计费 |
| Baseline | 作为 Task 中的起始代码字符串 | 单独只读快照，并逐字节与摘要复核 |
| 候选管理 | `best`/`verified` 为循环内变量 | 持久化 `candidate_registry.json`；V0 只有 `candidate_000` |
| 运行恢复 | 没有持久恢复协议 | ledger、result、registry、trace 支持幂等恢复和歧义动作记录 |
| Vitis 流程 | `vitis-run --mode hls`，两阶段 csim，synth/cosim 报告解析 | 保持相同核心 Tcl 与 phase 语义，增加报告存在性、时钟有效性和产物审计 |
| cosim 策略 | 正确性阶段主要用于 `requires_cosim=true` 的任务 | 为满足 V0 验收，baseline 一律执行 csim → synth → cosim |
| hidden grading | `scoring.py` 在预算外运行 | V0 明确不实现，也不读取 hidden |
| PPA 评分 | 示例 scorecard 与 acceleration 公式 | V0 只保存 synth 指标并检查最低频率，不计算比赛分数 |
| 容器 | 提供示例 `vitis.dockerfile` 和 `run-vitis.sh` | V0 当前使用 WSL 中的本地 Vitis 2025.2 |
| CLI | `scripts/run_poc.py`，执行 Agent 后再 grading | `python -m llm4hls_agent run` / `llm4hls-v0`，输出机器可读摘要 |

## 模块映射

| 官方模块 | 本地 V0 模块 | 说明 |
| --- | --- | --- |
| `llm4hls/config.py` | `ToolConfig`、`BudgetConfig`、`RunConfig`、`cli.py` | 本地按职责拆分，并把配置纳入缓存与运行产物 |
| `llm4hls/task.py` | `llm4hls_agent/task.py` | 兼容公开 task 字段；本地用 `PublicTask` 强制公开输入边界 |
| `llm4hls/budget.py` | `llm4hls_agent/budget.py` | 从内存 credit 计数扩展为带锁、追加式、多维 ledger |
| `llm4hls/vitis.py` | `llm4hls_agent/vitis.py` 的 `SubprocessRunner` | 负责超时、进程组和输出捕获 |
| `llm4hls/tools.py` | `VitisBackend`、`BackendResult` | csim、synth、cosim 的实际 Tcl 与报告判定 |
| `llm4hls/report.py` | `parse_synth_report`、`parse_cosim_report` | V0 规模较小，暂未拆成独立模块 |
| `llm4hls/harness.py` | `llm4hls_agent/tools.py` 的 `ToolServer`、`ToolResult` | 对 Agent/工作流暴露计费工具接口 |
| `llm4hls/agent.py` | `llm4hls_agent/workflow.py` 的 `run_v0` | 只是职责位置对应，不是算法等价；本地当前只有确定性 baseline 状态机 |
| `scripts/run_poc.py` | `cli.py`、`__main__.py`、`llm4hls-v0` | 本地 CLI 更强调可复现产物与退出码 |
| `llm4hls/llm.py` | 无 | 属于后续内部里程碑，不在 V0 提前实现 |
| `llm4hls/scoring.py` | 无 | hidden grading 属于外部评测边界 |
| `vitis.dockerfile` | 无 | 容器复现留到后续竞赛硬化阶段 |

## 已对齐的公开契约

### Task package

本地 loader 支持三个官方公开样例中的字段：

- `task_id`、`task_type`、`difficulty`；
- `top`、`kernel_file`、`header_files`、`public_tb`；
- `budget`、`requires_cosim`、`initial_condition`；
- `[target].part`、`[target].clock_ns`。

本地不会实例化官方 `Task` 类，也没有在包顶层导出官方的 `ReferenceAgent`、`grade` 等对象，因此不是 Python API 级别的 drop-in replacement；兼容对象是磁盘上的公开 task package。官方 README 列出的 `task_type` 枚举未包含样例实际使用的 `structural`，本地将其作为元数据字符串处理，所以 `residual_stream_deadlock` 可以正常加载。

两边的目标配置优先级也不同：官方 loader 优先采用 task 的 `[target]`；`part` 缺失时才回退到全局环境默认值，而 `clock_ns` 缺失时直接回退到字面量 `5.0`。本地采用 CLI、环境变量、task metadata、默认值的顺序。这是本地为可复现实验提供的显式覆盖能力，不应描述为完全相同的配置 API。

### ToolServer 与结果 phase

两边都暴露：

```python
server.csim(kernel_code)
server.synth(kernel_code)
server.cosim(kernel_code)
```

共同 phase 为 `pass`、`compile_error`、`runtime_fail`、`synth_error`、`cosim_fail`、`timeout`。本地额外使用 `tool_error` 表示基础设施异常，避免把缺失报告或执行器错误伪装成 kernel 失败。

本地 `ToolResult` 增加 `action_id`、`candidate_id`、`code_hash`、`tool_config_hash`、`task_fingerprint`、artifact 摘要和 `cached`，因此结果语义兼容但数据类字段并非完全相同。

### Vitis 调用

两边的核心行为一致：

- 使用 `vitis-run --mode hls --tcl`；
- csim 先运行 `csim_design -setup`，再单独执行 `csim.exe`，从而区分编译失败和功能失败；
- synth 使用 `csynth_design` 并解析 `csynth.xml`；
- cosim 先综合，再运行 `cosim_design`，并解析 `<top>_cosim.rpt`；
- 所有外部进程都有 timeout，死锁不会无限等待。

本地进一步要求 synth 报告存在、可解析且包含有限的有效时钟周期；缺失报告不能成为 PASS。

## V0 刻意没有实现的能力

以下缺失不是漏做，而是当前范围边界。官方参考实现已经提供、但本地 V0
刻意不搬入的能力包括：

1. `ReferenceAgent` 的 repair/optimize 循环；
2. OpenRouter 和 scripted LLM backend；
3. 读取或回放 `reference/*.cpp`；
4. hidden test、`grade()` 和 scorecard；
5. Docker 竞赛环境封装。

另一些能力属于本项目后续内部里程碑，官方示例本身也没有完整提供：

1. candidate tree、受限 Patch、多轮验证和多目标 PPA 搜索；
2. LangGraph、checkpointer、持久 State 和可恢复副作用节点。

这些能力必须按内部 V1 至 V4 的边界逐步实现，不能提前塞入 V0。

## 本地 V0 的工程增强

相对官方最小示例，本地 V0 已增加：

1. **不可变 baseline**：保存独立只读副本，并在结束时复核内容、摘要和写权限；
2. **权威 ledger**：`STARTED → execute → durable result → COMPLETED`，预算拒绝发生在 Vitis 之前；
3. **幂等动作**：稳定 SHA-256 action ID 绑定代码、公开 task、工具配置、工具链和 backend；
4. **artifact 完整性**：result、Tcl、stdout、stderr、XML 和报告都有摘要，缓存命中时重新校验；
5. **结构化失败**：预算、工具和报告错误会进入 workflow result、registry 和 trace；
6. **多维预算**：统一 credits 之外，还限制每工具调用数、Token 和总运行时间；
7. **并发与崩溃防护**：运行目录锁、ledger 锁、尾部修复和歧义动作状态；
8. **可复现 CLI**：固定退出码和机器可读 JSON 摘要。

## 仍需关注的差异与风险

1. **上游不可固定 revision**：匿名镜像会更新且不公开完整 commit。本地只能记录获取时间和公开文件树摘要；正式提交前应向比赛方索取固定版本。
2. **许可证缺失**：当前上游没有声明 harness 许可证，所以 `_external/` 只作本地对照，不提交或再发布。
3. **示例环境存在版本线索冲突**：README 指向 Vitis 2025.2，但 `vitis.dockerfile` 的基础镜像以及 `run-vitis.sh` 的默认路径仍带有 2023.2。当前项目以比赛 README 和实际 WSL Vitis 2025.2 验证结果为准。
4. **cosim 调用策略不同**：官方 Agent 主要对 structural task 使用 cosim；本地 V0 为建立统一验收证据，对所有 baseline 都执行 cosim。这是内部 V0 策略，不应误写成官方要求。
5. **公开快照不是官方 POC 的完整可运行副本**：安全过滤后没有黄金答案和 hidden 输入，不能运行 scripted backend 或官方 grading；公开 task 的 csim/synth/cosim fixture 不受影响。
6. **官方 cosim 退出码风险**：官方示例主要以 cosim report 判定成功，若进程非零退出但遗留 `Pass` report，可能误判。本地 V0 已显式拒绝该组合并加入回归测试，同时提升 backend 指纹以使旧缓存失效。
7. **未来的多源 task 需要 adapter**：官方 raw CSimTool 会把传入文件映射中的额外 `.c`、`.cc`、`.cpp` 加入仿真，本地 task schema 当前只显式组装 kernel 和 public TB。若正式 task 引入辅助 C/C++ 源文件，需要先扩展公开 task 契约和哈希绑定。
8. **综合 PASS 不等于完整 PPA**：本地已经强制 synth report 和有效时钟存在，但 latency 或部分 resource 字段仍可能为空。后续排序和评分前必须再验证所需指标，不能把任意 synth PASS 写成“完整 PPA 已获得”。
9. **CLI 不是兼容入口**：官方 `run_poc.py` 默认执行 scripted Agent 和预算外评分，本地 CLI 只运行 V0 并使用不同退出码。现有官方自动化脚本不能未经修改直接调用本地包。

## 使用方式

本地实现继续保持自包含：生产代码不 import、搜索或硬编码 `_external/fpt26-harness`。需要使用官方公开样例时，只通过 CLI 显式传入 task 目录：

```bash
REPO=/mnt/c/path/to/track-A
PROJECT="$REPO/llm4hls_harness"
TASKS="$REPO/_external/fpt26-harness/tasks"

cd "$PROJECT"
python3 -m llm4hls_agent run "$TASKS/dotProduct_optimize" \
  --run-dir "$PROJECT/runs/v0-dotproduct"
```

这使官方仓库保持“开发 fixture 和兼容性事实源”的角色，而不是运行时依赖或被复制进参赛 Agent 的实现。
