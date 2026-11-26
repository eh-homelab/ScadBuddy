"""The print worker's read of the unknown-trays block (#7143). The route is `scadbuddy-core`'s
(Go): Go's test pins `tests/fixtures/unknown-trays-block.json` byte for byte, and these
tests read the same file. Python has no route and no database here."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.crud.client import CrudClient, CrudUnavailableError
from scadbuddy.crud.print import BASE, RemoteUnknownTrays
from scadbuddy.filament.unknown_tray_rows import UnknownSpoolBlock
from scadbuddy.worker import PRINT_CRUD_AREAS

GOLDEN = Path(__file__).parents[1] / "fixtures" / "unknown-trays-block.json"
PRINTER = "5d0c8f1e-0000-4000-8000-000000000143"


class StubClient:
    """The one method `RemoteUnknownTrays` calls, answering the golden file or an error."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.seen: list[tuple[str, str]] = []

    async def request(self, method: str, path: str, **_: Any) -> httpx.Response:
        self.seen.append((method, path))
        if self.error is not None:
            raise self.error
        return httpx.Response(200, content=GOLDEN.read_bytes())


def test_the_print_worker_waits_for_the_unknown_trays_area() -> None:
    assert "unknown-trays" in PRINT_CRUD_AREAS
    assert CRUD_AREAS["print"][-1] == "unknown-trays"


async def test_remote_blocking_parses_the_golden_block() -> None:
    stub = StubClient()

    refs = await RemoteUnknownTrays(cast(CrudClient, stub)).blocking(PRINTER)

    golden = UnknownSpoolBlock.model_validate(json.loads(GOLDEN.read_text()))
    assert golden.count == len(golden.trays) == 2
    assert refs == golden.trays
    assert stub.seen == [("GET", f"{BASE}/filament/printers/{PRINTER}/unknown-trays")]


async def test_remote_blocking_raises_when_the_route_is_missing() -> None:
    missing = CrudUnavailableError("404 without X-ScadBuddy-Crud", "route_missing")

    with pytest.raises(CrudUnavailableError):
        await RemoteUnknownTrays(cast(CrudClient, StubClient(missing))).blocking(PRINTER)
