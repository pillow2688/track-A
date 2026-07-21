# 提交前 Checklist（非最终规则）

> 自动生成于 2026-07-20T17:29:13+00:00，事实来源：`evidence_manifest.json` 指定的 run JSON。
> 不读取 hidden/golden，不把脚本 Patch replay 计作真实 LLM。`TODO` 表示缺少实验，绝非 0。
> 2026-07-21 的勾选项为人工核对补充；当前 generator 不会重建这些补充，
> 不可变事实入口是 `llm4hls_harness/releases/v3d-overnight-daytime-summary-2026-07-21.{md,json}`。

> 本清单和 staging 都是内部候选，不代表官方最终提交格式已确认。

> 2026-07-21 人工核对补充：真实验收、DeepSeek 矩阵和源码/镜像基线 Docker 结果见
> [`v3d-overnight-daytime-summary-2026-07-21.md`](../../llm4hls_harness/releases/v3d-overnight-daytime-summary-2026-07-21.md)。

## 代码与运行

- [x] 源码基线 `a796da3` 已完成 unittest 与 `py_compile`；本次纯文档整理按要求不重复全量测试，`git diff --check` 通过。
- [ ] deterministic 官方三题 smoke 可复现。
- [ ] 外部 Vitis 2025.2 preflight 通过。
- [x] 所有本轮真实 run 使用独立目录，失败 run 未被覆盖。
- [x] 本轮 12 个 DeepSeek 矩阵 run 的 fresh final CSim/Synth/CoSim 可追溯到 run ID 和结果 SHA-256。

## 实验完整性

- [x] dotProduct 真实模型 + Vitis：1027→38 cycles。
- [x] projection post-fix A04 新真实模型闭环；继续标记 `LOCAL_BUDGET_OVERRIDE`。
- [x] residual STRUCTURAL_FIX 真实模型闭环。
- [x] SYNTH_FIX 真实模型闭环。
- [x] DeepSeek 官方三题各 3 次重复，所有成功、拒绝和 baseline fallback 均保留。
- [ ] Qwen3.5/Qwen3.6 相同配置矩阵；缺可达 endpoint/key/model alias。
- [ ] strict / fast 和两项消融已运行；缺失处保持 TODO。
- [x] deterministic Oracle 28 accepted / 0 rejected（fixture only）。
- [x] 四种 mode 均有有效真实 Vitis corpus anchor；不等同于真实 LLM Agent 成功。

## 脱敏与打包

- [ ] `python -m submission_tools.cli stage ...` 生成新的 `NOT FINAL` staging。
- [ ] staging 安全扫描为 0 findings。
- [ ] 不包含 `.env`、API key、Authorization token、license、用户名或本机绝对路径。
- [ ] 不包含 `runs/`、checkpoint、cache、临时文件或大文件。
- [ ] 不包含 `golden/`、`hidden_like/`、reference solution 或 mutation answer。
- [ ] Planner 输入专项扫描确认没有 golden/hidden-like 字段或路径。
- [ ] 官方最终目录、Docker/Vitis 部署和视频要求经最新规则人工确认。
