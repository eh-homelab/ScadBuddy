# Tracing, backend core (PR #1 of #988) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The API and render worker emit OpenTelemetry traces: one trace per render
from the HTTP request through Temporal to every `openscad` call, coalesced submits
linked to the render they joined, Bambuddy calls as client spans that inject nothing,
and no forbidden value (parameter values, exception messages, Bambuddy detail) in any
exported span.

**Architecture:** One module, `scadbuddy/core/tracing.py`, builds the process's
`TracerProvider` from the standard `OTEL_*` variables and exposes the helpers every
other file uses (`span`, `current_traceparent`, `link_to`, `use_traceparent`). A
`ScrubbingSpanExporter` (`core/trace_scrub.py`) sits in front of the OTLP exporter.
Instrumentation is FastAPI (per app, with excluded paths), psycopg (global), and
Temporal's `TracingInterceptor` (on the one `connect` both the API and worker use);
render stages and `openscad` calls get manual spans at their existing choke points
(`timed_stage`, `run_openscad`).

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, temporalio 1.33,
`opentelemetry-sdk` 1.45, `opentelemetry-exporter-otlp-proto-http` 1.45,
`opentelemetry-instrumentation-fastapi`/`-psycopg` 0.66b0, pytest.

**Spec:** `docs/superpowers/specs/2026-10-01-distributed-tracing-design.md` (§3, §4,
§5.1, §6, §8 backend part, §9 row 1). Rows 2–5 (relay, agent, frontend, dashboard)
get their own plans.

## Global Constraints

- Only standard `OTEL_*` variables configure tracing; no `SCADBUDDY_` alias (§3).
- No `OTEL_EXPORTER_OTLP_ENDPOINT`: provider installed, spans created and dropped, no export attempted (§3).
- `OTEL_SDK_DISABLED=true`: no SDK provider at all; no spans; `render_jobs.traceparent` stays NULL (§3).
- Propagator: W3C Trace Context only, no baggage (§4).
- Trace context never leaves ScadBuddy: no process-wide httpx instrumentation; the Bambuddy client injects no headers (§4, §5.1).
- `service.name` is `scadbuddy-api` (API, including the in-process worker, which adds resource attribute `scadbuddy.worker.inprocess=true`) or `scadbuddy-worker`; `service.instance.id` is `socket.gethostname()`; `service.version` is `Settings.version`; `scadbuddy.revision` is `Settings.revision` (§3).
- Default sampler: parent-based, root sampler drops a `CLIENT` span with no parent; `OTEL_TRACES_SAMPLER`, when set, replaces it (§6).
- Never recorded: parameter values, OpenSCAD source and stderr, headers/cookies/query strings, SQL parameter values (sqlcommenter off), anything from Bambuddy beyond the status code, any exception message (§6).
- Not traced: `/healthz`, `/metrics`, `/telemetry/v1/traces`, via `excluded_urls` set in code (§6).
- `scadbuddy.failure_class`: an `ApiError`'s problem `type`, or `http-<status>` when that is `about:blank`; otherwise the exception's class name (§6).
- Migrations: a NEW file `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a merged one (CLAUDE.md).
- `uv run --frozen ruff check .`, `ruff format --check .`, `mypy` (strict) and `pytest` must pass (CLAUDE.md "Commands").

## Review Focus

1. **Collector unreachable** (endpoint set, nothing listening): the API starts, serves, and renders; export errors are logged by the SDK, never raised into a request. Test in Task 3.
2. **A garbage `traceparent` request header** (from any client): ignored; the request gets a fresh root trace and a normal response. Test in Task 3.
3. **A pending row written before the migration** (`traceparent` NULL) that a new submit coalesces onto: no link, no error, the submit answers as before. Test in Task 6.
4. **`OTEL_SDK_DISABLED=true`**: the app runs, renders work, rows carry no `traceparent`. Test in Task 1 (provider) and Task 6 (row).
5. **A chained exception whose cause carries a forbidden value** (`raise ApiError(...) from ParameterValueError(...)`): neither message survives the scrub. Test in Task 2.

---

### Task 1: Dependencies and `core/tracing.py`

**Files:**
- Modify: `backend/pyproject.toml` (dependencies), `backend/uv.lock` (by `uv add`)
- Create: `backend/scadbuddy/core/tracing.py`
- Test: `backend/tests/test_tracing.py`

**Interfaces:**
- Produces (used by every later task):
  - `TRACER_NAME: Final = "scadbuddy"`
  - `DEFAULT_SAMPLER: Sampler`: `ParentBased(root=NoParentlessClients())`
  - `def tracing_disabled() -> bool`
  - `def build_provider(service_name: str, *, version: str, revision: str, inprocess_worker: bool = False, exporter: SpanExporter | None = None) -> TracerProvider`
  - `def configure_tracing(service_name: str, *, version: str, revision: str, inprocess_worker: bool = False) -> None`
  - `def span(name: str, *, kind: SpanKind = SpanKind.INTERNAL, attributes: Mapping[str, AttributeValue] | None = None, links: Sequence[Link] = ()) -> AbstractContextManager[Span]`
  - `def failure_class(error: BaseException) -> str`
  - `def current_traceparent() -> str | None`
  - `def link_to(traceparent: str | None) -> Link | None`
  - `def use_traceparent(traceparent: str | None) -> AbstractContextManager[None]`

- [ ] **Step 1: Add the dependencies**

```bash
cd backend
uv add 'opentelemetry-sdk>=1.45,<2' 'opentelemetry-exporter-otlp-proto-http>=1.45,<2' \
  'opentelemetry-instrumentation-fastapi>=0.66b0' 'opentelemetry-instrumentation-psycopg>=0.66b0' \
  'temporalio[opentelemetry]>=1.33.0,<1.34'
```

`pyproject.toml` lists `temporalio>=1.33.0,<1.34` twice (lines 19 and 21). Replace
both with the single `temporalio[opentelemetry]>=1.33.0,<1.34` line `uv add` wrote,
then `uv lock` and confirm `uv run --frozen python -c "import temporalio.contrib.opentelemetry"`
exits 0.

- [ ] **Step 2: Write the failing tests**

```python
# backend/tests/test_tracing.py
"""core/tracing.py: the provider, the sampler and the context helpers (spec 2026-10-01 §3, §6)."""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from scadbuddy.core import tracing
from scadbuddy.core.problems import ApiError


def _provider(monkeypatch: pytest.MonkeyPatch, **env: str) -> tuple[TracerProvider, InMemorySpanExporter]:
    for name in ("OTEL_TRACES_SAMPLER", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_RESOURCE_ATTRIBUTES"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    exported = InMemorySpanExporter()
    provider = tracing.build_provider("scadbuddy-api", version="1.2.3", revision="abc123")
    provider.add_span_processor(SimpleSpanProcessor(exported))
    return provider, exported


def test_the_resource_names_the_service_build_and_host(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _provider(monkeypatch, OTEL_RESOURCE_ATTRIBUTES="deployment.environment=prod")
    attributes = provider.resource.attributes
    assert attributes["service.name"] == "scadbuddy-api"
    assert attributes["service.version"] == "1.2.3"
    assert attributes["scadbuddy.revision"] == "abc123"
    assert attributes["service.instance.id"]
    assert attributes["deployment.environment"] == "prod"
    assert "scadbuddy.worker.inprocess" not in attributes


def test_the_inprocess_worker_is_marked_on_the_api_resource() -> None:
    provider = tracing.build_provider(
        "scadbuddy-api", version="v", revision="r", inprocess_worker=True
    )
    assert provider.resource.attributes["scadbuddy.worker.inprocess"] is True


def test_a_parentless_client_span_is_dropped_and_a_child_one_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, exported = _provider(monkeypatch)
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("SELECT", kind=SpanKind.CLIENT):
        pass
    with tracer.start_as_current_span("request", kind=SpanKind.SERVER):
        with tracer.start_as_current_span("SELECT", kind=SpanKind.CLIENT):
            pass
    names = [(s.name, s.kind) for s in exported.get_finished_spans()]
    assert names == [("SELECT", SpanKind.CLIENT), ("request", SpanKind.SERVER)]


def test_otel_traces_sampler_replaces_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, exported = _provider(monkeypatch, OTEL_TRACES_SAMPLER="always_on")
    with provider.get_tracer("t").start_as_current_span("SELECT", kind=SpanKind.CLIENT):
        pass
    assert [s.name for s in exported.get_finished_spans()] == ["SELECT"]


def test_no_endpoint_means_no_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _provider(monkeypatch)
    # Only the test's own processor: build_provider added none.
    assert len(provider._active_span_processor._span_processors) == 1


def test_disabled_is_read_from_otel_sdk_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "TRUE")
    assert tracing.tracing_disabled()
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    assert not tracing.tracing_disabled()


def test_the_traceparent_round_trips_into_a_link(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _provider(monkeypatch)
    with provider.get_tracer("t").start_as_current_span("submit") as current:
        traceparent = tracing.current_traceparent()
    assert traceparent is not None
    link = tracing.link_to(traceparent)
    assert link is not None
    assert link.context.trace_id == current.get_span_context().trace_id
    assert link.context.span_id == current.get_span_context().span_id


def test_no_span_no_traceparent_and_garbage_no_link() -> None:
    assert tracing.current_traceparent() is None
    assert tracing.link_to(None) is None
    assert tracing.link_to("not-a-traceparent") is None


def test_use_traceparent_parents_the_next_span(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, exported = _provider(monkeypatch)
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("first") as first:
        traceparent = tracing.current_traceparent()
    with tracing.use_traceparent(traceparent):
        with tracer.start_as_current_span("later"):
            pass
    later = [s for s in exported.get_finished_spans() if s.name == "later"][0]
    assert later.context.trace_id == first.get_span_context().trace_id


def test_an_unreachable_collector_raises_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review Focus 1: nothing listens on port 9 (discard); export errors stay in the SDK.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    provider = tracing.build_provider("scadbuddy-api", version="v", revision="r")
    with provider.get_tracer("t").start_as_current_span("request"):
        pass
    provider.force_flush(timeout_millis=2000)
    provider.shutdown()


def test_failure_class_names_the_problem_or_the_class() -> None:
    assert tracing.failure_class(ApiError(409, "x")) == "http-409"
    assert tracing.failure_class(ApiError(422, "x", type_="/problems/bad-param")) == "/problems/bad-param"
    assert tracing.failure_class(ValueError("x")) == "ValueError"


def test_span_records_the_failure_class_and_reraises(spans: InMemorySpanExporter) -> None:
    # `spans` is the tests' global provider (tests/conftest.py, Task 3), which
    # `tracing.span` uses.
    with pytest.raises(ValueError):
        with tracing.span("render.render"):
            raise ValueError("boom")
    (finished,) = spans.get_finished_spans()
    assert finished.attributes is not None
    assert finished.attributes["scadbuddy.failure_class"] == "ValueError"
    assert finished.status.status_code is trace.StatusCode.ERROR
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/test_tracing.py -v`
Expected: FAIL at collection with `ImportError: cannot import name 'tracing'`.

- [ ] **Step 4: Write `core/tracing.py`**

```python
# backend/scadbuddy/core/tracing.py
"""OpenTelemetry tracing for the API and the render worker (spec 2026-10-01 §3, §4, §6).

Only the standard ``OTEL_*`` variables configure it. With no
``OTEL_EXPORTER_OTLP_ENDPOINT`` the provider is installed with no exporter: spans are
created, so context still propagates, and dropped. ``OTEL_SDK_DISABLED=true`` installs
nothing at all."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Final

from opentelemetry import context, propagate, trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import Decision, ParentBased, Sampler, SamplingResult
from opentelemetry.trace import Link, Span, SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import AttributeValue

from scadbuddy.core.problems import ApiError
from scadbuddy.core.trace_scrub import ScrubbingSpanExporter

TRACER_NAME: Final = "scadbuddy"
_PROPAGATOR: Final = TraceContextTextMapPropagator()


class NoParentlessClients(Sampler):
    """The root sampler: a ``CLIENT`` span with no parent is dropped (spec §6). psycopg
    makes one per query, and the background loops (the reconciler's poll, the event
    bus, the pool) have no parent span; each would otherwise be a trace of its own."""

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Mapping[str, AttributeValue] | None = None,
        links: Sequence[Link] | None = None,
        trace_state: trace.TraceState | None = None,
    ) -> SamplingResult:
        if kind is SpanKind.CLIENT:
            return SamplingResult(Decision.DROP)
        return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes)

    def get_description(self) -> str:
        return "NoParentlessClients"


DEFAULT_SAMPLER: Final[Sampler] = ParentBased(root=NoParentlessClients())


def tracing_disabled() -> bool:
    return os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true"


def build_provider(
    service_name: str,
    *,
    version: str,
    revision: str,
    inprocess_worker: bool = False,
    exporter: SpanExporter | None = None,
) -> TracerProvider:
    """The process's provider. ``exporter`` is for tests; otherwise the OTLP/HTTP one,
    and only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. Whatever exports is behind
    `ScrubbingSpanExporter`."""
    attributes: dict[str, AttributeValue] = {
        "service.name": service_name,
        "service.version": version,
        "service.instance.id": socket.gethostname(),
        "scadbuddy.revision": revision,
    }
    if inprocess_worker:
        attributes["scadbuddy.worker.inprocess"] = True
    # Resource.create merges OTEL_RESOURCE_ATTRIBUTES; the attributes given here win.
    resource = Resource.create(attributes)
    sampler = None if os.environ.get("OTEL_TRACES_SAMPLER") else DEFAULT_SAMPLER
    provider = TracerProvider(resource=resource, sampler=sampler)
    if exporter is None and os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter()
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(ScrubbingSpanExporter(exporter)))
    return provider


def _instrument_libraries() -> None:
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    instrumentor = PsycopgInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument(enable_commenter=False)


def configure_tracing(
    service_name: str, *, version: str, revision: str, inprocess_worker: bool = False
) -> None:
    """Once per process, before anything that traces is built. A provider already set
    (the tests' own, or an earlier `create_app` in the same process) is kept."""
    propagate.set_global_textmap(_PROPAGATOR)
    if tracing_disabled():
        return
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(
            build_provider(
                service_name,
                version=version,
                revision=revision,
                inprocess_worker=inprocess_worker,
            )
        )
    _instrument_libraries()


def failure_class(error: BaseException) -> str:
    """What a span says failed (spec §6): never the message, which carries values."""
    if isinstance(error, ApiError):
        return f"http-{error.status}" if error.type == "about:blank" else error.type
    return type(error).__name__


@contextmanager
def span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, AttributeValue] | None = None,
    links: Sequence[Link] = (),
) -> Iterator[Span]:
    """A span of our own: on an exception it adds `scadbuddy.failure_class`; the SDK
    records the exception and sets ERROR, and the scrubbing exporter drops its message."""
    tracer = trace.get_tracer(TRACER_NAME)
    with tracer.start_as_current_span(name, kind=kind, attributes=attributes, links=links) as current:
        try:
            yield current
        except Exception as error:
            current.set_attribute("scadbuddy.failure_class", failure_class(error))
            raise


def current_traceparent() -> str | None:
    """The current span's ``traceparent``, when it is valid and sampled (spec §4)."""
    span_context = trace.get_current_span().get_span_context()
    if not (span_context.is_valid and span_context.trace_flags.sampled):
        return None
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier.get("traceparent")


def _context_from(traceparent: str | None) -> Context | None:
    if not traceparent:
        return None
    extracted = _PROPAGATOR.extract({"traceparent": traceparent})
    if not trace.get_current_span(extracted).get_span_context().is_valid:
        return None
    return extracted


def link_to(traceparent: str | None) -> Link | None:
    extracted = _context_from(traceparent)
    if extracted is None:
        return None
    return Link(trace.get_current_span(extracted).get_span_context())


@contextmanager
def use_traceparent(traceparent: str | None) -> Iterator[None]:
    """Run the block as a child of ``traceparent``, or unchanged without a valid one."""
    extracted = _context_from(traceparent)
    if extracted is None:
        yield
        return
    token = context.attach(extracted)
    try:
        yield
    finally:
        context.detach(token)


__all__ = [
    "DEFAULT_SAMPLER",
    "TRACER_NAME",
    "build_provider",
    "configure_tracing",
    "current_traceparent",
    "failure_class",
    "link_to",
    "span",
    "tracing_disabled",
    "use_traceparent",
]
```

`ScrubbingSpanExporter` comes in Task 2; until then create a stub so imports resolve:

```python
# backend/scadbuddy/core/trace_scrub.py
"""Task 2 replaces this file."""

from opentelemetry.sdk.trace.export import SpanExporter


class ScrubbingSpanExporter(SpanExporter):
    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/test_tracing.py -v`
Expected: PASS (11 tests), except `test_span_records_the_failure_class_and_reraises`,
which needs the `spans` fixture from Task 3: it errors with `fixture 'spans' not found`
until then. Mark it `@pytest.mark.skip(reason="needs Task 3's spans fixture")` now and
remove the mark in Task 3, Step 6. Then `uv run --frozen mypy` and `uv run --frozen ruff check . && uv run --frozen ruff format --check .`: clean. If mypy reports `_active_span_processor` as private in the test, add `# type: ignore[attr-defined]` on that one line only.

- [ ] **Step 6: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock backend/scadbuddy/core/tracing.py \
  backend/scadbuddy/core/trace_scrub.py backend/tests/test_tracing.py
git commit -m "feat(tracing): the backend's tracer provider, sampler and context helpers (#988)"
```

---

### Task 2: `ScrubbingSpanExporter`

**Files:**
- Modify (replace the stub): `backend/scadbuddy/core/trace_scrub.py`
- Test: `backend/tests/test_trace_scrub.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `class ScrubbingSpanExporter(SpanExporter)` with `__init__(self, inner: SpanExporter)`; `def scrub(span: ReadableSpan) -> ReadableSpan`; `def frames_only(stacktrace: str) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_trace_scrub.py
"""The scrub in front of every exporter (spec 2026-10-01 §6): no exception message,
in any form, leaves the process."""

from __future__ import annotations

import traceback

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from scadbuddy.core.problems import ApiError
from scadbuddy.core.trace_scrub import ScrubbingSpanExporter, frames_only
from scadbuddy.render.runner import ParameterValueError

SENTINEL = "s3ntinel-9f1c"


def _exported(raise_it: bool = True, description: str | None = None) -> InMemorySpanExporter:
    inner = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ScrubbingSpanExporter(inner)))
    tracer = provider.get_tracer("t")
    try:
        with tracer.start_as_current_span("work") as current:
            if description is not None:
                current.set_status(Status(StatusCode.ERROR, description))
            if raise_it:
                try:
                    raise ParameterValueError(f"parameter 'name' got {SENTINEL!r}")
                except ParameterValueError as cause:
                    raise ApiError(422, f"bad value {SENTINEL}") from cause
    except ApiError:
        pass
    return inner


def _everything(exported: InMemorySpanExporter) -> str:
    parts: list[str] = []
    for span in exported.get_finished_spans():
        parts.append(repr(dict(span.attributes or {})))
        parts.append(span.status.description or "")
        for event in span.events:
            parts.append(repr(dict(event.attributes or {})))
    return "\n".join(parts)


def test_a_chained_exception_leaves_no_message_anywhere() -> None:
    exported = _exported()
    assert SENTINEL not in _everything(exported)
    (span,) = exported.get_finished_spans()
    (event,) = span.events
    assert event.name == "exception"
    assert event.attributes is not None
    assert event.attributes["exception.type"].endswith("ApiError")
    assert "exception.message" not in event.attributes
    assert 'File "' in str(event.attributes["exception.stacktrace"])


def test_a_status_description_becomes_the_exception_type() -> None:
    exported = _exported()
    (span,) = exported.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is not None
    assert SENTINEL not in span.status.description
    assert span.status.description.endswith("ApiError")


def test_a_status_description_without_an_exception_becomes_error() -> None:
    exported = _exported(raise_it=False, description=f"failed: {SENTINEL}")
    (span,) = exported.get_finished_spans()
    assert span.status.description == "error"


def test_frames_only_keeps_file_lines_and_drops_messages_and_code() -> None:
    try:
        try:
            raise ValueError(SENTINEL)
        except ValueError as cause:
            raise RuntimeError(SENTINEL) from cause
    except RuntimeError as error:
        formatted = "".join(traceback.format_exception(error))
    kept = frames_only(formatted)
    assert SENTINEL not in kept
    assert kept
    assert all(line.startswith('File "') for line in kept.splitlines())


def test_a_clean_span_passes_through_unchanged() -> None:
    exported = _exported(raise_it=False)
    (span,) = exported.get_finished_spans()
    assert span.name == "work"
    assert span.status.status_code is StatusCode.UNSET
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/test_trace_scrub.py -v`
Expected: FAIL with `ImportError: cannot import name 'frames_only'`.

- [ ] **Step 3: Write the exporter**

```python
# backend/scadbuddy/core/trace_scrub.py
"""The scrub every exported span passes through (spec 2026-10-01 §6).

Exception messages carry what must never be recorded: `ParameterValueError` puts the
raw value in its message (`got {value!r}`), `map_response` puts Bambuddy's own
`detail` in an `ApiError`'s. The FastAPI instrumentation and Temporal's
`TracingInterceptor` record exceptions and status descriptions on their own, so the
rule is enforced here, once, rather than at each call site."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status

_FRAME: Final = re.compile(r'^\s*(File ".*", line \d+, in .*)$')


def frames_only(stacktrace: str) -> str:
    """The ``File "…", line N, in f`` lines of a formatted traceback, and nothing else:
    a traceback ends with, and for a chained exception repeats, the messages, and its
    code lines are source text."""
    kept = (match.group(1) for line in stacktrace.splitlines() if (match := _FRAME.match(line)))
    return "\n".join(kept)


def _scrub_event(event: Event) -> Event:
    if event.name != "exception" or not event.attributes:
        return event
    attributes = {
        key: value for key, value in event.attributes.items() if key != "exception.message"
    }
    stacktrace = attributes.get("exception.stacktrace")
    if isinstance(stacktrace, str):
        attributes["exception.stacktrace"] = frames_only(stacktrace)
    return Event(event.name, attributes, event.timestamp)


def _exception_type(events: Sequence[Event]) -> str | None:
    for event in events:
        if event.name == "exception" and event.attributes:
            value = event.attributes.get("exception.type")
            if isinstance(value, str):
                return value
    return None


def scrub(span: ReadableSpan) -> ReadableSpan:
    events = [_scrub_event(event) for event in span.events]
    status = span.status
    if status.description:
        status = Status(status.status_code, _exception_type(events) or "error")
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=span.attributes,
        events=events,
        links=span.links,
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class ScrubbingSpanExporter(SpanExporter):
    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([scrub(span) for span in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


__all__ = ["ScrubbingSpanExporter", "frames_only", "scrub"]
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/test_trace_scrub.py tests/test_tracing.py -v`
Expected: PASS. Then `uv run --frozen mypy`: clean.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/core/trace_scrub.py backend/tests/test_trace_scrub.py
git commit -m "feat(tracing): scrub exception messages and status descriptions before export (#988)"
```

---

### Task 3: The test harness, and tracing wired into the API and the worker

**Files:**
- Modify: `backend/tests/conftest.py` (top-level test provider, `spans` fixture, `wait_for_span`)
- Modify: `backend/scadbuddy/main.py` (`create_app`)
- Modify: `backend/scadbuddy/worker.py` (`main`)
- Test: `backend/tests/api/test_tracing_http.py`

**Interfaces:**
- Consumes: `configure_tracing`, `DEFAULT_SAMPLER` (Task 1); `ScrubbingSpanExporter` (Task 2).
- Produces (for Tasks 4–8): pytest fixture `spans -> InMemorySpanExporter` (cleared before and after each test); `def wait_for_span(spans: InMemorySpanExporter, predicate: Callable[[ReadableSpan], bool], timeout: float = 30) -> ReadableSpan` in `tests/conftest.py`; `EXCLUDED_URLS: Final = "/healthz,/metrics,/telemetry/v1/traces"` in `scadbuddy/main.py`.

- [ ] **Step 1: Install the tests' own provider**

The global provider can be set once per process, so the tests set it at import of
`tests/conftest.py`, before any app is built; `configure_tracing` then keeps it. Add
near the top of `backend/tests/conftest.py`, after the imports:

```python
from collections.abc import Callable

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.core.trace_scrub import ScrubbingSpanExporter
from scadbuddy.core.tracing import DEFAULT_SAMPLER

#: Every span any test makes, after the same scrub production uses (spec §6).
_SPANS = InMemorySpanExporter()
_provider = TracerProvider(sampler=DEFAULT_SAMPLER)
_provider.add_span_processor(SimpleSpanProcessor(ScrubbingSpanExporter(_SPANS)))
trace.set_tracer_provider(_provider)


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    _SPANS.clear()
    yield _SPANS
    _SPANS.clear()


def wait_for_span(
    spans: InMemorySpanExporter, predicate: Callable[[ReadableSpan], bool], timeout: float = 30
) -> ReadableSpan:
    """Spans end on the worker's own tasks after the job settles: poll, bounded."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for finished in spans.get_finished_spans():
            if predicate(finished):
                return finished
        time.sleep(0.05)
    raise AssertionError("no matching span was recorded")
```

(`time`, `Iterator` and `pytest` are already imported there; add whichever is missing.)

- [ ] **Step 2: Write the failing tests**

```python
# backend/tests/api/test_tracing_http.py
"""Requests are traced; the infrastructure paths are not (spec 2026-10-01 §6)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from tests.conftest import wait_for_span


def _server_spans(spans: InMemorySpanExporter) -> list[str]:
    return [
        str((s.attributes or {}).get("http.route"))
        for s in spans.get_finished_spans()
        if s.kind is SpanKind.SERVER
    ]


def test_an_api_request_is_a_server_span_named_by_its_route(
    client: TestClient, spans: InMemorySpanExporter
) -> None:
    assert client.get("/api/v1/models").status_code == 200
    server = wait_for_span(spans, lambda s: s.kind is SpanKind.SERVER)
    assert (server.attributes or {}).get("http.route") == "/api/v1/models"
    assert "?" not in server.name


def test_its_queries_are_children_of_the_request(
    client: TestClient, spans: InMemorySpanExporter
) -> None:
    # A job lookup reads render_jobs (projection.read) before answering 404.
    assert client.get("/api/v1/jobs/no-such-job").status_code == 404
    server = wait_for_span(spans, lambda s: s.kind is SpanKind.SERVER)
    queries = [
        s for s in spans.get_finished_spans()
        if s.kind is SpanKind.CLIENT and s.context.trace_id == server.context.trace_id
    ]
    assert queries, "the lookup's query is a child of the request"
    for query in queries:
        # Statement text only, never the bound values (sqlcommenter off, spec §6).
        assert "no-such-job" not in repr(query.attributes)


@pytest.mark.parametrize("path", ["/healthz", "/metrics"])
def test_the_infrastructure_paths_are_not_traced(
    client: TestClient, spans: InMemorySpanExporter, path: str
) -> None:
    assert client.get(path).status_code == 200
    assert _server_spans(spans) == []


def test_a_garbage_traceparent_starts_a_fresh_trace(
    client: TestClient, spans: InMemorySpanExporter
) -> None:
    response = client.get("/api/v1/models", headers={"traceparent": "00-garbage-xx-01"})
    assert response.status_code == 200
    server = wait_for_span(spans, lambda s: s.kind is SpanKind.SERVER)
    assert server.parent is None

```

The unreachable collector (Review Focus 1) is tested on the provider itself in
`test_tracing.py`: the tests' global provider is already set, so an app built here
would never construct an exporter.

- [ ] **Step 3: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/api/test_tracing_http.py -v`
(needs `SCADBUDDY_TEST_DATABASE_URL` and a Temporal, see CLAUDE.md)
Expected: the server-span tests FAIL with `AssertionError: no matching span was recorded`.

- [ ] **Step 4: Wire tracing into `create_app`**

In `backend/scadbuddy/main.py`, add the imports and constant:

```python
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from scadbuddy.core.tracing import configure_tracing

#: Never traced (spec §6): set in code so no deployment can drop it.
EXCLUDED_URLS: Final = "/healthz,/metrics,/telemetry/v1/traces"
```

(`Final` from `typing`; add it to the existing typing import.) In `create_app`, as the
first two lines after `configure_logging(app_settings.log_level)`:

```python
    configure_tracing(
        "scadbuddy-api",
        version=app_settings.version,
        revision=app_settings.revision,
        inprocess_worker=app_settings.temporal_worker_inprocess,
    )
```

and immediately before `return app`:

```python
    # Outermost, so the server span covers every middleware, the body gate included.
    FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS)
```

- [ ] **Step 5: Wire tracing into the worker**

In `backend/scadbuddy/worker.py` `main()`, after `configure_logging(settings.log_level)`:

```python
    configure_tracing("scadbuddy-worker", version=settings.version, revision=settings.revision)
```

with `from scadbuddy.core.tracing import configure_tracing` at the top. The health
server on 9090 is a separate uvicorn app that is never instrumented, so its `/healthz`
and `/metrics` are untraced without an exclusion.

- [ ] **Step 6: Run the tests**

Remove the `skip` mark Task 1 put on `test_span_records_the_failure_class_and_reraises`.

Run: `cd backend && uv run --frozen pytest tests/api/test_tracing_http.py tests/test_tracing.py -v`
Expected: PASS. Then the whole suite once, `uv run --frozen pytest`, to confirm the
global provider and psycopg instrumentation change nothing else.

- [ ] **Step 7: Commit**

```bash
git add backend/tests/conftest.py backend/tests/api/test_tracing_http.py \
  backend/scadbuddy/main.py backend/scadbuddy/worker.py
git commit -m "feat(tracing): trace API requests and their queries, not /healthz or /metrics (#988)"
```

---

### Task 4: Temporal: one trace from the request to every activity

**Files:**
- Modify: `backend/scadbuddy/workflows/client.py` (`connect`, `render_worker`)
- Test: `backend/tests/api/test_tracing_render.py`

**Interfaces:**
- Consumes: `spans`, `wait_for_span` (Task 3).
- Produces: nothing new; every Temporal client this codebase builds through `connect` carries `TracingInterceptor`, and the worker inherits it.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/api/test_tracing_render.py
"""One render is one trace (spec 2026-10-01 §4)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from tests.api.conftest import wait_for_job
from tests.conftest import wait_for_span


def test_a_render_is_one_trace_from_request_to_activities(
    client: TestClient, model: str, spans: InMemorySpanExporter
) -> None:
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
    assert response.status_code == 202
    assert wait_for_job(client, response.json()["job_id"])["status"] == "done"

    request = wait_for_span(
        spans,
        lambda s: s.kind is SpanKind.SERVER
        and str((s.attributes or {}).get("http.route", "")).endswith("/render"),
    )
    wait_for_span(spans, lambda s: s.name == "RunWorkflow:TemplatePipeline")
    in_trace = [
        s.name for s in spans.get_finished_spans() if s.context.trace_id == request.context.trace_id
    ]
    assert "StartWorkflow:TemplatePipeline" in in_trace
    assert "RunWorkflow:TemplatePipeline" in in_trace
    assert any(name.startswith("RunActivity:") for name in in_trace)
```

- [ ] **Step 2: Run it to see it fail**

Run: `cd backend && uv run --frozen pytest tests/api/test_tracing_render.py -v`
Expected: FAIL with `AssertionError: no matching span was recorded` (no Temporal spans yet).

- [ ] **Step 3: Add the interceptor**

In `backend/scadbuddy/workflows/client.py`:

```python
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions
```

`connect` becomes:

```python
async def connect(address: str, namespace: str, *, lazy: bool = False) -> Client:
    """``lazy`` connects on the first call instead of here (the API, which must boot
    with Temporal down); the worker connects eagerly and fails fast. Every client
    traces (spec 2026-10-01 §4): context rides in workflow headers, and a worker built
    on this client takes the same interceptor."""
    return await Client.connect(
        address,
        namespace=namespace,
        data_converter=pydantic_data_converter,
        lazy=lazy,
        interceptors=[TracingInterceptor()],
    )
```

and `render_worker` passes a runner that lets OpenTelemetry through the sandbox, after
`activities=activities.all(),`:

```python
        # The interceptor's workflow spans run inside the sandbox; OpenTelemetry's
        # module state must be the process's, not a sandboxed copy.
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules("opentelemetry")
        ),
```

- [ ] **Step 4: Run the test**

Run: `cd backend && uv run --frozen pytest tests/api/test_tracing_render.py tests/test_submit.py tests/test_worker.py -v`
Expected: PASS. If `RunWorkflow:TemplatePipeline` is missing while activities are
present, check the worker log for a sandbox restriction naming an `opentelemetry`
module; the passthrough above is what fixes it.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/workflows/client.py backend/tests/api/test_tracing_render.py
git commit -m "feat(tracing): Temporal carries the trace from the request to the worker (#988)"
```

---

### Task 5: Render stage and `openscad` spans

**Files:**
- Modify: `backend/scadbuddy/render/jobs.py` (`timed_stage`, `render_job`'s `stage`)
- Modify: `backend/scadbuddy/render/runner.py` (`run_openscad`)
- Modify: `backend/scadbuddy/render/solids.py` (`solid`)
- Test: `backend/tests/test_tracing_render_stages.py`

**Interfaces:**
- Consumes: `span` (Task 1); `spans` (Task 3).
- Produces: span names `render.source`, `render.render`, `render.split`, `render.solids`, `render.thumbnail`, `render.write`, `render.solid` (attribute `scadbuddy.colour_index: int`), `openscad.export` (attributes `scadbuddy.openscad.format: str`, `scadbuddy.openscad.backend: str`, `scadbuddy.openscad.exit_code: int`).

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_tracing_render_stages.py
"""Render stages and openscad calls are spans (spec 2026-10-01 §5.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.core.config import Config
from scadbuddy.core.metrics import Metrics
from scadbuddy.render.jobs import timed_stage
from scadbuddy.render.runner import OpenSCADError, run_openscad


def test_a_timed_stage_is_a_span_and_still_timed(spans: InMemorySpanExporter) -> None:
    metrics = Metrics()
    with timed_stage(metrics)("render"):
        pass
    with timed_stage(None)("thumbnail"):
        pass
    assert [s.name for s in spans.get_finished_spans()] == ["render.render", "render.thumbnail"]
    timed = metrics.registry.get_sample_value(
        "scadbuddy_render_stage_seconds_count", {"stage": "render"}
    )
    assert timed == 1


def test_a_failed_stage_is_an_error_with_its_class(spans: InMemorySpanExporter) -> None:
    with pytest.raises(OpenSCADError):
        with timed_stage(None)("render"):
            raise OpenSCADError("openscad exited with 1", ["SECRET LINE"], 1, [], 0)
    (failed,) = spans.get_finished_spans()
    assert (failed.attributes or {})["scadbuddy.failure_class"] == "OpenSCADError"


async def test_an_openscad_call_is_an_export_span(
    spans: InMemorySpanExporter, fake_openscad: str, tmp_path: Path, config: Config
) -> None:
    scad = tmp_path / "m.scad"
    scad.write_text("cube(1);")
    configured = config.model_copy(update={"openscad": fake_openscad})
    await run_openscad(
        ["--backend=Manifold", "-o", str(tmp_path / "out.3mf"), scad.name],
        cwd=tmp_path,
        config=configured,
    )
    (export,) = [s for s in spans.get_finished_spans() if s.name == "openscad.export"]
    attributes = export.attributes or {}
    assert attributes["scadbuddy.openscad.format"] == "3mf"
    assert attributes["scadbuddy.openscad.backend"] == "Manifold"
    assert attributes["scadbuddy.openscad.exit_code"] == 0
    assert "cube" not in repr(attributes)
```

Before writing these, check the fixture names: `grep -n "def fake_openscad\|def config" backend/tests/conftest.py`.
If the config fixture is named differently (for example it is built from `load_config`),
use that name and keep the test body unchanged. If `Config` is a frozen dataclass
rather than a pydantic model, replace `config.model_copy(update=…)` with
`dataclasses.replace(config, openscad=fake_openscad)`.

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/test_tracing_render_stages.py -v`
Expected: FAIL: `assert [] == ['render.render', 'render.thumbnail']`.

- [ ] **Step 3: Trace the stages**

In `backend/scadbuddy/render/jobs.py`, add `from contextlib import contextmanager` (if
absent) and `from scadbuddy.core.tracing import span`, then replace `timed_stage`:

```python
@contextmanager
def _traced_stage(name: RenderStage, timed: AbstractContextManager[None]) -> Iterator[None]:
    """One render stage: a span (spec 2026-10-01 §5.1) around the existing timing."""
    with span(f"render.{name}"), timed:
        yield


def timed_stage(metrics: Metrics | None) -> Callable[[RenderStage], AbstractContextManager[None]]:
    """A stage timed into `stage_duration`, as `render_job`'s are, and traced; untimed
    without metrics."""

    def stage(name: RenderStage) -> AbstractContextManager[None]:
        return _traced_stage(name, metrics.stage(name) if metrics is not None else nullcontext())

    return stage
```

and in `render_job` replace the inner `stage`'s return:

```python
    def stage(name: RenderStage) -> AbstractContextManager[None]:
        if on_stage is not None:
            on_stage(name)
        return _traced_stage(name, metrics.stage(name) if metrics is not None else nullcontext())
```

(`Iterator` from `collections.abc`; add it to the existing import.)

- [ ] **Step 4: Trace `openscad`**

In `backend/scadbuddy/render/runner.py` rename the existing `run_openscad` to
`_run_openscad` (body unchanged) and add, with `from scadbuddy.core.tracing import span`:

```python
def _export_attributes(args: Sequence[str]) -> dict[str, str]:
    """What the call renders, never its defines: those carry parameter values."""
    attributes: dict[str, str] = {}
    if "-o" in args:
        index = args.index("-o")
        if index + 1 < len(args):
            attributes["scadbuddy.openscad.format"] = Path(args[index + 1]).suffix.lstrip(".")
    for arg in args:
        if arg.startswith("--backend="):
            attributes["scadbuddy.openscad.backend"] = arg.removeprefix("--backend=")
    return attributes


async def run_openscad(args: Sequence[str], *, cwd: Path, config: Config) -> ProcessOutput:
    with span("openscad.export", attributes=_export_attributes(args)) as current:
        try:
            output = await _run_openscad(args, cwd=cwd, config=config)
        except OpenSCADError as error:
            if error.returncode is not None:
                current.set_attribute("scadbuddy.openscad.exit_code", error.returncode)
            raise
        current.set_attribute("scadbuddy.openscad.exit_code", output.returncode)
        return output
```

- [ ] **Step 5: Name the colour on the solids pass**

In `backend/scadbuddy/render/solids.py` `solid()`, wrap the `render_3mf(...)` call in
the existing `try:` (and only that call):

```python
                with span("render.solid", attributes={"scadbuddy.colour_index": index}):
                    await render_3mf(
                        wrapper,
                        schema,
                        params,
                        out_path,
                        config=config,
                        extra_defines=[*extra_defines, "-D", f"_sb_targets={targets}"],
                    )
```

with `from scadbuddy.core.tracing import span`.

- [ ] **Step 6: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/test_tracing_render_stages.py tests/test_runner.py tests/test_solids.py tests/test_jobs.py -v`
Expected: PASS. `uv run --frozen mypy` clean.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/render/jobs.py backend/scadbuddy/render/runner.py \
  backend/scadbuddy/render/solids.py backend/tests/test_tracing_render_stages.py
git commit -m "feat(tracing): render stages, each colour and each openscad call are spans (#988)"
```

---

### Task 6: `render_jobs.traceparent`, the coalesce link and the reconciler

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC stamp>_render_jobs_traceparent.sql`
- Modify: `backend/scadbuddy/render/job_models.py` (`Job`)
- Modify: `backend/scadbuddy/render/projection.py` (`PROJECTION_COLUMNS`, `submit`)
- Modify: `backend/scadbuddy/render/submit.py` (`submit`, `reconcile_once`)
- Test: `backend/tests/test_submit.py` (new tests), `backend/tests/test_projection.py` (new test)

**Interfaces:**
- Consumes: `span`, `current_traceparent`, `link_to`, `use_traceparent` (Task 1); `spans` (Task 3).
- Produces: `Job.traceparent: str | None`; span `render.submit` with attributes `scadbuddy.slug`, `scadbuddy.job_id`, `scadbuddy.coalesced: bool`, and a link to the first caller's span when coalesced.

- [ ] **Step 1: The migration**

```bash
cd backend/scadbuddy/migrations
cat > "$(date -u +%Y%m%dT%H%MZ)_render_jobs_traceparent.sql" <<'SQL'
-- #988: the first caller's trace context, so a request that coalesces onto this row
-- can link to the render it joined, and the reconciler can start a late workflow in
-- the trace it belongs to. NULL when the row predates tracing, the sampler dropped
-- the request, or OTEL_SDK_DISABLED was set.
ALTER TABLE render_jobs ADD COLUMN IF NOT EXISTS traceparent text;
SQL
```

- [ ] **Step 2: Write the failing tests**

Append to `backend/tests/test_projection.py` (it already has a `projection`-style
fixture; reuse whichever that file defines, by name, and a `Job` builder if it has one):

```python
def test_a_coalesced_submit_returns_the_first_callers_traceparent(
    projection: JobProjection,
) -> None:
    first = Job(id="a1", slug="demo", params={"w": 1}, created_at=now(),
                traceparent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
    second = Job(id="b2", slug="demo", params={"w": 1}, created_at=now(), traceparent=None)
    projection.submit(first, "key-1")
    joined = projection.submit(second, "key-1")
    assert joined.coalesced
    assert joined.job.traceparent == first.traceparent
```

Append to `backend/tests/test_submit.py`:

```python
async def test_a_coalesced_submit_links_to_the_render_it_joined(
    make_service: ServiceFactory, spans: InMemorySpanExporter
) -> None:
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}", reconcile_after=60.0)
        # No worker: the first job stays pending, so the second coalesces onto it.
        first = await service.submit(SLUG, {"width": 7})
        second = await service.submit(SLUG, {"width": 7})
        await service.aclose()
    assert second.id == first.id
    submits = [s for s in spans.get_finished_spans() if s.name == "render.submit"]
    assert len(submits) == 2
    opened, joined = submits
    assert (joined.attributes or {})["scadbuddy.coalesced"] is True
    assert [link.context.span_id for link in joined.links] == [opened.context.span_id]


async def test_a_row_without_a_traceparent_coalesces_without_a_link(
    make_service: ServiceFactory, projection: JobProjection, spans: InMemorySpanExporter
) -> None:
    # A pending row written before the migration (Review Focus 3).
    old = Job(id=uuid.uuid4().hex, slug=SLUG, params={"width": 8},
              inputs={"params": {"width": 8}}, created_at=now(), traceparent=None)
    await asyncio.to_thread(projection.submit, old, render_key(SLUG, {"width": 8}, None))
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}", reconcile_after=60.0)
        joined = await service.submit(SLUG, {"width": 8})
        await service.aclose()
    assert joined.id == old.id
    (submit,) = [s for s in spans.get_finished_spans() if s.name == "render.submit"]
    assert list(submit.links) == []


async def test_the_reconciler_starts_a_row_in_its_first_callers_trace(
    make_service: ServiceFactory, projection: JobProjection, spans: InMemorySpanExporter
) -> None:
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    stale = Job(id=uuid.uuid4().hex, slug=SLUG, params={"width": 9},
                inputs={"params": {"width": 9}}, created_at=now(), traceparent=traceparent)
    await asyncio.to_thread(projection.submit, stale, render_key(SLUG, {"width": 9}, None))
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}", reconcile_after=0.0)
        started: list[str | None] = []
        start = client.start_workflow

        async def capturing(*args: Any, **kwargs: Any) -> Any:
            started.append(current_traceparent())
            return await start(*args, **kwargs)

        client.start_workflow = capturing  # type: ignore[method-assign]
        assert await service.reconcile_once() == 1
        await service.aclose()
    assert started and started[0] is not None
    assert started[0].split("-")[1] == "4bf92f3577b34da6a3ce929d0e0e4736"


async def test_with_the_sdk_disabled_rows_carry_no_traceparent(
    make_service: ServiceFactory, projection: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review Focus 4: no valid span, so nothing to persist.
    monkeypatch.setattr("scadbuddy.render.submit.current_traceparent", lambda: None)
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}", reconcile_after=60.0)
        job = await service.submit(SLUG, {"width": 11})
        await service.aclose()
    assert (await asyncio.to_thread(projection.read, job.id)).traceparent is None
```

with these imports added to `test_submit.py`:
`from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter`
and `from scadbuddy.core.tracing import current_traceparent`.

Note on the first test: both submits happen inside the test, outside any request, so
each `render.submit` is a root of its own trace; the link is the assertion that matters.

- [ ] **Step 3: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/test_submit.py tests/test_projection.py -k "traceparent or link or reconciler_starts" -v`
Expected: FAIL with `ValidationError ... traceparent ... Extra inputs` or `TypeError: unexpected keyword argument 'traceparent'`.

- [ ] **Step 4: Carry the column**

`backend/scadbuddy/render/job_models.py`, in `class Job`, after `kind`:

```python
    #: The first caller's ``traceparent`` (spec 2026-10-01 §4): what a coalesced
    #: request links to and the reconciler starts a late workflow under. None before
    #: tracing, when the sampler dropped the request, or with the SDK disabled.
    traceparent: str | None = None
```

`backend/scadbuddy/render/projection.py`: add `"traceparent",` to `PROJECTION_COLUMNS`
after `"workflow_id",`, and in `submit` add the column to the INSERT:

```python
                "INSERT INTO render_jobs (id, slug, params, inputs, model_version, state,"
                " created_at, render_key, workflow_id, kind, traceparent)"
                " VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s)"
```

with `job.traceparent,` appended to the parameter tuple after `job.kind,`.

- [ ] **Step 5: The submit span and the link**

`backend/scadbuddy/render/submit.py`: import
`from scadbuddy.core.tracing import current_traceparent, link_to, span, use_traceparent`.
Rename the existing `submit` body to `_submit` (same signature) and make `submit`:

```python
    async def submit(
        self,
        slug: str,
        params: Mapping[str, ParamValue],
        *,
        model_version: str | None = None,
        supersedes: str | None = None,
    ) -> Job:
        """Record the job (or join the waiting one it matches) and start its workflow."""
        with span("render.submit", attributes={"scadbuddy.slug": slug}) as current:
            job, coalesced = await self._submit(
                slug, params, model_version=model_version, supersedes=supersedes
            )
            current.set_attribute("scadbuddy.job_id", job.id)
            current.set_attribute("scadbuddy.coalesced", coalesced)
            if coalesced and (link := link_to(job.traceparent)) is not None:
                current.add_link(link.context)
            return job
```

In `_submit`: build the `Job(...)` with `traceparent=current_traceparent(),` added; change
its return type to `tuple[Job, bool]`; make the coalesced early return
`return submitted.job, True` and the final return `return submitted.job, False`.

In `reconcile_once`, wrap the one `_start` call:

```python
            try:
                with use_traceparent(job.traceparent):
                    await self._start(job, WorkflowIDConflictPolicy.FAIL)
```

- [ ] **Step 6: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/test_submit.py tests/test_projection.py tests/test_pg_migrations.py tests/api/test_jobs.py -v`
Expected: PASS. `uv run --frozen mypy` clean.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/migrations/*_render_jobs_traceparent.sql \
  backend/scadbuddy/render/job_models.py backend/scadbuddy/render/projection.py \
  backend/scadbuddy/render/submit.py backend/tests/test_submit.py backend/tests/test_projection.py
git commit -m "feat(tracing): coalesced renders link to the one they joined (#988)"
```

---

### Task 7: Bambuddy calls are client spans that inject nothing

**Files:**
- Modify: `backend/scadbuddy/bambuddy/client.py` (`_send`, the streaming download at ~line 462, `download_library_file`)
- Test: `backend/tests/bambuddy/test_tracing.py`

**Interfaces:**
- Consumes: `span` (Task 1); `spans` (Task 3).
- Produces: span `bambuddy.<METHOD>` (kind CLIENT) with attributes `http.request.method`, `http.response.status_code`, `scadbuddy.bambuddy.scope`, and on failure `scadbuddy.failure_class`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/bambuddy/test_tracing.py
"""Bambuddy calls are traced on our side only (spec 2026-10-01 §4)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL

SENTINEL = "detail-s3ntinel"


@respx.mock
async def test_a_call_is_a_client_span_and_sends_no_trace_headers(
    bambuddy: Any, spans: InMemorySpanExporter
) -> None:
    route = respx.get(f"{BASE_URL}/api/v1/printers/").mock(return_value=httpx.Response(200, json=[]))
    with trace.get_tracer("t").start_as_current_span("request"):
        await bambuddy.printers()
    sent = route.calls.last.request
    assert "traceparent" not in sent.headers
    assert "baggage" not in sent.headers
    (call,) = [s for s in spans.get_finished_spans() if s.name == "bambuddy.GET"]
    assert call.kind is SpanKind.CLIENT
    attributes = call.attributes or {}
    assert attributes["http.response.status_code"] == 200
    assert attributes["scadbuddy.bambuddy.scope"] == "Read Status"
    assert "s3cret" not in repr(attributes)


@respx.mock
async def test_a_refusal_records_its_class_and_never_bambuddys_detail(
    bambuddy: Any, spans: InMemorySpanExporter
) -> None:
    respx.get(f"{BASE_URL}/api/v1/printers/").mock(
        return_value=httpx.Response(409, json={"detail": SENTINEL})
    )
    with trace.get_tracer("t").start_as_current_span("request"):
        with pytest.raises(ApiError):
            await bambuddy.printers()
    everything = repr([(s.attributes, s.status.description, [e.attributes for e in s.events])
                       for s in spans.get_finished_spans()])
    assert SENTINEL not in everything
    (call,) = [s for s in spans.get_finished_spans() if s.name == "bambuddy.GET"]
    assert (call.attributes or {})["http.response.status_code"] == 409
    assert str((call.attributes or {})["scadbuddy.failure_class"]).startswith(("http-", "/"))
```

Check the printers path first: `grep -n '"/printers/"' backend/scadbuddy/bambuddy/client.py`
and `grep -n "API_PREFIX =" backend/scadbuddy/bambuddy/client.py`; the URL the test mocks
is `BASE_URL + API_PREFIX + "/printers/"`. Adjust the literal if `API_PREFIX` differs.

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_tracing.py -v`
Expected: FAIL: `ValueError: not enough values to unpack (expected 1, got 0)`.

- [ ] **Step 3: Span the three call paths**

In `backend/scadbuddy/bambuddy/client.py` add
`from opentelemetry.trace import SpanKind` and `from scadbuddy.core.tracing import span`,
and a helper on the module:

```python
def _call_span(method: str, scope: Scope) -> AbstractContextManager[Span]:
    """Our side of a Bambuddy call (spec 2026-10-01 §4): no headers are injected; the
    span holds the method, the status code and the scope, never a path or a body."""
    return span(
        f"bambuddy.{method}",
        kind=SpanKind.CLIENT,
        attributes={"http.request.method": method, "scadbuddy.bambuddy.scope": str(scope)},
    )
```

(`AbstractContextManager` from `contextlib`, `Span` from `opentelemetry.trace`.)
`_send` becomes:

```python
        with _call_span(method, scope) as current:
            try:
                response = await self._http.request(
                    method,
                    self.config.url(path),
                    headers=self._headers,
                    params=dict(params) if params else None,
                    json=json,
                    files=files,
                    timeout=timeout or self.config.timeout,
                )
            except httpx.HTTPError as error:
                logger.warning("bambuddy request failed", extra={"method": method, "path": path})
                raise map_transport(error, what=what) from error
            current.set_attribute("http.response.status_code", response.status_code)
            if response.is_success:
                return response
            raise map_response(response, scope=scope, what=what)
```

The streaming GET (~line 462): wrap from `try: response = await self._http.send(...)`
through the `yield response` / `finally: await response.aclose()` in
`with _call_span("GET", Scope.READ_STATUS) as current:` and set
`current.set_attribute("http.response.status_code", response.status_code)` right after
`send` returns. `download_library_file`: wrap its `async with self._http.stream(...)`
block in `with _call_span("GET", Scope.MANAGE_LIBRARY) as current:` and set the status
code attribute first thing inside the `async with`. Do not instrument
`store/bambuddy.py`'s client: it is not a ScadBuddy service either, and spans there are
not in this spec.

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/bambuddy -v`
Expected: PASS, the existing Bambuddy tests included.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/bambuddy/client.py backend/tests/bambuddy/test_tracing.py
git commit -m "feat(tracing): Bambuddy calls are client spans that inject no trace headers (#988)"
```

---

### Task 8: Redaction, end to end

**Files:**
- Test: `backend/tests/api/test_tracing_redaction.py`

**Interfaces:**
- Consumes: everything above; `set_fake_env`, `wait_for_job` (`tests/api/conftest.py`); FAIL_WIDTH (`width: 999` fails the fake openscad).

- [ ] **Step 1: Write the tests**

```python
# backend/tests/api/test_tracing_redaction.py
"""Nothing forbidden reaches an exported span, on success or failure (spec 2026-10-01 §6, §8)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.core.config import Settings
from tests.api.conftest import set_fake_env, wait_for_job
from tests.conftest import wait_for_span

SENTINEL = "zz-s3ntinel-zz"


def _everything(spans: InMemorySpanExporter) -> str:
    parts: list[str] = []
    for finished in spans.get_finished_spans():
        parts.append(repr(dict(finished.attributes or {})))
        parts.append(finished.status.description or "")
        parts.extend(repr(dict(event.attributes or {})) for event in finished.events)
        parts.extend(repr(dict(link.attributes or {})) for link in finished.links)
    return "\n".join(parts)


def test_a_parameter_value_never_appears(
    client: TestClient, model: str, spans: InMemorySpanExporter
) -> None:
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"label": SENTINEL}})
    if response.status_code == 202:
        wait_for_job(client, response.json()["job_id"])
        wait_for_span(spans, lambda s: s.name == "RunWorkflow:TemplatePipeline")
    assert SENTINEL not in _everything(spans)


def test_an_invalid_parameter_value_never_appears(
    client: TestClient, model: str, spans: InMemorySpanExporter
) -> None:
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": SENTINEL}})
    assert response.status_code == 422
    assert SENTINEL not in _everything(spans)


def test_a_failed_renders_log_never_appears(
    client: TestClient, model: str, settings: Settings, spans: InMemorySpanExporter
) -> None:
    set_fake_env(Path(settings.openscad).parent, "FAKE_STDERR", [f"ERROR: {SENTINEL}"])
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 999}})
    assert wait_for_job(client, response.json()["job_id"])["status"] == "failed"
    wait_for_span(spans, lambda s: s.name == "RunWorkflow:TemplatePipeline")
    assert SENTINEL not in _everything(spans)
    failed = [s for s in spans.get_finished_spans() if s.name == "openscad.export"]
    assert failed and (failed[0].attributes or {}).get("scadbuddy.failure_class") == "OpenSCADError"


def test_an_unhandled_exception_message_never_appears(
    app: FastAPI, spans: InMemorySpanExporter
) -> None:
    async def explode() -> None:
        raise RuntimeError(SENTINEL)

    app.router.add_api_route("/api/v1/_explode", explode)
    # Ahead of the SPA fallback, if a frontend bundle is mounted.
    app.router.routes.insert(0, app.router.routes.pop())
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/v1/_explode").status_code == 500
    assert SENTINEL not in _everything(spans)


def test_the_bambuddy_api_key_never_appears(
    settings: Settings, spans: InMemorySpanExporter
) -> None:
    keyed = settings.model_copy(update={"bambuddy_api_key": SENTINEL})
    from scadbuddy.main import create_app

    with TestClient(create_app(keyed)) as client:
        client.get("/api/v1/bambuddy/status")
    assert SENTINEL not in _everything(spans)
```

Before writing these, confirm three names: the model fixture's text parameter
(`grep -n "label\|string" backend/tests/conftest.py` around the `model` fixture; use any
string parameter it declares, or add `label = "x";` to that fixture's source if it has
none), `Settings.bambuddy_api_key` (`grep -n bambuddy_api_key backend/scadbuddy/core/settings.py`),
and a Bambuddy status route (`grep -rn '"/status"\|bambuddy' backend/scadbuddy/api/*.py | head`).
The Bambuddy 409 `detail` case is already covered in Task 7.

- [ ] **Step 2: Run them**

Run: `cd backend && uv run --frozen pytest tests/api/test_tracing_redaction.py -v`
Expected: PASS. A failure here is a real leak: find which span carries the sentinel
(print `_everything(spans)`), and fix it at its source or in `trace_scrub.py`; never
weaken the assertion.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/api/test_tracing_redaction.py
git commit -m "test(tracing): no parameter value, log line, key or message reaches a span (#988)"
```

---

### Task 9: Documentation

**Files:**
- Modify: `README.md` ("Deploying")
- Modify: `CLAUDE.md` ("Layout", backend bullets)

- [ ] **Step 1: README**

Add a subsection at the end of "Deploying":

```markdown
### Tracing (#988)

The API and the render worker export OpenTelemetry traces over OTLP/HTTP when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set (in the cluster, the `alloy-receiver`; see
eh-homelab/clusters#1596). Without it nothing is exported. Only standard `OTEL_*`
variables apply: `OTEL_RESOURCE_ATTRIBUTES` (add `deployment.environment`),
`OTEL_TRACES_SAMPLER` (replaces the default, which keeps every trace but drops
parentless database spans), and `OTEL_SDK_DISABLED=true`, the kill switch for an SDK
problem. Design: `docs/superpowers/specs/2026-10-01-distributed-tracing-design.md`.
```

- [ ] **Step 2: CLAUDE.md**

Under "Layout", after the `backend/scadbuddy/api/` bullet, add:

```markdown
- `backend/scadbuddy/core/tracing.py` — OpenTelemetry (#988): the provider from the
  standard `OTEL_*` variables, the sampler (parentless `CLIENT` spans dropped), and the
  helpers every traced file uses (`span`, `current_traceparent`, `link_to`,
  `use_traceparent`). `core/trace_scrub.py` strips exception messages and status
  descriptions before anything is exported; never record a parameter value, a log
  line or anything Bambuddy returned. Tests share one provider (`tests/conftest.py`,
  fixture `spans`); the Bambuddy client injects no trace headers.
```

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: tracing configuration and where it lives (#988)"
```

---

## After the last task

Run the full CI set from `backend/`:

```bash
uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest
```

Then open the PR `feat(tracing): backend tracing core (#988)` with `Refs #988` (the epic
stays open for rows 2–5).
