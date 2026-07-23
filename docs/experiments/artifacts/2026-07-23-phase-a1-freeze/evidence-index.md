# Phase A.1 冻结证据索引

## 仓库与边界

- `initial-git-state.txt`：Phase A.1 开始前分支、工作区和起始 HEAD
- `initial-diff-stat.txt`：Phase A.1 开始前已跟踪 diff 统计
- `preexisting-files.txt`：预先存在用户文件、Router、Phase A 与 Phase A.1 四类边界

## Router 与离线验证

- `focused-router-tests.txt`：25 个 Router/语料聚焦测试
- `full-tests.txt`：提交前完整单元测试
- `compileall.txt`：提交前全包编译检查
- `git-diff-check-before-commit.txt`：提交前 whitespace 检查

Router 产品提交：

```text
e5ba32a032432579bb9daa8015d2f705cff93498
fix(router): restore baseline-fact phase routing
```

## 运行方案计算

- `proposed-frozen-run-config.json`：提案级冻结配置
- `next-real-run-options-decision.json`：方案 A/B 精确预算与决策
- `next-real-run-options-decision.md`：人读决策摘要
- `documentation-budget-validation.txt`：28 题分布、Tool cap、Credit 和文档口径断言

## 提交后证据

以下文件在文档冻结提交后生成：

- `full-tests-after-commit.txt`
- `compileall-after-commit.txt`
- `git-diff-check-after-commit.txt`
- `final-git-state.txt`
- `final-commit-summary.json`

## 预算边界

Phase A.1 实际预算：

```text
真实 LLM calls = 0
真实 Token = 0
CSim = 0
Synth = 0
CoSim = 0
Tool Credits = 0
```
