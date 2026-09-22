"""Disable Chroma product telemetry for this local-only demo."""

from chromadb.telemetry.product import ProductTelemetryClient
from chromadb.telemetry.product.events import ProductTelemetryEvent
from overrides import override


class NoopTelemetry(ProductTelemetryClient):
    """本地演示关闭产品遥测，避免索引服务产生外部遥测请求。"""
    @override
    def capture(self, event: ProductTelemetryEvent) -> None:
        return None
