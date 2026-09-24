"""智能查询的轻量条件完整性检查。

只拦截“查询某类数据”但完全没有限定维度的问题，避免因缺少条件而直接执行过宽查询。
这里不替模型猜测业务条件；用户补充后再把原问题和补充条件一起送入工作流。
"""

from __future__ import annotations

import re
from typing import Any


_ALL_SCOPE = ("全部", "所有", "全量", "不限", "整体", "汇总", "统计", "分布", "总数", "概览")
_COMMON_FILTERS = (
    "华东", "华南", "华北", "华中", "东北", "西北", "西南", "北京", "上海", "广州", "深圳",
    "p0", "p1", "p2", "p3", "高优", "中优", "低优", "严重", "紧急",
    "未关闭", "未处理", "待处理", "待执行", "处理中", "已关闭", "已确认", "已确认", "open", "closed", "pending",
    "离线", "在线", "维护", "异常", "正常", "状态", "最近", "最新", "过去", "近", "今天", "昨天", "本周", "本月",
    "小时", "分钟", "天", "周", "月", "id", "编号", "名称", "负责人", "按区域", "按状态", "按级别",
    "cpu", "内存", "磁盘", "网络", "延迟", "趋势", "数量", "多少", "排序", "top", "前",
)

_DOMAINS: tuple[tuple[str, tuple[str, ...], str, tuple[str, ...]], ...] = (
    ("alert", ("告警", "报警"), "你想筛选哪类告警？可以补充严重级别、状态、区域或时间范围。", ("未关闭的 P1 告警", "华东最近告警", "全部告警数量")),
    ("asset", ("资产", "设备", "网关", "机器人"), "你想查哪些设备？可以补充区域、运行状态或设备名称。", ("华东离线设备", "设备 2 的状态", "全部设备状态分布")),
    ("ticket", ("工单",), "你想筛选哪类工单？可以补充优先级、状态、负责人或区域。", ("未关闭的高优工单", "负责人 li 的工单", "全部工单数量")),
    ("work_order", ("作业", "作业单", "运维任务"), "你想查哪些作业？可以补充作业状态、设备编号或时间范围。", ("待执行作业", "设备 2 的作业", "最近作业")),
    ("metric", ("指标", "监控指标"), "你想查哪个指标？请补充指标名称或编号，并说明时间范围。", ("CPU 最近 24 小时趋势", "指标 1 的最新样本", "内存指标定义")),
)


def clarification_for_query(question: str) -> dict[str, Any] | None:
    """返回针对明显缺少筛选维度的数据问题的追问；足够具体时返回 None。"""
    compact = re.sub(r"\s+", "", question.casefold())
    matched = next((item for item in _DOMAINS if any(term in compact for term in item[1])), None)
    if not matched:
        return None
    if any(term.casefold() in compact for term in _ALL_SCOPE + _COMMON_FILTERS) or re.search(r"\d", compact):
        return None
    if not any(term in compact for term in ("查", "查询", "列出", "查看", "显示", "统计", "多少", "有哪些", "数据")):
        return None
    domain, _, prompt, examples = matched
    return {"domain": domain, "prompt": prompt, "examples": list(examples), "original_question": question}
