---
name: change_review
description: 变更评审：对 SQL 或工具变更进行风险识别，给出影响确认、回滚与审批建议。
category: 变更安全
risk: manual
runnable: true
suggestions: DELETE FROM alerts WHERE status = 'closed';|SELECT * FROM alerts LIMIT 20;
triggers: 变更评审|SQL 风险评审|评估这条 SQL|检查变更风险
tools:
input: SQL 或变更方案描述
steps: 识别操作类型和风险|说明影响与审批要求|给出回滚和验证检查项
output: 风险等级、评审理由、审批要求和回滚检查项
---

# 变更评审 SOP

1. 明确变更对象、影响范围、执行窗口、负责人和回滚方案。
2. 写操作、工具变更、DDL 与多语句请求必须进入人工审批。
3. 审批前先做只读影响预估；审批意见与结论必须写入审计。
4. 默认安全模式下只保留审批演示，不直接写入业务数据。
