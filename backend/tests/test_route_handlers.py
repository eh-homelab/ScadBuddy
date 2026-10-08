"""Route handlers called directly, where what matters is how they run, not the app."""

from __future__ import annotations

import threading
from typing import Any, cast

import pytest
from pydantic import ValidationError

from scadbuddy.api import jobs as jobs_api
from scadbuddy.api.presets import invalid_copy_detail
from scadbuddy.core.problems import ApiError
from scadbuddy.library.presets import ParamPresetCreate


async def test_the_colour_breakdown_reads_its_job_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1274: as the preview and the view do; the read is a database query."""
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []

    def missing(render: object, job_id: str) -> object:
        seen.append(threading.current_thread())
        raise ApiError(404, f"no job with id {job_id!r}")

    monkeypatch.setattr(jobs_api, "require_job", missing)
    none = cast(Any, None)
    with pytest.raises(ApiError):
        await jobs_api.get_job_colours("j", none, none, none, none)
    assert seen and seen[0] is not loop_thread


def test_a_copy_that_fails_validation_names_every_problem() -> None:
    """#1274: not only the first."""
    with pytest.raises(ValidationError) as caught:
        ParamPresetCreate(name="", description="x" * 100_000)
    detail = invalid_copy_detail(caught.value)
    assert len(caught.value.errors()) == 2
    for error in caught.value.errors():
        assert str(error["msg"]) in detail
