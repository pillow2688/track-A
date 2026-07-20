# 提交前 Checklist（非最终规则）

> 自动生成于 2026-07-20T16:31:43+00:00，事实来源：`evidence_manifest.json` 指定的 run JSON。
> 不读取 hidden/golden，不把脚本 Patch replay 计作真实 LLM。`TODO` 表示缺少实验，绝非 0。

> 本清单和 staging 都是内部候选，不代表官方最终提交格式已确认。

## 代码与运行

- [ ] 完整 unittest、`py_compile`、`git diff --check` 通过。
- [ ] deterministic 官方三题 smoke 可复现。
- [ ] 外部 Vitis 2025.2 preflight 通过。
- [ ] 所有真实 run 使用独立目录，失败 run 未被覆盖。
- [ ] fresh final CSim/Synth/CoSim 证据可追溯到 run ID。

## 实验完整性

- [x] dotProduct 真实模型 + Vitis：1027→38 cycles。
- [ ] projection post-fix 新真实模型闭环。当前只有失败 A01–A03 与 Patch replay。
- [ ] residual STRUCTURAL_FIX 真实模型闭环。当前只有 scripted replay + real Vitis。
- [ ] SYNTH_FIX 真实模型闭环。当前只有 scripted replay + real Vitis。
- [ ] DeepSeek 三次重复及 Qwen 相同配置矩阵：TODO：配置真实 endpoint/key/model 后，运行 DeepSeek 官方三题各3次及 Qwen 同配置矩阵；当前没有可报告的新矩阵平均值或成功率。
- [ ] strict / fast 和两项消融已运行；缺失处保持 TODO。
- [x] deterministic Oracle 28 accepted / 0 rejected（fixture only）。
- [ ] 四种 mode 各有 1 个有效真实 Vitis corpus anchor；不等同于真实 LLM Agent 成功。

## 脱敏与打包

- [ ] `python -m submission_tools.cli stage ...` 生成新的 `NOT FINAL` staging。
- [ ] staging 安全扫描为 0 findings。
- [ ] 不包含 `.env`、API key、Authorization token、license、用户名或本机绝对路径。
- [ ] 不包含 `runs/`、checkpoint、cache、临时文件或大文件。
- [ ] 不包含 `golden/`、`hidden_like/`、reference solution 或 mutation answer。
- [ ] Planner 输入专项扫描确认没有 golden/hidden-like 字段或路径。
- [ ] 官方最终目录、Docker/Vitis 部署和视频要求经最新规则人工确认。
