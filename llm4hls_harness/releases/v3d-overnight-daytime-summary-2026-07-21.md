# V3-D 昨夜至今日白天执行总结

- 覆盖时间：2026-07-20 18:56 至 2026-07-21 白天
- 分支：`feat/v3d-overnight-execution`
- 本报告绑定的源码 HEAD：`a796da3fc50bb187cad68034c7dfc99e413f3837`
- 机器摘要：[`v3d-overnight-daytime-summary-2026-07-21.json`](v3d-overnight-daytime-summary-2026-07-21.json)
- 证据边界：真实本地 LLM/Vitis 结果；不声称 hidden grader 或最终官方分数

## 1. 一句话结论

这一轮已经把项目从“V3-C 四模式代码和 smoke 完成”推进到“V3-D 有 corpus、Oracle、批量评测、接口保护、Docker 和真实重复实验”。四种 mode 均已有真实 LLM + Vitis fresh final 成功证据；`deepseek-v4-pro` 的 12 次重复运行全部完成 fresh CSim/Synth/CoSim。当前唯一无法立即启动的模型实验是 Qwen，因为没有可达的 Qwen OpenAI-compatible 服务配置。

## 2. 昨晚主要完成了什么

| 时间/提交 | 里程碑 | 实际产物 |
|---|---|---|
| `d56a2ed` | Task-aware Graph | PhaseRouter、四种 mode、三类 Failure Evidence |
| `d1cf869` | Patch 落地修复 | 唯一 old-hunk 上下文重定位，解决正确 Patch 因行号偏移被拒 |
| `509ca99` | 真实 mode 缺口修复 | 修正 0-cycle、Synth failure 和 deadlock/timeout/FIFO evidence |
| `7559e30`、`807579b` | V3-D fast corpus | 28 题：REPAIR 8、SYNTH_FIX 6、STRUCTURAL_FIX 6、OPTIMIZE 8 |
| `457e886` | Corpus Oracle | fail-closed 任务准入和可恢复队列 |
| `df39593` | Batch Benchmark | 模型/任务/重复矩阵、失败继续、证据等级隔离 |
| `b44f559` | TopInterfaceGuard | 独立保护顶层函数签名和固定接口 |
| `9fb0d5f` | Clean-room | Dockerfile、依赖锁、环境模板、Vitis preflight 和复现入口 |
| `2bb7e1e`～`ea2b31c` | 提交证据 | 报告、QA、staging、12 个可移植真实 Vitis receipts |
| `35a31f0` | 新 backend probe | A01/A02 保留 XSIM 启动异常证据，未覆盖失败历史 |

累计 16 个提交完成了从 PhaseRouter 到 V3-D 交付底座的纵向推进。没有加入多 Agent、RL、RAG 或新的 Supervisor。

## 3. 今天白天补齐的真实任务模式

这些是“真实模型在本次 run 内生成 Patch，并由真实 Vitis 完成 fresh final”的原子证据，不是 scripted replay。

| Run | Mode | Baseline 分诊 | Tokens | Credits | LLM/C/S/Co | Wall | Fresh final |
|---|---|---|---:|---:|---:|---:|---|
| `v3c-real-projection-repair-a04-LOCAL_BUDGET_OVERRIDE` | REPAIR | CSim FAIL | 2043 | 31 | 1/3/2/1 | 83.919s | 全 PASS |
| `v3c-real-residual-structural-a01` | STRUCTURAL_FIX | CSim/Synth PASS，CoSim FAIL | 2216 | 71 | 1/3/2/3 | 127.148s | 全 PASS |
| `v3c-real-synth-fix-dynamic-a01` | SYNTH_FIX | CSim PASS，Synth FAIL | 1675 | 35 | 1/3/3/1 | 75.316s | 全 PASS |

projection 必须继续标记 `LOCAL_BUDGET_OVERRIDE`。它证明本地完整闭环成功，但不证明满足公开样例的 20-credit 上限。

OPTIMIZE 的独立成功 run `v3b_fast_dotproduct_live_deepseek_retry2` 仍有效：`1027 → 38 cycles`，27.026×，8128 Tokens、35 Credits，fresh final 全 PASS。

## 4. Corpus 与真实 Vitis Oracle

| 项目 | 结果 | 证据含义 |
|---|---:|---|
| V3-D fast corpus | 28 题 | 四种 mode 和多种 mutation，Planner 不可见 golden/hidden-like |
| Deterministic Oracle | 28 accepted / 0 rejected，134/134 checks | 只验证数据和编排，不是比赛成绩 |
| Portable real Vitis receipts | 12 anchors、四 mode 各 3、59/59 checks | 真实工具证据，不是模型成绩 |
| Artifact binding | 238 hashes | clean clone 可校验 receipt 与实现/任务的绑定 |

## 5. XSIM：从 A01/A02 失败到 A03 恢复

A01/A02 中 `015/016/017/028` 都在 XSIM 第一条 Tcl 执行附近出现内部异常，当时不能写成 RTL 算错。A03 使用全新 run/simulator 工作目录，并在非沙箱环境串行重跑：

- 4 accepted / 0 rejected；
- `015/016/017`：baseline CoSim 预期命中真实 FIFO deadlock，golden CoSim PASS；
- `028`：baseline/golden 均通过正确性，Synth 从 39 降到 6 cycles，6.5×；
- 总耗时 328.602 秒；
- A03 没有再次出现 XSIM internal exception。

这个证据支持“瞬态状态或运行隔离问题”，但不足以武断认定某个具体缓存文件就是根因。历史 A01/A02 报告保持不改，A03 作为后续恢复证据。

## 6. DeepSeek 重复矩阵

固定真实 `deepseek-v4-pro`、真实 Vitis 2025.2、同一 fast-experiment 验证策略和 fresh final。所有 run 使用独立目录，失败或无提升不会被覆盖。

### 6.1 官方三题

| Task | Runs | DONE | Fresh final | Tokens | Credits | LLM/C/S/Co | Wall |
|---|---:|---:|---:|---:|---:|---:|---:|
| projection | 3 | 3 | 3 | 6090 | 93 | 3/9/6/3 | 294.712s |
| residual | 3 | 3 | 3 | 6552 | 213 | 3/9/6/9 | 475.308s |
| dotProduct | 3 | 3 | 3 | 13712 | 115 | 6/11/11/3 | 630.719s |
| **合计** | **9** | **9** | **9** | **26354** | **421** | **12/29/23/15** | **1400.738s** |

### 6.2 dotProduct 三次结果

| Repeat | Baseline | Final | Acceleration | 关键决策 |
|---|---:|---:|---:|---|
| R01 | 1027 | 38 | 27.026× | 第一轮 518，第二轮继续改善到 38，final 全 PASS |
| R02 | 1027 | 518 | 1.983× | 第二 Candidate 出现 2684355097-cycle 异常高 latency，被拒绝并保留 incumbent |
| R03 | 1027 | 1027 | 1.000× | 没有严格改善，安全回退 baseline 并完成 fresh final |

这三次分别证明了继续搜索、拒绝退化 Candidate 和无收益安全回退，不应只报告最好的一次。

### 6.3 额外 SYNTH_FIX 重复

synth-fix 另跑 3 次，3/3 DONE 且 3/3 fresh final 全 PASS：4904 Tokens、105 Credits、244.903 秒。

全部 12 次矩阵合计：12/12 DONE、12/12 fresh final 全 PASS、31258 Tokens、526 Credits、LLM/CSim/Synth/CoSim 为 15/38/32/18，总 wall time 1645.641 秒。

## 7. Docker 与测试

源码/镜像基线 `a796da3fc50bb187cad68034c7dfc99e413f3837` 已重建为：

- `llm4hls-v3d:py3.12`；
- `llm4hls-v3d:git-a796da3fc50b`；
- Image ID：`sha256:48edd63ee1c7212aa62f9e42692542e29f9d014b0c747a2a40a776864a74636d`；
- revision label：`a796da3fc50bb187cad68034c7dfc99e413f3837`。

Docker daemon 正常，socket 仍保持安全的 `root:docker 660`。当前 Codex 进程通过 `sg docker` 使用已存在的组权限，没有执行 `chmod 666`。验证结果：

- 容器 `demo-smoke` PASS；
- 容器 quick tests：`Ran 382 tests; OK (skipped=3)`；
- 宿主 quick tests：`Ran 382 tests; OK`。

镜像是 Agent-only clean-room runtime，Vitis 2025.2 仍由宿主/Distrobox 外部提供；不能声称 Vitis 已被合法封装进镜像。

## 8. 现在仍未完成什么

1. Qwen3.5/Qwen3.6 没有运行。当前 endpoint 只暴露 `deepseek-v4-flash`、`deepseek-v4-pro`，本机也没有 Qwen serving、权重或监听端口。唯一外部阻塞是可达的 `OPENAI_BASE_URL`、`OPENAI_API_KEY` 和服务端真实 `LLM4HLS_MODEL` alias。
2. projection 完整闭环至少需要 31 Credits，而公开样例预算是 20；需要确认正式评分是否把 final closure 计入同一预算。
3. strict/fast 对照、Evidence/CoSim gate 消融和 hidden grader 仍未完成。
4. 最终提交格式、Docker/Vitis 部署口径、两页论文和视频仍需冻结。

## 9. 下一步优先级

1. 获得 Qwen3.5/Qwen3.6 的真实 serving 三项配置后，复用完全相同任务、Prompt、预算和超时各跑官方三题至少一轮；
2. 把本报告的矩阵数据纳入论文表和 5 分钟 Demo，同时保留均值、失败和 baseline fallback；
3. 运行 strict/fast 与两个最小消融，随后停止新增架构并进入提交冻结。

## 10. 证据校验方式

机器摘要为每个原始 `v3_prototype_result.json` 保存相对 run ID 和 SHA-256，但原始 `runs/` 按仓库规则不进入 Git。A03 使用已提交的脱敏 Oracle snapshot 和 source hash。摘要不包含 API key、Authorization header、license server、本机绝对路径或 hidden/golden 内容。
