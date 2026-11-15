"""A native copy's reuse is recorded best effort (#4337): on the print worker the touch
goes through the API's CRUD route (#5298 Task 25), and an API that does not answer or
refuses it leaves the print going, as a failed SQL touch did."""

from __future__ import annotations

from typing import cast

import pytest

from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.print_library.copies import _touch


class _FailingTouch:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def touch(self, output_id: str, library_file_id: int) -> None:
        self.calls += 1
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        CrudUnavailableError("no answer", "unreached"),
        CrudRefusedError(422, {"detail": "refused"}),
    ],
    ids=["unavailable", "refused"],
)
async def test_a_touch_the_api_does_not_take_leaves_the_print_going(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    uploads = _FailingTouch(error)

    await _touch(cast(BambuddyUploadStore, uploads), "out-1", 7)

    assert uploads.calls == 1
    assert "could not record a native copy's reuse" in caplog.text
