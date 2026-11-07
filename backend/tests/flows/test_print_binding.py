"""#4445: a flow's `print_output` call names, and its approval binds, the registry
printer that prints, as the agent's `prepareApproval` resolves it."""

import logging
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest

from scadbuddy.api import flows as flows_api
from scadbuddy.flows.print_binding import api_lookup, bind_call
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.printers.component import Printers
from scadbuddy.printers.models import PrinterRow

PRINTER = "00000000-0000-4000-8000-0000000000aa"
CALL: dict[str, Any] = {"output_id": "o1", "printer": PRINTER}


def row(bambuddy: int | None) -> PrinterRow:
    now = datetime(2026, 11, 6, tzinfo=UTC)
    return PrinterRow(
        id=PRINTER,
        serial="01P00A0000000AA",
        name="Garage",
        bambuddy_printer_id=bambuddy,
        created_at=now,
        updated_at=now,
    )


def holding(found: PrinterRow | None) -> Any:
    async def lookup(printer: str) -> PrinterRow | None:
        assert printer == PRINTER
        return found

    return lookup


async def nothing(printer: str) -> PrinterRow | None:
    raise AssertionError("no printer is read for this call")


async def test_a_linked_printer_binds_the_pair_and_names_it() -> None:
    bound = await bind_call("print_output", CALL, holding(row(2)), native(False))
    assert bound.args == {**CALL, "printer_id": 2}
    assert bound.summary == "Prints on Garage (Bambuddy printer 2)"


def native(lan: bool) -> Any:
    async def check(printer: str) -> bool:
        assert printer == PRINTER
        return lan

    return check


async def broken_native(printer: str) -> bool:
    raise RuntimeError("the settings are away")


# #5466: the line names the path the run takes, as the agent's `prepareApproval` words it,
# and never names Bambuddy for a run it could not check goes through Bambuddy.
@pytest.mark.parametrize(
    ("check", "line"),
    [
        (native(False), "Prints on Garage (Bambuddy printer 2)"),
        (native(True), "Prints on Garage over the LAN"),
        (None, "Prints on Garage"),
        (broken_native, "Prints on Garage"),
    ],
)
async def test_a_linked_printers_line_names_the_path_its_run_takes(check: Any, line: str) -> None:
    bound = await bind_call("print_output", CALL, holding(row(2)), check)
    assert bound.args == {**CALL, "printer_id": 2}
    assert bound.summary == line


async def test_a_call_asking_for_bambuddys_queue_is_named_so_on_a_lan_printer() -> None:
    """#5352: `path: "bambuddy"` goes through Bambuddy whatever the row's path."""
    call = {**CALL, "path": "bambuddy"}
    bound = await bind_call("print_output", call, holding(row(2)), native(True))
    assert bound.args == {**call, "printer_id": 2}
    assert bound.summary == "Prints on Garage (Bambuddy printer 2)"


async def test_a_lan_only_printer_asked_through_bambuddys_queue_names_the_refusal() -> None:
    """#5352: the run refuses it, so the line never says it prints over the LAN."""
    call = {**CALL, "path": "bambuddy"}
    bound = await bind_call("print_output", call, holding(row(None)), native(True))
    assert bound.summary == (
        "Refused: Garage links no Bambuddy printer, so it cannot print through Bambuddy's queue"
    )


async def test_a_printer_only_the_lan_reaches_is_not_checked_for_its_path() -> None:
    bound = await bind_call("print_output", CALL, holding(row(None)), nothing_native)
    assert bound.summary == "Prints on Garage over the LAN"


async def nothing_native(printer: str) -> bool:
    raise AssertionError("a row that links no Bambuddy printer needs no path read")


async def test_a_pair_the_script_sent_is_bound_as_sent() -> None:
    sent = {**CALL, "printer_id": 3}
    bound = await bind_call("print_output", sent, holding(row(2)), native(False))
    assert bound.args == sent
    assert bound.summary == "Prints on Garage (Bambuddy printer 2)"


# Security review: never the raw arguments, so a row linked after the approval hashes
# otherwise, and the agent never falls back to the raw hash for a person's decision.
async def test_a_printer_only_the_lan_reaches_binds_no_printer_id() -> None:
    bound = await bind_call("print_output", CALL, holding(row(None)))
    assert bound.args == {**CALL, "printer_id": None}
    assert bound.summary == "Prints on Garage over the LAN"


async def test_a_printer_the_registry_does_not_hold_binds_no_printer_id() -> None:
    bound = await bind_call("print_output", CALL, holding(None))
    assert bound.args == {**CALL, "printer_id": None}
    assert bound.summary == f"Prints on printer {PRINTER}, which the registry does not hold"


# Review of #5450: the agent sends `printer` verbatim and hashes it so; the binding keeps
# the script's string, case and all (the shared vector in approval-hash-vectors.json pins
# that hash on both sides).
async def test_an_uppercase_printer_is_bound_as_the_script_wrote_it() -> None:
    upper = {"output_id": "o1", "printer": PRINTER.upper()}
    seen: list[str] = []

    async def lookup(printer: str) -> PrinterRow | None:
        seen.append(printer)
        return row(2)

    bound = await bind_call("print_output", upper, lookup)
    assert bound.args == {**upper, "printer_id": 2}
    assert seen == [PRINTER.upper()]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("send_to_bambuddy", CALL),
        ("print_output", {"output_id": "o1", "printer_id": 2}),
        ("print_output", {"output_id": "o1", "printer": "garage"}),
        ("print_output", {"output_id": "o1", "printer": 7}),
        # Forms Python's uuid parses but the agent's schema refuses: never named here.
        ("print_output", {"output_id": "o1", "printer": "{" + PRINTER + "}"}),
        ("print_output", {"output_id": "o1", "printer": "urn:uuid:" + PRINTER}),
        ("print_output", {"output_id": "o1", "printer": PRINTER.replace("-", "")}),
        ("print_output", {"output_id": "o1", "printer": "00000000-0000-0000-0000-0000000000aa"}),
    ],
)
async def test_other_calls_read_no_printer(tool: str, args: dict[str, Any]) -> None:
    bound = await bind_call(tool, args, nothing)
    assert bound.args == args
    assert bound.summary is None


async def test_the_api_lookup_reads_the_row_and_says_none_for_a_404() -> None:
    linked = {"id": 2}

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/printers/{PRINTER}"
        if linked["id"] == 0:
            return httpx.Response(404, json={"detail": "no printer"})
        return httpx.Response(200, json=row(linked["id"]).model_dump(mode="json"))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="http://api"
    ) as client:
        lookup = api_lookup(client)
        found = await lookup(PRINTER)
        assert found is not None and found.bambuddy_printer_id == 2
        linked["id"] = 0
        assert await lookup(PRINTER) is None


async def test_the_api_lookup_raises_when_the_api_fails() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "away"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="http://api"
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await api_lookup(client)(PRINTER)


class _Settings:
    """The settings store, counting its reads."""

    def __init__(self) -> None:
        self.loads = 0

    def load(self) -> object:
        self.loads += 1
        return object()


class _Printers:
    store = None
    reports = None


# #5472: the run page reads which printers a run reaches over the LAN once, and a failed
# read once: two linked entries make one read and one log line, and name no path.
async def test_a_failed_lan_read_is_read_and_logged_once_per_page(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    reads: list[object] = []

    async def failing(settings: object, printers: object, reports: object) -> list[str]:
        reads.append(settings)
        raise RuntimeError("the registry is away")

    monkeypatch.setattr(flows_api, "lan_printers", failing)
    settings = _Settings()
    check = flows_api._native(cast(Printers, _Printers()), cast(SettingsStore, settings))
    with caplog.at_level(logging.WARNING, logger=flows_api.logger.name):
        lines = [
            (await bind_call("print_output", CALL, holding(row(2)), check)).summary
            for _ in range(2)
        ]
    assert lines == ["Prints on Garage", "Prints on Garage"]
    assert (settings.loads, len(reads)) == (1, 1)
    assert [r.getMessage() for r in caplog.records] == [
        "could not read which printers a run reaches over the LAN"
    ]


async def test_a_lan_read_answers_every_entry_on_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    async def lan(settings: object, printers: object, reports: object) -> list[str]:
        return [PRINTER]

    monkeypatch.setattr(flows_api, "lan_printers", lan)
    settings = _Settings()
    check = flows_api._native(cast(Printers, _Printers()), cast(SettingsStore, settings))
    assert [await check(PRINTER), await check("00000000-0000-4000-8000-0000000000bb")] == [
        True,
        False,
    ]
    assert settings.loads == 1
