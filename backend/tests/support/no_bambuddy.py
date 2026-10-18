"""A window in which any call to Bambuddy fails the test (#3818 tier A, Task 1).

``forbid_bambuddy(spans, base_url)`` records two things while it is open:

- every HTTP request whose URL is on ``base_url`` (same scheme, host and port, and a path
  under ``base_url``'s), answered ``599`` so the code under test fails on it too; a
  caller that swallows the failure ("best effort") is still caught by the record;
- every finished span named ``bambuddy.*``: the client's own span for each call
  (``scadbuddy/bambuddy/client.py`` ``_call_span``, ``f"bambuddy.{operation}"``,
  ``SpanKind.CLIENT``).

On exit it raises :class:`BambuddyCalledError` naming each span and each request.

Two things it relies on, read in the code it runs against:

- The span check sees only spans the tracer samples. ``DEFAULT_SAMPLER`` drops a
  ``CLIENT`` span with no parent (``core/tracing.py`` ``NoParentlessClients``), so the
  window runs inside a root span of its own (``test.no_bambuddy``): a client span made in
  the window's own context has a parent and is kept. A client span made with no parent
  elsewhere (a task started from another context) is dropped by the sampler, and only
  the request record catches its call.
- The request check is a respx router (respx 0.23, ``respx/mocks.py``: the mocker asks
  each registered router in turn, and the first one that does not raise
  ``AllMockedAssertionError`` answers). Ours is moved to the front so no other router,
  an outer test's mock of Bambuddy included, can answer a Bambuddy request first; any
  other request it hands on to the other routers in their order, and to the network
  when there are none, so a strict outer router still refuses what it does not mock.
  An outer router's Bambuddy route is therefore never called inside the window: give
  that router ``assert_all_called=False`` if its setup routes are not all called before.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx
import respx
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from respx.models import AllMockedAssertionError, PassThrough, Route

from scadbuddy.core.tracing import span

#: What a Bambuddy request is answered with inside the window: no real status, so no
#: caller mistakes it for one of Bambuddy's answers.
FORBIDDEN_STATUS = 599


class BambuddyCalledError(AssertionError):
    """A Bambuddy span or request inside a :func:`forbid_bambuddy` window."""


@dataclass
class BambuddyRecord:
    requests: list[httpx.Request] = field(default_factory=list)
    spans: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"span {name}" for name in self.spans]
        lines += [f"request {request.method} {request.url.path}" for request in self.requests]
        return "\n".join(lines)


class _Forbidder(respx.MockRouter):
    """Records and refuses every request on ``base``; hands the rest on."""

    def __init__(self, base: httpx.URL, record: BambuddyRecord) -> None:
        super().__init__(assert_all_called=False, assert_all_mocked=False)
        self._base = base
        self._prefix = base.path.rstrip("/") + "/"
        self._record = record
        self._route = Route()

    def _forbidden(self, request: httpx.Request) -> bool:
        url = request.url
        return (
            url.scheme == self._base.scheme
            and url.host == self._base.host
            and url.port == self._base.port
            and (url.path.rstrip("/") + "/").startswith(self._prefix)
        )

    def _refuse(self, request: httpx.Request) -> httpx.Response:
        self._record.requests.append(request)
        return httpx.Response(FORBIDDEN_STATUS, text="forbid_bambuddy: no Bambuddy call here")

    def _others(self) -> list[respx.Router]:
        routers = self.Mocker.routers if self.Mocker else []
        return [router for router in routers if router is not self]

    def _pass(self, request: httpx.Request, refused: AllMockedAssertionError | None) -> PassThrough:
        if refused is not None:
            raise refused
        return PassThrough("forbid_bambuddy: not Bambuddy", request=request, origin=self._route)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self._forbidden(request):
            return self._refuse(request)
        refused = None
        for router in self._others():
            try:
                return router.handler(request)
            except AllMockedAssertionError as error:
                refused = error
        raise self._pass(request, refused)

    async def async_handler(self, request: httpx.Request) -> httpx.Response:
        if self._forbidden(request):
            return self._refuse(request)
        refused = None
        for router in self._others():
            try:
                return await router.async_handler(request)
            except AllMockedAssertionError as error:
                refused = error
        raise self._pass(request, refused)

    def start(self) -> None:
        super().start()
        if self.Mocker:
            self.Mocker.routers.remove(self)
            self.Mocker.routers.insert(0, self)


@contextmanager
def forbid_bambuddy(spans: InMemorySpanExporter, base_url: str) -> Iterator[BambuddyRecord]:
    """Fail on any Bambuddy span or any request to ``base_url`` made inside the block."""
    record = BambuddyRecord()
    spans.clear()
    router = _Forbidder(httpx.URL(base_url), record)
    try:
        with router, span("test.no_bambuddy"):
            yield record
    except Exception as error:
        # The code under test failed, most likely on a 599: name the call, not the symptom.
        _check(spans, record, cause=error)
        raise
    _check(spans, record)


def _check(
    spans: InMemorySpanExporter, record: BambuddyRecord, *, cause: Exception | None = None
) -> None:
    record.spans = [s.name for s in spans.get_finished_spans() if s.name.startswith("bambuddy.")]
    if record.requests or record.spans:
        raise BambuddyCalledError(f"Bambuddy was called:\n{record.summary()}") from cause
