import pytest
from starlette.datastructures import Headers

from scadbuddy.core.authorship import InvalidAuthorError, author_from


def test_tool_header_is_read() -> None:
    got = author_from(
        Headers({"x-scadbuddy-agent-author": "token:t1", "x-scadbuddy-agent-tool": "render_model"})
    )
    assert got is not None and got.tool == "render_model"


def test_flow_run_header_is_read_and_checked() -> None:
    """The run a call is made from (#4252), a run id's shape or refused."""
    got = author_from(
        Headers({"x-scadbuddy-agent-author": "token:t1", "x-scadbuddy-flow-run": "run-1"})
    )
    assert got is not None and got.flow_run == "run-1"
    with pytest.raises(InvalidAuthorError):
        author_from(
            Headers({"x-scadbuddy-agent-author": "token:t1", "x-scadbuddy-flow-run": "a/b"})
        )


def test_malformed_tool_header_is_refused() -> None:
    with pytest.raises(InvalidAuthorError):
        author_from(
            Headers({"x-scadbuddy-agent-author": "token:t1", "x-scadbuddy-agent-tool": "a b"})
        )
