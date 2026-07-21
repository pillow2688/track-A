# V3-E Experience 学习准备度

## Strategy Ranker: NOT_READY

- FAIL：each_mode_at_least_10
- PASS：harmful_recommendation_rate_at_most_5_percent
- PASS：key_subtype_has_positive_and_negative
- FAIL：leave_one_task_out_coverage_at_least_40_percent
- PASS：ranking_candidates_at_least_60

## CoSim Risk: NOT_READY

- FAIL：pass_and_fail_or_timeout_present
- PASS：real_cosim_samples_at_least_30
- PASS：single_family_share_at_most_50_percent

## Continue Predictor: NOT_READY

- FAIL：decisions_at_least_50
- PASS：improvement_and_no_improvement_present

## Evidence Selector: NOT_READY

- PASS：evidence_usage_and_outcome_attributable
- FAIL：planner_rounds_at_least_100

结论：现阶段不得训练复杂模型，应继续按 Coverage Queue 积累真实正例、失败和无收益样本。
