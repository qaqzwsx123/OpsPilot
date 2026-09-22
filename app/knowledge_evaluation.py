"""知识库检索评测集：验证命中率、召回率和引用准确性。"""

from uuid import uuid4

from app.rag import KnowledgeRag

CASES = (
    ("P1 告警如何升级处理", "P1 告警处置 SOP"),
    ("设备离线应该检查什么", "设备离线排障手册"),
    ("消息队列积压如何处理", "消息队列积压处置指南"),
    ("发布失败后怎么回滚验证", "发布失败回滚与验证 SOP"),
    ("数据库连接池耗尽如何处置", "数据库连接池耗尽应急 SOP"),
    ("网络链路丢包延迟怎么排查", "网络链路丢包与延迟排障"),
    ("网关磁盘空间超过80%怎么办", "网关磁盘空间清理规范"),
    ("服务 CPU 和内存持续升高怎么处理", "服务 CPU 与内存异常排查"),
    ("发布前需要检查哪些内容", "发布变更前检查清单"),
    ("值班交接和事件升级要记录什么", "值班交接与事件升级规范"),
    ("指标异常波动如何判断根因", "指标异常波动分析手册"),
    ("高优工单什么时候响应", "工单优先级规范"),
)

# 作用：说明函数 run_knowledge_evaluation 的输入、输出与安全边界，避免调用方越过受控流程。
def run_knowledge_evaluation() -> dict:
    # 每条问题都要求期望文档出现在 Top-3 中，并记录排名作为证据。
    rag = KnowledgeRag(); results = []; evaluation_id = str(uuid4())
    for question, expected in CASES:
        hits = rag.search(question, 3); titles = [item["title"] for item in hits]
        rank = titles.index(expected) + 1 if expected in titles else None
        results.append({"question": question, "expected": expected, "titles": titles, "evidence": [{"rank": index + 1, "title": item["title"], "score": item["score"], "cited": item["title"] == expected} for index, item in enumerate(hits)], "rank": rank, "passed": rank is not None, "top_score": hits[0]["score"] if hits else 0})
    total = len(results); passed = sum(item["passed"] for item in results); reciprocal = sum(1 / item["rank"] for item in results if item["rank"])
    return {"evaluation_id": evaluation_id, "total": total, "passed": passed, "hit_at_1": round(sum(item["rank"] == 1 for item in results) / total * 100, 1), "hit_at_3": round(passed / total * 100, 1), "recall_at_3": round(passed / total * 100, 1), "mrr": round(reciprocal / total, 3), "citation_accuracy": round(sum(item["rank"] == 1 for item in results) / total * 100, 1), "evidence_coverage": round(sum(bool(item["evidence"]) for item in results) / total * 100, 1), "results": results, "manual_feedback": {"count": 0, "average_score": None}}
