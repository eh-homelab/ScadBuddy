"""``lan_command``: one MQTT command and the reply that matches it (#7276, plan 2026-11-25
Task 2). The printer is a scripted stand-in injected through `LanDeps.connect`; no
printer or Bambuddy is contacted."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, cast
from unittest import mock

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.printers import print_activities
from scadbuddy.printers.lan import LanPrinter
from scadbuddy.printers.lan_command_models import LanCommandInput
from scadbuddy.printers.lan_commands import extrusion_cali_get, matches
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.print_activities import LanActivities, LanDeps
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from scadbuddy.workflows.print_models import (
    FAILED,
    LAN_NO_ANSWER,
    LAN_READ_TRIES,
    LAN_REFUSED,
    LAN_UNSURE,
)
from tests.conftest import PgPool
from tests.printers.conftest import KEK

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000001"
SEQ = "424242"
T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
#: The activity's source of sequence ids, fixed where a test must know the id.
RANDBELOW = "scadbuddy.printers.print_activities.secrets.randbelow"


class Scripted:
    """A printer that answers each request with the first of ``replies`` that matches the
    command and sequence it was sent, or nothing. ``fails`` is raised instead, as a
    connection that could not be made."""

    def __init__(
        self, replies: list[dict[str, Any]] | None = None, fails: BaseException | None = None
    ) -> None:
        self.replies = replies or []
        self.fails = fails
        self.published: list[dict[str, Any]] = []
        self.waits: list[float] = []

    async def request(
        self, command: dict[str, Any], *, name: str, seq: str, wait: float
    ) -> dict[str, Any] | None:
        if self.fails is not None:
            raise self.fails
        self.published.append(command)
        self.waits.append(wait)
        return next((r for r in self.replies if matches(r, name, seq)), None)


def write_input(**fields: Any) -> LanCommandInput:
    return LanCommandInput(
        printer_id="",
        name="extrusion_cali_set",
        payload={"print": {"command": "extrusion_cali_set", "sequence_id": "0"}},
        wait_s=6.0,
        kind="write",
    ).model_copy(update=fields)


def read_input(**fields: Any) -> LanCommandInput:
    return LanCommandInput(
        printer_id="",
        name="extrusion_cali_get",
        payload=extrusion_cali_get("0.4", "0"),
        wait_s=5.0,
        kind="read",
    ).model_copy(update=fields)


def _printer(pg_pool: PgPool, *, linked: bool = True) -> tuple[PrinterStore, str]:
    printers = PrinterStore(pg_pool, KEK)
    if (made := next(iter(printers.list()), None)) is not None:
        return printers, made.id
    row = printers.create(PrinterCreate(serial=SERIAL, name="H2C"))
    if linked:
        printers.link_lan(
            row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
        )
    return printers, row.id


async def _run(
    pg_pool: PgPool,
    lan: Scripted,
    input: LanCommandInput,
    *,
    linked: bool = True,
    reports: PrinterReportStore | None = None,
    then: Callable[[str], None] | None = None,
) -> Any:
    printers, printer_id = _printer(pg_pool, linked=linked)
    if then is not None:
        then(printer_id)
    deps = LanDeps(
        printers=printers,
        settings_store=cast(SettingsStore, None),
        connect=lambda *_: cast(LanPrinter, lan),
        reports=reports,
    )
    activities = LanActivities(deps)
    with mock.patch.object(print_activities, "LAN_READ_PAUSE", 0.0):
        return await ActivityEnvironment().run(
            activities.lan_command, input.model_copy(update={"printer_id": printer_id})
        )


def _fixed_seq() -> Any:
    return mock.patch(RANDBELOW, return_value=int(SEQ))


async def test_the_reply_matching_command_and_sequence_is_answered(pg_pool: PgPool) -> None:
    ours = {"command": "extrusion_cali_set", "sequence_id": SEQ, "result": "success"}
    lan = Scripted(
        replies=[
            {"command": "extrusion_cali_set", "sequence_id": "7", "result": "fail"},
            {"command": "extrusion_cali_get", "sequence_id": SEQ, "result": "fail"},
            ours,
        ]
    )
    with _fixed_seq():
        reply = await _run(pg_pool, lan, write_input())
    assert reply.confirmed
    assert reply.reply == ours
    assert reply.received_at is not None
    assert lan.published[0]["print"]["sequence_id"] == SEQ
    assert lan.waits == [6.0]


async def test_no_reply_is_unconfirmed_not_failed(pg_pool: PgPool) -> None:
    lan = Scripted()
    with _fixed_seq():
        reply = await _run(pg_pool, lan, write_input())
    assert not reply.confirmed
    assert reply.reply is None
    assert reply.received_at is None
    # A write is sent once: a resend could apply it twice.
    assert len(lan.published) == 1


async def test_a_fail_reply_is_refused_with_its_reason(pg_pool: PgPool) -> None:
    lan = Scripted(
        replies=[
            {
                "command": "extrusion_cali_set",
                "sequence_id": SEQ,
                "result": "FAIL",
                "reason": "ERROR STATE",
            }
        ]
    )
    with _fixed_seq(), pytest.raises(ApplicationError) as raised:
        await _run(pg_pool, lan, write_input())
    assert raised.value.type == LAN_REFUSED
    assert raised.value.non_retryable
    assert "ERROR STATE" in raised.value.message


async def test_a_read_with_no_reply_is_no_answer(pg_pool: PgPool) -> None:
    lan = Scripted()
    with pytest.raises(ApplicationError) as raised:
        await _run(pg_pool, lan, read_input())
    assert raised.value.type == LAN_NO_ANSWER
    assert raised.value.non_retryable
    # Bambuddy's three tries (`get_kprofiles`), each under a sequence of its own.
    assert len(lan.published) == LAN_READ_TRIES == 3
    assert len({p["print"]["sequence_id"] for p in lan.published}) == 3


async def test_a_read_answered_on_a_later_try_is_confirmed(pg_pool: PgPool) -> None:
    lan = Scripted()
    seqs = iter([1, 2, 3])
    lan.replies = [{"command": "extrusion_cali_get", "sequence_id": "2", "filaments": []}]
    with mock.patch(RANDBELOW, side_effect=lambda _: next(seqs)):
        reply = await _run(pg_pool, lan, read_input())
    assert reply.confirmed
    assert reply.reply == lan.replies[0]
    assert len(lan.published) == 2


async def test_each_command_gets_a_fresh_sequence_id(pg_pool: PgPool) -> None:
    lan = Scripted()
    await _run(pg_pool, lan, write_input())
    await _run(pg_pool, lan, write_input())
    first, second = (p["print"]["sequence_id"] for p in lan.published)
    assert first != "0" and second != "0"
    assert first.isdecimal() and second.isdecimal()
    assert first != second


async def test_the_input_payload_is_left_as_it_was(pg_pool: PgPool) -> None:
    input = write_input()
    with _fixed_seq():
        await _run(pg_pool, Scripted(), input)
    assert input.payload["print"]["sequence_id"] == "0"


async def test_a_row_without_a_lan_link_is_409(pg_pool: PgPool) -> None:
    lan = Scripted()
    with pytest.raises(ApplicationError) as raised:
        await _run(pg_pool, lan, write_input(), linked=False)
    assert raised.value.type == FAILED
    [problem] = raised.value.details
    assert cast(Mapping[str, Any], _dump(problem))["status"] == 409
    detail = cast(Mapping[str, Any], _dump(problem))["detail"]
    assert "Settings > Printers" in detail
    assert "print again" not in detail
    assert lan.published == []


async def test_a_printer_that_cannot_be_reached_is_unsure(pg_pool: PgPool) -> None:
    lan = Scripted(fails=OSError("no route to host"))
    with pytest.raises(ApplicationError) as raised:
        await _run(pg_pool, lan, write_input())
    assert raised.value.type == LAN_UNSURE
    assert raised.value.non_retryable


async def test_the_reply_never_reaches_the_report_store(pg_pool: PgPool) -> None:
    reports = PrinterReportStore(pg_pool)
    kept = {"gcode_state": "IDLE", "nozzle_temper": 25}

    def seed(printer_id: str) -> None:
        reports.put(printer_id, kept, T0)

    lan = Scripted(
        replies=[{"command": "extrusion_cali_set", "sequence_id": SEQ, "gcode_state": "FAILED"}]
    )
    with _fixed_seq():
        reply = await _run(pg_pool, lan, write_input(), reports=reports, then=seed)
    assert reply.confirmed
    printers = PrinterStore(pg_pool, KEK)
    [row] = printers.list()
    stored = reports.get(row.id)
    assert stored is not None and stored[0] == kept


def _dump(problem: Any) -> Any:
    return problem.model_dump() if hasattr(problem, "model_dump") else problem
