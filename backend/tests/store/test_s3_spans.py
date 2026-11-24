"""One CLIENT span per S3 request (#7245), with the kind of object and its size, never
the key, the bucket or a URL."""

from __future__ import annotations

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from scadbuddy.core.tracing import span
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.store.content_models import BlobScope
from scadbuddy.store.s3 import S3ContentBackend

SCOPE = BlobScope(slug="demo", title="Demo")


def _requests(spans: InMemorySpanExporter) -> list[ReadableSpan]:
    return [s for s in spans.get_finished_spans() if s.name.startswith("S3.")]


async def test_each_request_is_a_client_span_with_its_kind_and_size(
    s3: S3Settings, spans: InMemorySpanExporter
) -> None:
    backend = S3ContentBackend(s3)
    data = b"twelve bytes"
    try:
        # A parentless CLIENT span is dropped (spec §6): the request's span is the parent.
        with span("request"):
            key = await backend.upload("asset", data, name="x.svg", scope=SCOPE)
            assert b"".join([chunk async for chunk in backend.download(key)]) == data
            await backend.remove(key)
            assert not await backend.exists(key)
    finally:
        await backend.aclose()

    requests = _requests(spans)
    assert [s.name for s in requests] == [
        "S3.HeadObject",
        "S3.PutObject",
        "S3.GetObject",
        "S3.DeleteObject",
        "S3.HeadObject",
    ]
    for request in requests:
        assert request.kind is SpanKind.CLIENT
        assert request.attributes is not None
        assert request.attributes["rpc.system"] == "aws-api"
        assert request.attributes["scadbuddy.blob_kind"] == "asset"
        for value in request.attributes.values():
            assert not (isinstance(value, str) and (key in value or s3.bucket in value))
    head, put, get, _, gone = requests
    assert head.attributes is not None and head.attributes["scadbuddy.blob_found"] is False
    assert put.attributes is not None and put.attributes["scadbuddy.bytes"] == len(data)
    assert get.attributes is not None and get.attributes["scadbuddy.bytes"] == len(data)
    assert gone.attributes is not None and gone.attributes["scadbuddy.blob_found"] is False


async def test_a_listing_is_a_span_per_page(s3: S3Settings, spans: InMemorySpanExporter) -> None:
    backend = S3ContentBackend(s3)
    try:
        with span("sweep"):
            client = await backend.client()
            pages = [items async for items in backend._listed(client, f"{s3.prefix}/", "other")]
    finally:
        await backend.aclose()
    assert pages == [[]]
    (listing,) = _requests(spans)
    assert listing.name == "S3.ListObjectsV2"
    assert listing.attributes is not None and listing.attributes["scadbuddy.objects"] == 0
