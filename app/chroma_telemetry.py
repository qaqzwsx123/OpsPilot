"""Disable Chroma product telemetry for this local-only demo."""

from chromadb.telemetry.product import ProductTelemetryClient
from chromadb.telemetry.product.events import ProductTelemetryEvent
from overrides import override


class NoopTelemetry(ProductTelemetryClient):
    """Chroma 遥测空实现。

    本项目的 Chroma 只作为本地可重建索引，不需要把集合创建、查询等产品事件发送到外部服务；
    通过配置这个实现保持离线运行，同时不影响 Chroma 的本地存取能力。
    """

    @override
    def capture(self, event: ProductTelemetryEvent) -> None:
        # 明确丢弃事件；参数保留是为了匹配 Chroma 的回调协议。
        return None
