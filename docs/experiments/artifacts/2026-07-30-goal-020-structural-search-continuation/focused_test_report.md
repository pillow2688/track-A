# 聚焦测试报告

执行时间：2026-07-30（本地，不调用 DeepSeek，不启动 Vitis）。

执行命令：

```bash
/home/ying/CompetitionTrackA/track-A/.venv/bin/python3 -m unittest -q \
  tests.test_structural_liveness_guard tests.test_v3_search_control \
  tests.test_v3_prototype tests.test_v3_openai_planner tests.test_openai_provider
/home/ying/CompetitionTrackA/track-A/.venv/bin/python3 -m unittest discover -s tests -t .
```

结果：均以退出码 0 完成。

覆盖内容包括：深度-only、残余多生产者、残余未初始化环、单向 SPSC、可证明初始
令牌、真实公开 020 源码的 Guard-before-tools 回放、Guard 后语义计数为零、新签名和
新策略族进展、未尝试族继续/无族安全停止、A2/A3 四种标签下 Guard 等价，以及三种
非 STRUCTURAL_FIX Mode 不获取新语义进展。
