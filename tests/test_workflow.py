from __future__ import annotations

import unittest

from app.database import DB_PATH, seed_demo_data
from app.sql_agent import MetadataRetriever, SqlFixer, SqlReviewer
from app.workflow import SqlAgentWorkflow


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Keep tests safe to run while the local Uvicorn demo server is using SQLite.
        # The seed function is idempotent, so no shared database needs to be deleted.
        seed_demo_data()

    def test_p1_alert_query_completes(self) -> None:
        result = SqlAgentWorkflow().run("查询最近的 P1 告警", "test-user")
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.rows), 2)
        self.assertIn("severity = 'P1'", result.sql or "")

    def test_offline_asset_query_uses_region(self) -> None:
        result = SqlAgentWorkflow().run("查询华东区离线设备", "test-user")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.rows[0]["region"], "华东")

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


if __name__ == "__main__":
    unittest.main()
