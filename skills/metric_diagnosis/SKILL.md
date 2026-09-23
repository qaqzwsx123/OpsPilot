---
name: metric_diagnosis
description: 指标诊断：读取指定指标的 24 小时样本，计算当前值、均值、波动与处置建议。
category: 可观测性
risk: auto
runnable: true
suggestions: 指标 #1|指标 #6
triggers: 指标诊断|指标趋势分析|CPU 指标异常|指标波动分析
tools: metric_catalog
input: 指标编号，例如 #1；未提供时使用默认 CPU 指标
steps: 读取指定指标趋势|计算最新值、均值、最小值和最大值|依据波动生成验证建议
output: 指标名称、最新值、趋势统计、偏差和验证建议
---

# 指标诊断 SOP

1. 先确认当前值、24 小时均值、最大最小值和异常偏差。
2. 将异常时点与发布、流量变化、告警和设备状态关联。
3. 对超过阈值的指标创建告警或工单，并记录验证结论。
4. 不直接修改阈值、配置或生产数据。
