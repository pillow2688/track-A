# Track A 完成看板（2026-07-23）

| 项目 | 状态 | 证据 / 下一道 Gate |
|---|---|---|
| P0 Router | DONE | baseline-fact authority 已恢复；Router commit `e5ba32a032432579bb9daa8015d2f705cff93498` |
| Phase A 离线审计 | DONE | 72/72 历史 run 已完成离线审计 |
| Phase A.1 Git 冻结 | DONE | Router 已独立提交；中文验收和运行提案进入文档冻结提交 |
| 历史 72-run 审计 | DONE | 72/72 定位；Manifest、Ledger、复用与 repeat pair 已核对 |
| 28 题 inventory | DONE | 28/28；历史覆盖 12/28 |
| 当前版本影响分析 | DONE | `GLOBAL_BEHAVIORAL` 28/28 |
| 五类真实 Anchor | TODO / AWAITING_APPROVAL | Gate B1 提案已完成，本阶段未运行 |
| 28 题正式矩阵 | TODO / AWAITING_APPROVAL | Gate B2 方案 A/B 待用户选择 |
| 16 题独立 gap run | DROPPED_AS_DEFAULT | 避免与正式 28 题矩阵重复；只保留工程参考清单 |
| Continuation | SHADOW / INSUFFICIENT_EVIDENCE | 10 候选点、9 完整绑定、1 排除；禁止 enforce |
| Experience | SHADOW | 81 Candidate records；`training_ready=false` |
| Strategy Ranker | BAYESIAN_SHADOW / TRAINING_NOT_READY | 81 Candidate records；未训练 learned Ranker |
| 多模型矩阵 | TODO / BLOCKED | 正式单模型 Gate 未批准，不能启动 |
| 复现和 staging | TODO | 等待正式同版本矩阵后开展 |
| 论文和视频 | TODO | 等待正式证据与提交口径冻结 |

Proposal 完成不计为真实实验完成。下一阶段状态为 `AWAITING_USER_APPROVAL`。
