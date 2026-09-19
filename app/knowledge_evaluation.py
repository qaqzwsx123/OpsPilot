from app.rag import KnowledgeRag

CASES = (
    ("P1 告警如何升级处理", "P1 告警处置 SOP"),
    ("设备离线应该检查什么", "设备离线排障手册"),
    ("消息队列积压如何处理", "消息队列积压处置指南"),
    ("发布失败后怎么回滚验证", "发布失败回滚与验证 SOP"),
    ("数据库连接池耗尽如何处置", "数据库连接池耗尽应急 SOP"),
)

def run_knowledge_evaluation() -> dict:
    rag = KnowledgeRag(); results = []
    for question, expected in CASES:
        hits = rag.search(question, 3); titles = [item["title"] for item in hits]
        results.append({"question": question, "expected": expected, "titles": titles, "passed": expected in titles, "top_score": hits[0]["score"] if hits else 0})
    passed = sum(item["passed"] for item in results)
    return {"total": len(results), "passed": passed, "hit_at_3": round(passed / len(results) * 100, 1), "results": results}
