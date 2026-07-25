# Phase C1：Experience Record V2 状态

日期：2026-07-23  
冻结 HEAD：`4a05763b593a527878a0056f64763126c58ee63b`  
组件状态：`PASS_PROMOTABLE`  
正式权限：`SHADOW`  
Guided：`NOT_ADMITTED`

## 结论

当前仓库已有的 `v3e.experience.v2` Schema、V1→V2 转换链和 103 条
Experience V2 记录通过独立离线冻结审计。C1 没有另造 Schema，也没有修改
主 Graph、预算、Candidate、fresh final 或 Experience 正式权限。

`PASS_PROMOTABLE` 只表示这批结构正确的数据可以作为 C2 离线评估输入并进入
人工 Shadow 审查；不表示 Experience Guided 已准入。

## 冻结数据

| 项目 | 结果 |
| --- | ---: |
| V2 记录 | 103 |
| Schema 校验通过 | 103 |
| 唯一 record_id | 103 |
| 唯一 run/candidate/round 身份 | 103 |
| 历史运行数 | 72 |
| task family | 25 |
| 可检索 | 76 |
| 可排名 | 76 |
| quarantine | 0 |
| Store SHA-256 | `ba452e1f57e433fae2727e614cb6ab2986ff4ae301762f852559bb909e004cb7` |

模式分布：

| Mode | 全部记录 | 可排名 |
| --- | ---: | ---: |
| REPAIR | 34 | 25 |
| SYNTH_FIX | 19 | 19 |
| STRUCTURAL_FIX | 8 | 6 |
| OPTIMIZE | 42 | 26 |

Outcome 分布为 SUCCESS 62、FAILURE 32、NO_IMPROVEMENT 9。所有 103 条记录的
evidence level 均为 `REAL_LLM_VITIS`，但它们是已有历史证据；C1 当次运行没有
调用真实 LLM 或 Vitis。

## 边界与缺口

- Record 只保存派生元数据，不保存源码、Patch 文本、Prompt 或日志正文。
- 未发现 API Key、Authorization、绝对主目录路径或 hidden/reference/golden
  Artifact 引用。
- C1 只审计记录，不作策略决策；C2 查询输入必须排除 `validation`、
  `performance`、`cost`、outcome、promoted 和 rejected。
- 全部 103 条记录的 task split 都是 train；必须由 C2 重新执行按 family/task
  留出评估，不能把训练内表现当作泛化证据。
- STRUCTURAL_FIX 只有 8 条、其中可排名 6 条，仍是最明显的数据缺口。

## 验证

- C1 聚焦测试：41/41 PASS。
- 全套测试：574/574 PASS。
- `compileall`：PASS。
- `git diff --check`：PASS。
- 审计器初版的字节类型与秘密模式误报在同一局部修复循环内修正；正式重跑通过。
- 当次真实预算：LLM calls 0、Token 0、CSim 0、Synth 0、CoSim 0、
  Tool Credits 0。

## 证据

- 审计摘要：
  `docs/experiments/artifacts/2026-07-23-c1-experience-record-v2/c1-audit-summary.json`
- 冻结 JSONL：
  `docs/experiments/artifacts/2026-07-23-c1-experience-record-v2/experience-record-v2-freeze.jsonl`
- 审计脚本：
  `docs/experiments/artifacts/2026-07-23-c1-experience-record-v2/c1_offline_audit.py`
- 专用测试：
  `llm4hls_harness/tests/test_c1_experience_record_v2_audit.py`

