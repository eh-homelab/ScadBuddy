"""Task 2 replaces this file."""

from opentelemetry.sdk.trace.export import SpanExporter


class ScrubbingSpanExporter(SpanExporter):
    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner
