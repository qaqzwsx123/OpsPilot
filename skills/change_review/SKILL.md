---
name: change_review
description: 变更评审：对 SQL 或工具变更进行风险识别，给出影响确认、回滚与审批建议。
category: 变更安全
risk: manual
runnable: true
suggestions: DELETE FROM alerts WHERE status = 'closed';|SELECT * FROM alerts LIMIT 20;
---

# 变更评审 SOP

1. 明确变更对象、影响范围、执行窗口、负责人和回滚方案。
2. 写操作、工具变更、DDL 与多语句请求必须进入人工审批。
3. 审批前先做只读影响预估；审批意见与结论必须写入审计。
4. 默认安全模式下只保留审批演示，不直接写入业务数据。
