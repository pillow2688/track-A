# Full Agent 公开覆盖阻塞项修复状态（2026-07-28）

## 结论

本轮只修复两个确定性阻塞项：

1. Ledger 已完成的 provider-output-rejected LLM action 未被完整封入
   package/provenance；
2. unified-diff hunk 坐标错误只留下泛化拒绝，下一轮 Planner 看不到
   精确原因。

两项修复均保持 fail-closed。未调用 DeepSeek，未启动 Vitis HLS action，
未修改历史 run、Corpus、Prompt 主体、A1/A2/A3、Gate、Admission、Store、
Full Agent Manifest、预算或 `min/`。

## 本轮修改文件

产品代码：

- `llm4hls_harness/llm4hls_agent/repair.py`
- `llm4hls_harness/llm4hls_agent/v3_planner_action.py`
- `llm4hls_harness/llm4hls_agent/v3_prototype.py`
- `llm4hls_harness/llm4hls_agent/v3_openai_planner.py`
- `llm4hls_harness/llm4hls_agent/v3_batch_benchmark.py`

测试：

- `llm4hls_harness/tests/test_repair.py`
- `llm4hls_harness/tests/test_v3_live_planner.py`
- `llm4hls_harness/tests/test_v3_prototype.py`
- `llm4hls_harness/tests/test_v3_openai_planner.py`
- `llm4hls_harness/tests/test_v3_batch_benchmark.py`
- `llm4hls_harness/tests/test_v3_planner_action.py`

本状态记录：

- `docs/status/2026-07-28-full-agent-coverage-blocker-fixes.md`

工作树在本轮开始前已经包含大量未提交修改；本轮没有清理、覆盖或提交这些
既有修改。

## 问题一：provider failure package/provenance

### 当前合同

`completed LLM action` 的唯一集合口径现在来自 Agent Ledger：

```text
kind == llm
and state == COMPLETED
```

成功 `PROPOSAL` 与 `PROVIDER_OUTPUT_REJECTED` 使用同一集合合同。每个集合成员
都必须具有并封装：

- Planner input及其canonical hash；
- provider request及其canonical hash；
- STARTED journal；
- COMPLETED journal；
- 成功outcome或provider failure outcome及文件hash；
- Ledger STARTED/COMPLETED及Token绑定；
- provider failure对应的proposal rejection、拒绝原因及input/output hash绑定。

Package sealing、terminal package re-entry验证和外层benchmark provenance
validator都使用该口径。缺任一Artifact、集合不相等、schema错误、usage不一致、
拒绝原因不一致或hash不一致均拒绝。

每个action还必须满足生产侧的确定性identity合同：

- `logical_operation_id`由Planner fingerprint和input SHA-256派生；
- provider request audit绑定同一logical ID、fingerprint和input；
- request路径固定为`planner/requests/{logical_operation_id}.json`；
- `attempt_index`必须是严格整数`0`，不能以`false`或`0.0`代替；
- `retry_of=null`且`replay_policy=NON_REPLAYABLE`；
- action ID必须是完整action request的canonical hash。

成功和provider rejection还共享Token预留合同。若Ledger标记
`token_reservation_overrun`，或实际Token超过STARTED预留，首次执行、恢复、
package seal和外层provenance都会继续fail-closed，不能在重入时降级成普通
cached rejection。

外层provenance receipt中的`model_outcomes`现在显式区分：

- `PROPOSAL`
- `PROVIDER_OUTPUT_REJECTED`

失败action仍保留在Ledger和统计数量中，不能通过忽略failed action绕过。

### `v3d_fast_018` 离线检查

对既有run执行了只读action-chain验证：

```text
Ledger completed LLM actions: 4
底层完整审计链通过: 4/4
provider failure:
fcad4e82c585e28b1a44e2927cd20484bf8b825e9712cbdb398ae7fe43055d9b
reason: PATCH_INCOMPLETE
```

四个action也全部通过当前新增的deterministic identity、request路径、
NON_REPLAYABLE、Ledger预留和hash绑定检查。

旧Manifest相对新合同缺少：

```text
control/live_planner_actions/fcad4e82....started.json
control/live_planner_actions/fcad4e82....completed.json
control/proposal_rejections/round_001.json
planner/inputs/round_001.json
planner/requests/f3c5d403....json
```

provider failure outcome本身已在旧Manifest中；其余五项过去没有封入。

本轮没有原地改写旧Manifest、search result、frozen Candidate或B2 receipt。
原地重封会改变search-result SHA，并要求重新认证；因此旧终态仍保持历史
ERROR，标记为`RECERTIFICATION_REQUIRED`。

为验证“已有Artifact可重建”，另将上述审计链复制到临时目录，以当前
package/provenance函数离线重建（临时目录自动删除）：

```text
validated completed actions: 4
required chain artifacts: 21
provider failure packaged: true
proposal rejection packaged: true
result: PASS
```

新代码还证明相同的reject-once-then-success运行会完整seal，并可在terminal
re-entry时只读重验且不改变Ledger。

## 问题二：unified-diff hunk精确反馈

严格parser现在为hunk错误生成有界结构化证据，错误类型包括：

- `PATCH_HUNK_OLD_COUNT_MISMATCH`
- `PATCH_HUNK_NEW_COUNT_MISMATCH`
- `PATCH_HUNK_NEW_START_MISMATCH`

hunk证据包含：

- 文件名；
- 1-based hunk序号；
- hunk header；
- declared/actual old count；
- declared/actual new count；
- declared old/new start；
- 坐标错误时的expected new start；
- 一条短小、可操作的regenerate指令。

错误中的hunk header只保留坐标部分，不携带任意长度的上下文后缀。

Graph仍在Candidate分配前严格拒绝非法Patch。拒绝证据写入：

```text
evidence/failures/patch_round_NNN.json
```

`control/proposal_rejections/round_NNN.json`保存其ref/hash和精确error type。
下一轮Planner通过hash-bound history读取该证据，并在既有
`RECENT REJECTED CANDIDATE FAILURES`上下文中看到精确值。Prompt主体未修改。
Evidence ref和SHA必须成对存在，且其round、parent Candidate、Planner action、
Planner output和error type必须与proposal rejection一致，否则拒绝。

本轮没有新增Patch宽松接受，也没有新增自动坐标改写。既有确定性的hunk
count normalization和old-context唯一定位合同保持不变；最终
`apply_unified_diff`仍是权威严格校验器。

因此，纯old/new count错误在正式Graph中会先走既有的安全metadata
normalization：该步骤只重算count，不改变路径、start、hunk body或Patch语义，
随后仍必须通过严格apply。严格parser本身会拒绝未normalize的count mismatch并
给出declared/actual值。`v3d_fast_012`的真实问题是new-start坐标错误，既未被
normalizer修改，也未被relocator修改。

### `v3d_fast_012` 离线回放

使用既有两个Planner Patch和baseline source离线走当前
normalize/relocate/strict-apply路径：

```text
Round 1:
error_type=PATCH_HUNK_NEW_START_MISMATCH
file=kernel.cpp
hunk=2
header=@@ -19,7 +18,7 @@
declared_new_start=18
expected_new_start=17

Round 2:
error_type=PATCH_HUNK_NEW_START_MISMATCH
file=kernel.cpp
hunk=2
header=@@ -19,7 +18,7 @@
declared_new_start=18
expected_new_start=19
```

这两轮不再只能得到泛化的`PATCH_INVALID`或`PATCH_POLICY_REJECTED`。
非法Patch不创建Candidate，incumbent和Candidate Registry保持不变。

## 测试结果

用户指定的三个模块加package/provenance/repair补充模块：

```bash
../.venv/bin/python3 -m unittest \
  tests.test_v3_batch_benchmark \
  tests.test_v3_prototype \
  tests.test_v3_openai_planner \
  tests.test_v3_live_planner \
  tests.test_v3_planner_action \
  tests.test_repair
```

结果：

```text
Ran 138 tests
OK
```

完整测试：

```bash
../.venv/bin/python3 -m unittest discover -s tests -t .
```

结果：

```text
Ran 711 tests
OK
```

工作树检查：

```text
git diff --check: PASS
```

可选的`ruff`检查未执行，因为当前`.venv`没有安装`ruff`；这不影响上述
必需测试结果。

## 安全边界

- provider failure仍计入Agent Ledger；
- package与Ledger completed action集合必须完全相等；
- action identity、request audit和NON_REPLAYABLE语义必须一致；
- provider failure Token预留超限不能在重入时恢复成cached rejection；
- 删除failure request、failure outcome或rejection decision会拒绝；
- 篡改rejection hash或reason会拒绝；
- 成功与失败action混合时数量和Token保持一致；
- Patch错误仍在Candidate创建前拒绝；
- Patch failure evidence缺ref/SHA或跨轮身份不一致会拒绝；
- incumbent、Candidate Registry和合法回退没有放松；
- 没有模型调用、Vitis action或历史Artifact写入；
- 没有自动commit、merge或push。

## 下一步

当前代码和本地测试已具备申请以下定向真实重跑的条件，但本轮不执行：

```text
重跑v3d_fast_018一次
重跑v3d_fast_012一次
两题通过后继续019–028
```
