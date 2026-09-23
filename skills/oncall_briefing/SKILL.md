---
name: oncall_briefing
description: 值班简报：聚合未关闭告警、工单、作业和待审批变更，生成交接优先级与检查清单。
category: 值班协同
risk: auto
runnable: true
suggestions: 当前值班简报|华东夜班交接
triggers: 值班简报|值班交接简报|生成交接报告
tools: knowledge_search|alert_query|ticket_query|work_order_query|approval_queue
input: 可选值班区域或班次说明
steps: 检索值班交接规范|汇总未关闭告警|汇总未关闭工单和待执行作业|汇总待审批事项|结合规范按风险生成交接清单
output: 告警、工单、作业、待审批摘要、交接规范来源和交接优先级
---

# 值班简报 SOP

1. 汇总未关闭告警，优先确认 P1 影响范围和当前负责人。
2. 检查高优工单、待执行作业以及待审批变更，明确阻塞项。
3. 为每项风险记录最近动作、下一次更新时间和升级路径。
4. 将简报同步给接班人；本 Skill 只读汇总，不会关闭告警或执行变更。
