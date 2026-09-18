from __future__ import annotations

import unittest

from app.database import DB_PATH, seed_demo_data
from app.sql_agent import MetadataRetriever, SqlReviewer
from app.workflow import SqlAgentWorkflow


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if DB_PATH.exists():
            DB_PATH.unlink()
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


if __name__ == "__main__":
    unittest.main()

