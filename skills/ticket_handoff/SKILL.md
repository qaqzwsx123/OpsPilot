---
name: ticket_handoff
description: 工单交接：检索交接规范，汇总未关闭工单，突出高优与待认领事项，并生成交接检查清单。
category: 工单协同
risk: auto
runnable: true
suggestions: 当前待处理工单|高优工单交接
triggers: 工单交接|工单交接清单|交接未关闭工单
tools: knowledge_search|ticket_query|unassigned_tickets|ticket_priority_summary
input: 可选负责人或交接班次说明
steps: 检索工单交接规范|查询未关闭工单|识别高优和未分配事项|按规范生成逐单交接检查清单
output: 工单列表、高优数量、未分配事项、交接规范来源和交接检查项
---

# 工单交接 SOP

1. 按优先级、负责人、当前状态和创建时间排序。
2. 高优工单必须写明最近动作、阻塞项、下一次更新时间和升级路径。
3. 交接人确认后在工单中补齐接手人和验证计划。
4. 已关闭工单不纳入当班交接，但需保留根因和验证记录。
