---
name: capacity_review
description: 容量巡检：结合非在线资产与 CPU 指标趋势，输出容量风险、验证重点和后续建议。
category: 容量管理
risk: auto
runnable: true
suggestions: 早间容量巡检|检查 CPU 与离线资产
triggers: 容量巡检|容量风险评估|早间容量检查
tools: asset_lookup|metric_catalog|latest_metric_samples
input: 可选区域或关注指标；默认检查 CPU 和非在线资产
steps: 汇总非在线资产|读取 CPU 指标趋势|区分当前异常与持续风险|生成验证建议
output: 非在线资产清单、指标趋势摘要、容量风险和后续检查建议
---

# 容量巡检 SOP

1. 识别离线、维护中或长时间未更新的资产。
2. 查看 CPU 当前值、24 小时均值和峰值，判断是瞬时还是持续压力。
3. 将指标波动与发布、流量和告警进行关联验证。
4. 容量不足时提出扩容评估或工单建议；不直接变更阈值、实例或生产配置。
