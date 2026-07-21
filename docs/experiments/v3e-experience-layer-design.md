# V3-E 经验引导层：当前实现说明

更新时间：2026-07-21

## 结论

V3-E 已作为一层可插拔建议系统接到现有 V3-D 上。它不会改变 PhaseRouter、预算、工具权限、Candidate 晋升、CoSim gate 或 fresh final closure。经验层只能回答“历史上相似情况试过什么、成功率和代价如何”，最终 Patch 仍由 Planner 生成，所有结果仍由真实工具验证。

支持三种模式：

- `off`：完全关闭；Planner fingerprint 和旧 V3-D 保持兼容。
- `shadow`：计算并记录建议，但不把建议发送给模型；经验层故障时自动降级，不影响原流程。
- `guided`：只在存在可行动历史建议时，将有界摘要加入 Planner Context；空经验自动回退旧 Prompt。

默认模式是 `shadow`。

## 数据如何流动

```text
公开任务 + 当前 Evidence + 当前 Budget
                 |
                 v
        固定特征提取器
                 |
                 v
       train-only 相似案例检索
                 |
                 v
      贝叶斯策略排序 + 风险/继续建议
                 |
          Guidance Quality Gate
          INJECT / ABSTAIN
                 |
          off / shadow / guided
                 |
                 v
        现有 V3-D Planner 和 Graph
                 |
                 v
 Patch Validator -> CSim -> Synth -> CoSim gate -> Final closure
                 |
                 v
        Candidate 级经验记录与报告
```

经验只影响 guided 模式下的 Planner 输入，不拥有工具调用、预算、晋升或 final 权限。

## 主要文件与职责

| 文件 | 作用 |
|---|---|
| `llm4hls_agent/v3_experience.py` | 经验记录、查询、建议、Protocol 和安全校验的版本化契约 |
| `llm4hls_agent/v3_experience_store.py` | append-only JSONL、文件锁、幂等写入、崩溃尾部恢复、冻结快照 |
| `llm4hls_agent/v3_experience_guidance.py` | 固定特征提取、加权 kNN、贝叶斯排序、Risk/Continue advisory |
| `llm4hls_agent/v3_experience_quality.py` | 判断历史建议是否达到注入门槛；冲突或证据不足时 ABSTAIN，默认上限 600 tokens |
| `llm4hls_agent/v3_experience_importer.py` | 从历史真实 run 提取 Candidate 记录，排除 fixture/oracle |
| `llm4hls_agent/v3_experience_attribution.py` | 在 run 结束后幂等重建“模型是否采用建议、采用后结果如何” |
| `llm4hls_agent/v3_experience_loro.py` | Leave-One-Run-Out 离线评估覆盖率、命中率、有害建议率和失败抑制率 |
| `llm4hls_agent/v3_openai_planner.py` | 把经验层接入 Planner；保证 off 等价、shadow fail-open、guided 有界注入 |
| `llm4hls_agent/openai_provider.py` | 在三类 Planner Prompt 中渲染可选经验摘要 |
| `llm4hls_agent/v3_prototype_cli.py` | `--experience-mode/store/task-split` 入口和 run-local 经验导出 |
| `llm4hls_agent/v3_batch_benchmark.py` | 为批量实验冻结经验 seed，并绑定模型、profile、split 和实现指纹 |
| `llm4hls_agent/v3_experience_pilot.py` | 汇总 shadow/guided 12 题结果，缺失 slot 也保留为 `NOT_RUN` |
| `v3_experience_import_cli.py` | 历史经验导入命令 |

## 相似案例和排序

检索器只消费 `train` split 的真实 LLM/Vitis Candidate。`unknown`、`dev`、`hidden_like`、`test` 和 `holdout` 查询都不能读取非 train 经验。相似度不使用 task ID，主要比较：mode、失败/瓶颈、算法族、II、TripCount、transaction interval、资源压力及 stream/dataflow/FIFO/interface/bitwidth 特征。

排序按 `(mode, bottleneck/failure context, strategy bundle)` 聚合，Beta(1,1) 平滑后综合成功概率、收益、Credit、Token、时间和失败代价。它只输出推荐/避免的策略，不输出 Patch。

## 安全边界

- 不存源码、完整 Patch、日志、Prompt、API key 或绝对路径。
- task ID 仅保存不可逆哈希，不参与检索和排序。
- `hidden/golden/reference/mutation/answer/secret` 路径或字段在 schema 边界被拒绝。
- corpus manifest 中可能带答案含义的 `family/operator/mutation` 标签不进入 Planner。
- 非完整 `Final CSim + Synth + CoSim PASS` 不记作 final success。
- 只有真实、已物化 Candidate 进入默认统计；Patch rejection 只进入审计。

## Guidance Quality Gate

Gate 只允许同 mode、同已知 failure type 或 bottleneck、来自 train split 的真实 `REAL_LLM_VITIS` Candidate 提供支持。默认至少需要两条支持记录；当前轮已尝试策略、纯失败策略和与当前 Evidence 冲突的策略不能再次推荐。Oracle、golden、scripted、deterministic/demo fixture 都不能成为成功证据。

`ABSTAIN` 是正常安全结果，不是报错。此时 Planner 收到原始 Prompt，经验层只留下审计记录。`INJECT` 时也只加入不超过 600 tokens 的策略摘要，不加入源码、Patch、完整日志、task ID 或隐藏信息。

## 当前限制

完整 Candidate 经验目前在 run 终止后从哈希绑定 artifacts 重建；因此进程在终局前被强杀时，已完成 Candidate 的结构化经验可能要靠后续 importer 恢复。它不影响原有 Candidate/工具证据，但这是下一步最值得补的可靠性缺口。

当前只有 18 条可训练真实 Candidate。离线回放中只有 STRUCTURAL_FIX 达到注入门槛，且没有高置信度案例；因此最主要缺口不是继续增加算法，而是积累更多相同语义上下文下的真实成功和失败 Candidate。真实 12 题 shadow 仍被运行平台的外部数据边界阻止；详见 Quality Gate 与 pilot 报告。
