from __future__ import annotations

import unittest
from uuid import uuid4

from app.database import add_chat_message, add_evaluation_case, approve, approval_count, audit_count, audit_integrity, create_approval, create_chat_conversation, data_catalog, delete_chat_conversation, delete_evaluation_case, execute_approved, execute_readonly, get_chat_messages, import_metric_csv, knowledge_document_count, list_approvals, list_audit, list_chat_conversations, list_knowledge_documents, list_metric_definitions, list_metric_imports, metric_trend, reject_approval, seed_demo_data, seed_metric_demo_data, table_snapshot, write_audit
from app.evaluation import available_evaluation_cases, run_evaluation
from app.main import KnowledgeDocumentRequest, ToolInvokeRequest, create_knowledge, invoke_tool
from app.skills import SkillRegistry, run_skill
from app.tool_registry import invoke as invoke_registered_tool
from app.tool_planner import ToolPlanner
from fastapi import HTTPException
from pathlib import Path
from app.sql_agent import MetadataRetriever, SqlFixer, SqlReviewer
from app.workflow import SqlAgentWorkflow


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Keep tests safe to run while the local Uvicorn demo server is using SQLite.
        # The seed function is idempotent, so no shared database needs to be deleted.
        seed_demo_data()
        seed_metric_demo_data()

    def test_p1_alert_query_completes(self) -> None:
        result = SqlAgentWorkflow().run("查询最近的 P1 告警", "test-user")
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.rows), 2)
        self.assertIn("severity = 'P1'", result.sql or "")

    def test_offline_asset_query_uses_region(self) -> None:
        result = SqlAgentWorkflow().run("查询华东区离线设备", "test-user")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.rows[0]["region"], "华东")

    def test_agent_automatically_plans_and_executes_readonly_tools(self) -> None:
        result = SqlAgentWorkflow().run("查询华东 P1 告警关联设备", "test-tool-planner")
        plan = next(event for event in result.events if event.stage == "tool_plan")
        selected = {item["name"] for item in plan.details["tools"]}
        self.assertTrue({"alert_query", "asset_lookup"}.issubset(selected))
        tool_events = [event for event in result.events if event.stage == "tool"]
        self.assertTrue(tool_events)
        self.assertTrue(all(event.details["status"] == "completed" for event in tool_events))

    def test_tool_planner_never_selects_manual_or_blocked_tools(self) -> None:
        selected = ToolPlanner().plan("删除已关闭告警并维护数据库")
        self.assertTrue(all(item.name not in {"create_work_order", "close_alert", "database_maintenance"} for item in selected))

    def test_sop_question_uses_rag(self) -> None:
        result = SqlAgentWorkflow().run("P1 告警应该如何处理", "test-user")
        self.assertEqual(result.status, "answered_by_rag")
        self.assertIn("P1 告警处置 SOP", result.sources)

    def test_writer_rejects_write_sql(self) -> None:
        result = SqlAgentWorkflow().run("删除已关闭告警", "test-user")
        self.assertEqual(result.status, "approval_required")
        self.assertIsNotNone(result.approval_id)

    def test_reviewer_denies_unknown_table(self) -> None:
        reviewer = SqlReviewer()
        review = reviewer.review("SELECT * FROM secrets LIMIT 1;", ["alerts"])
        self.assertFalse(review.accepted)

    def test_retrieval_is_business_aware(self) -> None:
        tables = MetadataRetriever().retrieve("有没有未关闭的高优工单")
        self.assertEqual(tables[0].name, "tickets")

    def test_fixer_only_repairs_read_queries(self) -> None:
        fixed = SqlFixer().fix("SELECT id FROM alerts; DROP TABLE alerts;", ["只允许单条 SQL"], ["alerts"])
        self.assertEqual(fixed, "SELECT id FROM alerts LIMIT 100;")
        self.assertIsNone(SqlFixer().fix("DELETE FROM alerts;", ["只允许 SELECT 查询"], ["alerts"]))

    def test_metric_trend_query_completes(self) -> None:
        result = SqlAgentWorkflow().run("查询指标 #1 近24小时趋势", "test-user")
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.rows), 24)

    def test_real_metric_csv_import_validates_and_upserts_samples(self) -> None:
        metric_name = "CSV 导入测试 " + uuid4().hex[:8]
        content = (
            "metric_name,category,unit,asset_scope,observed_at,value,description\n"
            f"{metric_name},主机,%,test-gateway,2026-09-19T09:00:00+08:00,68.5,真实采集样本\n"
            f"{metric_name},主机,%,test-gateway,2026-09-19T10:00:00+08:00,72.1,真实采集样本\n"
        )
        report = import_metric_csv(content, "real-metrics.csv", "test-importer")
        self.assertEqual(report["sample_count"], 2)
        metric = next(item for item in list_metric_definitions(metric_name, limit=10) if item["name"] == metric_name)
        self.assertEqual(metric["source"], "csv")
        trend = metric_trend(metric["id"])
        self.assertIsNotNone(trend)
        self.assertEqual([point["value"] for point in trend["points"]], [68.5, 72.1])
        updated = import_metric_csv(content.replace("72.1", "75.0"), "real-metrics.csv", "test-importer")
        self.assertEqual(updated["created_metrics"], 0)
        self.assertTrue(any(item["id"] == updated["import_id"] for item in list_metric_imports()))
        self.assertEqual(metric_trend(metric["id"])["points"][-1]["value"], 75.0)

    def test_viewer_cannot_request_write_approval(self) -> None:
        result = SqlAgentWorkflow().run("删除已关闭告警", "test-viewer", role="viewer")
        self.assertEqual(result.status, "blocked")

    def test_safe_mode_approval_records_impact_without_deleting_rows(self) -> None:
        before = len(execute_readonly("SELECT id FROM alerts WHERE status = 'closed'"))
        approval_id = create_approval("test-approval", "DELETE FROM alerts WHERE status = 'closed';", "测试高风险变更")
        approve(approval_id, "test-approver", "已核对影响范围")
        result = execute_approved(approval_id, allow_writes=False)
        self.assertEqual(result["outcome"], "safe_mode")
        self.assertEqual(len(execute_readonly("SELECT id FROM alerts WHERE status = 'closed'")), before)
        approval = next(item for item in list_approvals() if item["id"] == approval_id)
        self.assertEqual(approval["status"], "approved_safe_mode")
        self.assertEqual(approval["impact_preview"]["matched_rows"], before)

    def test_approver_can_reject_pending_change_without_execution(self) -> None:
        approval_id = create_approval("test-reject", "DELETE FROM alerts WHERE status = 'closed';", "测试拒绝流程")
        rejected = reject_approval(approval_id, "test-approver", "维护窗口不满足要求")
        self.assertIsNotNone(rejected)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["decision_comment"], "维护窗口不满足要求")

    def test_audit_hash_chain_is_verifiable(self) -> None:
        write_audit("test-audit", "integrity_test", {"case": "hash_chain"})
        self.assertTrue(audit_integrity()["valid"])
        recent = audit_integrity(limit=1)
        self.assertTrue(recent["valid"])
        self.assertEqual(recent["scope"], "recent")
        self.assertEqual(recent["checked_events"], 1)

    def test_custom_evaluation_cases_can_be_selected_and_removed(self) -> None:
        custom = add_evaluation_case("自定义离线设备", "查询华东区离线设备", "completed", "test-evaluation")
        try:
            all_cases = available_evaluation_cases()
            self.assertTrue(any(item["id"] == custom["id"] and item["source"] == "custom" for item in all_cases))
            report = run_evaluation("selected", [custom["id"]])
            self.assertEqual(report["dataset_size"], 1)
            self.assertEqual(report["passed"], 1)
            self.assertEqual(report["cases"][0]["actual"], "completed")
        finally:
            self.assertTrue(delete_evaluation_case(custom["id"]))

    def test_data_explorer_uses_allowlisted_tables_and_bounded_rows(self) -> None:
        self.assertIn("assets", [table["name"] for table in data_catalog()])
        snapshot = table_snapshot("assets", limit=2)
        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot["rows"]), 2)
        self.assertIsNone(table_snapshot("sqlite_master"))

    def test_knowledge_library_has_baseline_documents_and_supports_pagination(self) -> None:
        self.assertGreaterEqual(knowledge_document_count(), 13)
        first_page = list_knowledge_documents(limit=5, offset=0)
        second_page = list_knowledge_documents(limit=5, offset=5)
        self.assertEqual(len(first_page), 5)
        self.assertEqual(len(second_page), 5)
        self.assertNotEqual(first_page[0]["id"], second_page[0]["id"])
        self.assertTrue(any(item["title"] == "发布失败回滚与验证 SOP" for item in list_knowledge_documents()))

    def test_tool_invocation_enforces_role_and_manual_tools_create_approval(self) -> None:
        with self.assertRaises(HTTPException) as denied:
            invoke_tool("close_alert", ToolInvokeRequest(role="viewer", requester="test-viewer"))
        self.assertEqual(denied.exception.status_code, 403)
        requested = invoke_tool("close_alert", ToolInvokeRequest(role="operator", requester="test-operator"))
        self.assertEqual(requested["status"], "approval_required")
        self.assertTrue(requested["approval_id"])

    def test_legacy_tool_call_without_request_body_is_safe_for_auto_tools(self) -> None:
        result = invoke_tool("alert_query")
        self.assertEqual(result["status"], "completed")

    def test_viewer_cannot_write_knowledge_base(self) -> None:
        with self.assertRaises(HTTPException) as denied:
            create_knowledge(KnowledgeDocumentRequest(title="无权写入", content="这条文档不应该被观察者写入知识库。", role="viewer"))
        self.assertEqual(denied.exception.status_code, 403)

    def test_operational_skills_have_metadata_and_structured_runtime_output(self) -> None:
        registry = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
        loaded = {skill.name: skill for skill in registry.load()}
        self.assertTrue(loaded["incident_triage"].runnable)
        self.assertEqual(run_skill("incident_triage", "华东 P1 告警")["status"], "completed")
        self.assertEqual(run_skill("metric_diagnosis", "指标 #1")["status"], "completed")
        self.assertEqual(run_skill("oncall_briefing", "当前值班简报")["status"], "completed")
        self.assertEqual(run_skill("capacity_review", "早间容量巡检")["status"], "completed")
        self.assertEqual(run_skill("change_review", "DELETE FROM alerts WHERE status = 'closed';")["risk"], "manual")

    def test_extended_readonly_tools_return_structured_operational_data(self) -> None:
        self.assertEqual(invoke_registered_tool("approval_queue")["status"], "completed")
        self.assertEqual(invoke_registered_tool("audit_recent")["status"], "completed")
        catalog = invoke_registered_tool("metric_catalog")
        self.assertEqual(catalog["status"], "completed")
        self.assertTrue(catalog["metrics"])

    def test_audit_and_approval_lists_support_bounded_pagination(self) -> None:
        write_audit("test-pagination", "pagination_test", {"sequence": 1})
        first_audit_page = list_audit(limit=1, offset=0)
        second_audit_page = list_audit(limit=1, offset=1)
        self.assertLessEqual(len(first_audit_page), 1)
        if second_audit_page:
            self.assertNotEqual(first_audit_page[0]["id"], second_audit_page[0]["id"])
        self.assertLessEqual(len(list_approvals(limit=10, offset=0)), 10)
        self.assertGreaterEqual(audit_count(), len(first_audit_page))
        self.assertGreaterEqual(approval_count(), len(list_approvals(limit=10, offset=0)))

    def test_agent_chat_conversation_persists_and_isolated_by_requester(self) -> None:
        conversation = create_chat_conversation("test-chat-user")
        self.assertIsNotNone(add_chat_message(conversation["id"], "test-chat-user", "user", "你好，帮我解释 P1 告警处理流程"))
        self.assertIsNotNone(add_chat_message(conversation["id"], "test-chat-user", "assistant", "请先确认影响范围。"))
        self.assertEqual(len(get_chat_messages(conversation["id"], "test-chat-user") or []), 2)
        self.assertIsNone(get_chat_messages(conversation["id"], "other-user"))
        self.assertIn(conversation["id"], [item["id"] for item in list_chat_conversations("test-chat-user")])
        self.assertFalse(delete_chat_conversation(conversation["id"], "other-user"))
        self.assertTrue(delete_chat_conversation(conversation["id"], "test-chat-user"))


if __name__ == "__main__":
    unittest.main()
