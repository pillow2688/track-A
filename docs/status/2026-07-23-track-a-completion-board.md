# Track A 完成看板（2026-07-23，2026-07-24 更新）

| 项目 | 状态 | 证据 / 下一道 Gate |
|---|---|---|
| P0 Router | DONE | baseline-fact authority 已恢复；Router commit `e5ba32a032432579bb9daa8015d2f705cff93498` |
| Phase A 离线审计 | DONE | 72/72 历史 run 已完成离线审计 |
| Phase A.1 Git 冻结 | DONE | Router 已独立提交；中文验收和运行提案进入文档冻结提交 |
| 历史 72-run 审计 | DONE | 72/72 定位；Manifest、Ledger、复用与 repeat pair 已核对 |
| 28 题 inventory | DONE | 28/28；历史覆盖 12/28 |
| 当前版本影响分析 | DONE | `GLOBAL_BEHAVIORAL` 28/28 |
| Phase B1 原始 8000 Token 计划 | BLOCKED / 历史负结果 | 原报告保持不变：REPAIR `8000<8511`，STRUCTURAL_FIX `8000<10562`，0/5 Anchor PASS |
| Phase B1.1 Token 预检 | DONE | 五题最低需求和 128-token headroom 已离线复核；全部低于公开单题 32,768 Token 上限 |
| Revised frozen budget | DONE | per-task cap：8,639 / 9,201 / 10,691 / 10,802 / 9,782；plan digest `330c6e7334e59408e14db51a40a1ee3b35d4bc45b95d5460c61c3b0750e5ee5a` |
| Five real anchors | DONE | Phase B1.1 四类 PASS，加 Phase B1.2 稳定公开 STRUCTURAL_FIX 补证 PASS；同一 HEAD 下五种实际 Mode 5/5、fresh final 三件套 5/5 |
| REPAIR Anchor | DONE | `v3d_fast_002` 首次遇到 TRANSIENT_MODEL_TRANSPORT，按规则一次全新目录重试后 REPAIR final 三件套 PASS |
| SYNTH_FIX Anchor | DONE | `v3d_fast_010` actual Mode=SYNTH_FIX；fresh final 三件套 PASS |
| STRUCTURAL_FIX Anchor | DONE | Phase B1.2 使用公开 `residual_stream_deadlock`；当前 HEAD 三次 baseline 均 CSim/Synth PASS、CoSim FAIL；Router=STRUCTURAL_FIX；DeepSeek + fresh final 三件套 PASS |
| OPTIMIZE Anchor | DONE | `v3d_fast_022` actual Mode=OPTIMIZE；latency 9→2，4.5×；Performance-Area=TRADEOFF，Power=UNSUPPORTED |
| Empty Stub Anchor | DONE | `track_a_empty_stub_generation` actual Mode=REPAIR；真实生成、fresh final CSim/Synth/CoSim 全 PASS |
| 28 题正式矩阵 | DONE / MEASURED_FAILURES | 当前冻结 HEAD、DeepSeek、Vitis、1 repeat 已完成；28/28 唯一终态，23 DONE / 1 FAILED / 4 ERROR，E2E 82.14% |
| 16 题独立 gap run | DROPPED_AS_DEFAULT | 避免与正式 28 题矩阵重复；只保留工程参考清单 |
| Continuation V1 | IMPLEMENTED / SHADOW | 主路径能力保留；runtime default=`off`，当前接受权限只到 Shadow |
| Continuation V2 offline | DONE / INSUFFICIENT_EVIDENCE | 矩阵后固定协议：27 完整绑定；OPTIMIZE=23、REPAIR=1、STRUCTURAL_FIX=3、SYNTH_FIX=0 |
| Continuation V2 Shadow Pilot | NOT_READY | V1 beneficial retention 4/10、waste block 10/17；V2 retention 10/10、waste block 0/17，Mode/essential evidence 仍不足 |
| Continuation Enforce | DISABLED | Phase C0 未接主 Graph，禁止正式停止权限 |
| Experience Record V2 | DONE / PASS_PROMOTABLE | 矩阵新增 31 条，与冻结 103 条分池合并为 134 条；102 条可排名；只准隔离离线分析 |
| Experience Guided | NOT_ADMITTED | authority 仍为 SHADOW；C1 数据通过不等于 Guided 准入 |
| Bayesian Strategy Ranker V2 | DONE / NEGATIVE_RESULT | 矩阵后固定协议：LOTO/LO-family coverage 38.24%，harmful 15.38% > 5%；不晋升、不调阈值 |
| Ranker Training | NOT_READY | V1 保持 SHADOW/ABSTAIN；未训练 learned Ranker |
| 多模型矩阵 | TODO / NOT_STARTED | 单模型五类 Anchor Gate 已通过；本阶段未授权启动 |
| 当前 HEAD 重复统计 | TODO | 单次正式基线已完成；需先修 4 个 P0 执行器 ERROR，再按同协议做 repeats |
| 复现和 staging | TODO | 正式单次基线已冻结；待失败修复和 repeats 后开展 |
| 论文和视频 | TODO | 等待正式证据与提交口径冻结 |

Proposal 完成不计为真实实验完成。Phase B1 历史验收仍为 `BLOCKED`，Phase B1.1 历史验收仍为 `PARTIAL / PARTIALLY_ACCEPTED`；Phase B1.2 在同一 HEAD 下用稳定公开任务补齐 STRUCTURAL_FIX，单题验收 `ACCEPTED`。当前冻结版本的 28×1 已完成，但并非 28/28 成功：23/28 E2E，失败已按 1 个 REPAIR 策略问题、2 个 Candidate 终态绑定问题和 2 个 latency 终态问题分层。多模型矩阵尚未启动。最新统一结论见 `docs/status/2026-07-24-formal-matrix-results-and-horizontal-update.md`。
