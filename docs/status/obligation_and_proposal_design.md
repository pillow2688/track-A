# Obligation 与 Proposal 实验设计

Mode 仍是粗路由：`REPAIR`、`SYNTH_FIX`、`STRUCTURAL_FIX`、`OPTIMIZE`。每轮新增一个确定性的主 Obligation，例如 `FUNCTIONAL_CORRECTNESS`、`SYNTHESIS_LEGALITY`、`RTL_LIVENESS`、`INTERFACE_PROTOCOL`、`TIMING_LEGALITY` 或 `RESOURCE_LEGALITY`。

Planner Patch 在持久化前被投影为 `v3.proposal-experiment.v1`：

- `target_obligation`
- `hypothesis`
- `action_family`
- `expected_effect`
- `validation_plan`
- `failure_criteria`
- `fallback`
- `experiment_sha256`

历史通用 Provider JSON 合同保持兼容；**当前 task-aware Provider** 则必须直接输出
这些字段，并由确定性控制层校验 target obligation、mode、validation plan、Patch、
接口和重复实验关系后才能物化 Candidate。旧 Artifact 仍可按兼容路径读取，但
不得借用新字段伪造已发生的实验。Live task-aware Prompt 同时收到
`SEARCH CONTROL (MANDATORY)`，要求满足 `required_next_change`，而 A3 仍只是一条
可拒绝的 Strategy Card。

Planner-visible 状态另外包含 CandidateState（验证层级、CSim/Synth/CoSim、时钟、
资源和风险）及三个 Portfolio 引用：Verified Best、Active Probe、Fallback Parent。
失败 Probe 之后默认选择已验证父节点；只有另有已验证局部改进的明确证据时，才有
资格成为后续 parent。

父节点默认是 Verified Best；结构性失败后若没有其他已验证候选，明确记录从 baseline/Verified Best 重新分支，而不会从失败 Candidate 叠加 Patch。所有 Candidate 仍保持 `parent_id`、代码 hash 和 provenance。
