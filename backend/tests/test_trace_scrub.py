"""The scrub in front of every exporter (spec 2026-10-01 §6): no exception message,
in any form, leaves the process."""

from __future__ import annotations

import linecache
import os
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, Status, StatusCode

from scadbuddy.core import trace_scrub
from scadbuddy.core.problems import ApiError
from scadbuddy.core.trace_scrub import ScrubbingSpanExporter, frames_only
from scadbuddy.render.runner import ParameterValueError

SENTINEL = "s3ntinel-9f1c"
CODE_ROOTS = trace_scrub._CODE_ROOTS


@pytest.fixture(autouse=True)
def _tests_are_code(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """These tests raise in this file, which in production is no code root."""
    tests = os.path.join(os.path.realpath(os.path.dirname(__file__)), "")
    monkeypatch.setattr(trace_scrub, "_CODE_ROOTS", (*CODE_ROOTS, tests))
    yield


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
                    raise ParameterValueError("name", f"got {SENTINEL!r}")
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
    exc_type = cast(str, event.attributes["exception.type"])
    assert exc_type.endswith("ApiError")
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


def _exported_attributes(attributes: dict[str, str]) -> dict[str, object]:
    inner = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ScrubbingSpanExporter(inner)))
    with provider.get_tracer("t").start_as_current_span("work", attributes=attributes):
        pass
    (span,) = inner.get_finished_spans()
    return dict(span.attributes or {})


def test_no_query_string_or_user_agent_survives() -> None:
    kept = _exported_attributes(
        {
            "http.url": f"http://h/api/v1/x?q={SENTINEL}",
            "url.full": f"http://h/api/v1/x?q={SENTINEL}",
            "http.target": f"/api/v1/x?q={SENTINEL}",
            "url.query": f"q={SENTINEL}",
            "http.user_agent": SENTINEL,
            "user_agent.original": SENTINEL,
            "http.route": "/api/v1/x",
        }
    )
    assert SENTINEL not in repr(kept)
    assert kept["http.url"] == "http://h/api/v1/x"
    assert kept["url.full"] == "http://h/api/v1/x"
    assert kept["http.target"] == "/api/v1/x"
    assert kept["http.route"] == "/api/v1/x"


def test_a_frame_line_with_anything_but_a_function_name_does_not_survive() -> None:
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{__file__}", line 3, in _everything\n'
        f'  File "{__file__}", line 1, in {SENTINEL}-detail with spaces\n'
        "ValueError: boom"
    )
    kept = frames_only(text)
    assert SENTINEL not in kept
    assert f'File "{__file__}", line 3, in _everything' in kept


def test_a_message_shaped_like_frames_does_not_survive() -> None:
    # A Bambuddy detail keeps its newlines: whatever follows the exception line is
    # message text, however much it looks like a traceback.
    detail = (
        f'\n  File "/tok {SENTINEL} here", line 1, in g'
        f'\n  File "{__file__}", line 1, in {SENTINEL}_abc'
        "\nTraceback (most recent call last):"
        f'\n  File "/{SENTINEL}", line 1, in g'
    )
    try:
        raise RuntimeError(detail)
    except RuntimeError as error:
        formatted = "".join(traceback.format_exception(error))
    kept = frames_only(formatted)
    assert SENTINEL not in kept
    assert f'File "{__file__}"' in kept


def test_frames_inside_nested_exception_groups_are_kept() -> None:
    def inner() -> None:
        raise ValueError(SENTINEL)

    try:
        try:
            inner()
        except ValueError as cause:
            raise ExceptionGroup("outer", [ExceptionGroup("inner", [cause])]) from None
    except ExceptionGroup as group:
        formatted = "".join(traceback.format_exception(group))
    kept = frames_only(formatted)
    assert SENTINEL not in kept
    assert "in inner" in kept


def test_a_message_cannot_open_a_traceback_of_its_own() -> None:
    # Review of #1064: a fake header inside a message, then a frame on a real file
    # whose "function name" is message text.
    text = (
        "ApiError: something failed\n"
        "Traceback (most recent call last):\n"
        '  File "/usr/lib/python3.12/os.py", line 1, in LEAKEDTOKEN\n'
    )
    assert "LEAKEDTOKEN" not in frames_only(text)


def test_a_real_frame_from_a_real_traceback_is_kept() -> None:
    def raises_here() -> None:
        raise ValueError(SENTINEL)

    try:
        raises_here()
    except ValueError as error:
        formatted = "".join(traceback.format_exception(error))
    kept = frames_only(formatted).splitlines()
    assert any(line.endswith("in raises_here") for line in kept)
    assert any(line.endswith("in test_a_real_frame_from_a_real_traceback_is_kept") for line in kept)


def test_a_pseudo_file_frame_keeps_only_its_path_and_line() -> None:
    text = (
        "Traceback (most recent call last):\n"
        f'  File "<frozen importlib._bootstrap>", line 7, in {SENTINEL}\n'
        "ValueError: boom"
    )
    assert frames_only(text) == 'File "<frozen importlib._bootstrap>", line 7'


def test_a_dotted_name_is_checked_segment_by_segment() -> None:
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{__file__}", line 1, in _exported\n'
        f'  File "{__file__}", line 1, in _exported.{SENTINEL}\n'
        f'  File "{__file__}", line 1, in LEAKEDTOKEN._exported\n'
        "ValueError: boom"
    )
    kept = frames_only(text)
    assert SENTINEL not in kept
    assert "LEAKEDTOKEN" not in kept
    assert kept == f'File "{__file__}", line 1, in _exported'


def test_a_frozen_module_must_be_one() -> None:
    text = (
        "Traceback (most recent call last):\n"
        '  File "<frozen LEAKEDTOKEN>", line 7, in f\n'
        f'  File "{__file__}", line 999999, in _exported\n'
        "ValueError: boom"
    )
    assert frames_only(text) == ""


def test_no_captured_header_survives() -> None:
    kept = _exported_attributes(
        {
            "http.request.header.cookie": SENTINEL,
            "http.request.header.authorization": SENTINEL,
            "http.response.header.set_cookie": SENTINEL,
            "http.route": "/api/v1/x",
        }
    )
    assert kept == {"http.route": "/api/v1/x"}


def test_no_event_or_link_carries_what_a_span_may_not() -> None:
    # Review 3 of #1064: the rule covers every attribute that leaves the process, not
    # only the span's own and its exception events'.
    leaky = {
        "http.url": f"http://h/api/v1/x?q={SENTINEL}",
        "url.query": f"q={SENTINEL}",
        "http.user_agent": SENTINEL,
        "user_agent.original": SENTINEL,
        "http.request.header.cookie": SENTINEL,
        "http.response.header.set_cookie": SENTINEL,
        "scadbuddy.attempt": 2,
    }
    inner = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ScrubbingSpanExporter(inner)))
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("earlier") as earlier:
        pass
    link = Link(earlier.get_span_context(), attributes=leaky)
    with tracer.start_as_current_span("work", links=[link]) as current:
        current.add_event("retry", attributes=leaky)
    (span,) = [s for s in inner.get_finished_spans() if s.name == "work"]
    (event,) = span.events
    (exported_link,) = span.links
    for attributes in (dict(event.attributes or {}), dict(exported_link.attributes or {})):
        assert SENTINEL not in repr(attributes)
        assert attributes["scadbuddy.attempt"] == 2
        for key in leaky.keys() - {"http.url", "scadbuddy.attempt"}:
            assert key not in attributes
    assert event.name == "retry"
    assert exported_link.context == earlier.get_span_context()


def test_a_real_file_outside_the_code_roots_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review 4 of #1064: a message's fake traceback naming a data file must not make
    # the exporter read it, or keep it in linecache.
    monkeypatch.setattr(trace_scrub, "_CODE_ROOTS", CODE_ROOTS)
    data = tmp_path / "model.3mf"
    data.write_text("def leaked():\n    pass\n")
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{data}", line 1, in <module>\n'
        f'  File "{data}", line 1, in leaked\n'
        "ValueError: boom"
    )
    assert frames_only(text) == ""
    assert str(data) not in linecache.cache


def test_stdlib_and_scadbuddy_frames_are_kept_without_filling_linecache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trace_scrub, "_CODE_ROOTS", CODE_ROOTS)
    stdlib = os.path.realpath(os.__file__)
    ours = os.path.realpath(trace_scrub.__file__)
    linecache.cache.pop(stdlib, None)
    linecache.cache.pop(ours, None)
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{stdlib}", line 1, in makedirs\n'
        f'  File "{ours}", line 1, in frames_only\n'
        "ValueError: boom"
    )
    assert frames_only(text).splitlines() == [
        f'File "{stdlib}", line 1, in makedirs',
        f'File "{ours}", line 1, in frames_only',
    ]
    assert stdlib not in linecache.cache
    assert ours not in linecache.cache
