"""#4445: a flow's `print_output` call names, and its approval binds, the registry
printer that prints, as the agent's `prepareApproval` resolves it."""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from scadbuddy.flows.print_binding import api_lookup, bind_call
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
    bound = await bind_call("print_output", CALL, holding(row(2)))
    assert bound.args == {**CALL, "printer_id": 2}
    assert bound.summary == "Prints on Garage (Bambuddy printer 2)"


async def test_a_pair_the_script_sent_is_bound_as_sent() -> None:
    sent = {**CALL, "printer_id": 3}
    bound = await bind_call("print_output", sent, holding(row(2)))
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
