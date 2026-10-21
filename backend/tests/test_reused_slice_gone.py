"""#2955, #3895: whether a refused ``POST /queue/`` was for a reused slice Bambuddy no
longer has, and what is logged when it was not."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import UpstreamAnswer
from scadbuddy.core.problems import ApiError
from scadbuddy.workflows import print_activities
from scadbuddy.workflows.print_models import EnqueueInput

SLICED = 77


class _Reads:
    def __init__(self, error: ApiError | None) -> None:
        self.error = error

    async def library_file(self, file_id: int) -> Any:
        if self.error is not None:
            raise self.error
        return SimpleNamespace(id=file_id)


def _reused() -> EnqueueInput:
    plate = SimpleNamespace(plan=object(), sliced_file_id=SLICED)
    return cast(EnqueueInput, SimpleNamespace(plate=plate, sliced=SLICED))


def _refused(status: int) -> ApiError:
    error = ApiError(502, "Bambuddy refused the queue")
    error.upstream = UpstreamAnswer(status=status)
    return error


async def _gone(read_back: ApiError | None) -> bool:
    client = cast(BambuddyClient, _Reads(read_back))
    return await print_activities._reused_slice_gone(client, _reused(), _refused(400))


async def test_a_404_on_the_read_back_is_a_gone_slice() -> None:
    assert await _gone(ApiError(404, "File not found"))


@pytest.mark.parametrize(
    ("read_back", "message", "status"),
    [
        (ApiError(403, "Forbidden"), "a refused queue's slice could not be read back", 403),
        (None, "a queue was refused while its slice is still present", 400),
    ],
)
async def test_a_refusal_that_is_not_a_gone_slice_says_why(
    caplog: pytest.LogCaptureFixture, read_back: ApiError | None, message: str, status: int
) -> None:
    with caplog.at_level(logging.INFO, logger=print_activities.__name__):
        assert not await _gone(read_back)
    [record] = [r for r in caplog.records if r.name == print_activities.__name__]
    assert record.getMessage() == message
    assert (record.__dict__["library_file_id"], record.__dict__["status"]) == (SLICED, status)
