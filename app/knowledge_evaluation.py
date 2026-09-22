"""知识库检索评测集：验证命中率、召回率和引用准确性。

本模块用于对知识库 RAG 检索做离线评测，核心目标：
1. 内置一组带“期望命中文档标题”的评测用例；
2. 复用真实 KnowledgeRag 检索，结果反映当前知识库和嵌入模型；
3. 计算 Hit@1、Hit@3、Recall@3、MRR、引用准确性等指标；
4. 为每条问题保留 Top-3 检索证据，便于人工复核和排查；
5. 返回结构可直接用于审计中心或评测页面展示。

安全边界：
- 只读检索，不修改知识库、不分块、不写库；
- 评测逻辑不绕过 KnowledgeRag，确保结果与线上一致；
- manual_feedback 仅作为占位字段，实际评分由上层通过数据库保存。
"""

from uuid import uuid4  # 为每次评测生成唯一 ID，便于保存人工评分并追溯

from app.rag import KnowledgeRag  # 真实 RAG 检索入口，评测直接复用


# 每项为（用户问题、期望命中的文档标题）；结果会计算 Hit@1、Hit@3、MRR 和引用准确性。
# 用例覆盖告警、设备、消息队列、发布、数据库、网络、磁盘、服务、值班、指标、工单等常见主题，
# 期望标题必须与知识库中的 title 完全一致，避免因标题差异导致假阴性。
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


def run_knowledge_evaluation() -> dict:
    """执行知识库检索评测并返回各项指标。

    流程：
    1. 为本次评测生成 evaluation_id，供人工评分保存和结果追溯；
    2. 创建 KnowledgeRag 实例，复用与线上相同的检索逻辑；
    3. 逐条问题调用 rag.search(question, 3)，取 Top-3 结果的标题；
    4. 计算期望文档在 Top-3 中的排名 rank：
       - 命中：rank = 索引 + 1（1 表示 Top-1）；
       - 未命中：rank = None，passed = False；
    5. 为每条用例保留 Top-3 证据（rank、title、score、cited），便于人工复核；
    6. 汇总整体指标：
       - total：用例总数；
       - passed：Top-3 命中数；
       - hit_at_1：Top-1 命中率（百分比）；
       - hit_at_3：Top-3 命中率（等价于 passed / total）；
       - recall_at_3：Top-3 召回率（此处与 hit_at_3 相同，按“每条仅一个正确文档”设定）；
       - mrr：平均倒数排名，衡量期望文档排名的平均质量；
       - citation_accuracy：引用准确性，此处定义为 Top-1 命中率；
       - evidence_coverage：有用例证据的覆盖率（此处恒为 100%）；
       - results：每条用例的详细结果；
       - manual_feedback：人工评分占位，由上层通过数据库回填。

    返回：
    - dict：评测结果汇总，可直接序列化给前端展示。

    安全边界：
    - 只读检索，不写入任何业务数据；
    - 不缓存检索结果，保证每次评测反映当前知识库状态；
    - 不绕过 KnowledgeRag，避免评测结果与线上行为不一致。
    """
    # 每条问题都要求期望文档出现在 Top-3 中，并记录排名作为证据。
    rag = KnowledgeRag()  # 复用真实检索器，保证评测与线上一致
    results = []  # 收集每条用例的检索证据
    evaluation_id = str(uuid4())  # 本次评测唯一 ID，供后续人工评分关联

    # 逐条执行评测用例。
    for question, expected in CASES:
        # 取 Top-3 检索结果；KnowledgeRag.search 返回按相关度排序的列表。
        hits = rag.search(question, 3)

        # 提取 Top-3 的标题，用于计算排名。
        titles = [item["title"] for item in hits]

        # 计算期望文档的排名：命中则 rank = 索引 + 1；未命中为 None。
        rank = titles.index(expected) + 1 if expected in titles else None

        # 记录本条用例的详细结果，包括检索证据，便于人工复核。
        results.append({
            "question": question,
            "expected": expected,
            "titles": titles,
            # evidence 中标记 cited 表示该条是否为期望文档，便于前端高亮。
            "evidence": [
                {
                    "rank": index + 1,
                    "title": item["title"],
                    "score": item["score"],
                    "cited": item["title"] == expected,
                }
                for index, item in enumerate(hits)
            ],
            "rank": rank,
            # Top-3 命中即视为通过；未命中则不计入通过数。
            "passed": rank is not None,
            # Top-1 分数便于排查低分命中或误命中。
            "top_score": hits[0]["score"] if hits else 0,
        })

    # 汇总指标。
    total = len(results)  # 用例总数
    passed = sum(item["passed"] for item in results)  # Top-3 命中数
    # MRR 分母为 total；只累加命中用例的 1/rank，未命中不计入。
    reciprocal = sum(1 / item["rank"] for item in results if item["rank"])

    # 返回汇总结果。
    # - hit_at_1：Top-1 命中率；
    # - hit_at_3 / recall_at_3：Top-3 命中率（每条仅一个正确文档时二者一致）；
    # - mrr：平均倒数排名，越大表示期望文档整体排名越靠前；
    # - citation_accuracy：此处定义为 Top-1 命中率，衡量首条结果是否直接给出正确引用；
    # - evidence_coverage：有用例证据的比例，当前实现恒为 100%；
    # - manual_feedback：人工评分占位，由上层查询数据库后回填。
    return {
        "evaluation_id": evaluation_id,
        "total": total,
        "passed": passed,
        "hit_at_1": round(sum(item["rank"] == 1 for item in results) / total * 100, 1),
        "hit_at_3": round(passed / total * 100, 1),
        "recall_at_3": round(passed / total * 100, 1),
        "mrr": round(reciprocal / total, 3),
        "citation_accuracy": round(sum(item["rank"] == 1 for item in results) / total * 100, 1),
        "evidence_coverage": round(sum(bool(item["evidence"]) for item in results) / total * 100, 1),
        "results": results,
        "manual_feedback": {"count": 0, "average_score": None},
    }