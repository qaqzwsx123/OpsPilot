---
name: capacity_review
description: 容量巡检：结合非在线资产与 CPU 指标趋势，输出容量风险、验证重点和后续建议。
category: 容量管理
risk: auto
runnable: true
suggestions: 早间容量巡检|检查 CPU 与离线资产
---

# 容量巡检 SOP

1. 识别离线、维护中或长时间未更新的资产。
2. 查看 CPU 当前值、24 小时均值和峰值，判断是瞬时还是持续压力。
3. 将指标波动与发布、流量和告警进行关联验证。
4. 容量不足时提出扩容评估或工单建议；不直接变更阈值、实例或生产配置。
