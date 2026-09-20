"""Disable Chroma product telemetry for this local-only demo."""

from chromadb.telemetry.product import ProductTelemetryClient
from chromadb.telemetry.product.events import ProductTelemetryEvent
from overrides import override


class NoopTelemetry(ProductTelemetryClient):
    @override
    def capture(self, event: ProductTelemetryEvent) -> None:
        return None
