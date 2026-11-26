"""``lan_command`` against the fake LAN printer (``e2e-stack/fake-printer/printer.py``) over
its real broker (#7276, plan 2026-11-25 Task 3): `LanDeps.connect` is the real `LanPrinter`,
which `tests/support/fake_lan.py` points at the fake. No printer or Bambuddy is contacted."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.printers.lan_command_models import LanCommandInput, LanCommandReply
from scadbuddy.printers.lan_commands import KProfileRow, extrusion_cali_get, extrusion_cali_set
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.print_activities import LanActivities, LanDeps
from scadbuddy.printers.store import PrinterStore
from scadbuddy.workflows.print_models import LAN_REPLY_WAIT_READ, LAN_REPLY_WAIT_WRITE
from tests.conftest import PgPool
from tests.printers.conftest import KEK
from tests.support.fake_lan import FakeLan, needs_fake_lan, running_fake_lan

pytestmark = [pytest.mark.requires_postgres, *needs_fake_lan]

ROW = KProfileRow(
    cali_idx=-1,
    extruder_id=0,
    nozzle_id="HS01-0.4",
    filament_id="GFA00",
    name="PLA Basic",
    k_value="0.025000",
    setting_id="PF00000000000000001",
)


@pytest.fixture
async def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeLan]:
    async with running_fake_lan(tmp_path, monkeypatch) as lan:
        yield lan


async def _run(pg_pool: PgPool, fake: FakeLan, input: LanCommandInput) -> LanCommandReply:
    printers = PrinterStore(pg_pool, KEK)
    row = next(iter(printers.list()), None) or printers.create(
        PrinterCreate(serial=fake.serial, name="Fake H2C", model="H2C")
    )
    if row.lan_host is None:
        printers.link_lan(
            row.id,
            LanLink(host=fake.host, access_code=fake.access_code),
            cert_sha256=fake.cert_sha256,
        )
    activities = LanActivities(LanDeps(printers=printers, settings_store=cast(SettingsStore, None)))
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, start_to_close_timeout=timedelta(seconds=60))
    reply: LanCommandReply = await env.run(
        activities.lan_command, input.model_copy(update={"printer_id": row.id})
    )
    return reply


def _sent(fake: FakeLan, command: str) -> list[dict[str, Any]]:
    return [
        entry["print"]
        for entry in fake.received()["mqtt"]
        if entry.get("print", {}).get("command") == command
    ]


async def test_a_write_then_a_read_each_reach_the_printer_once(
    pg_pool: PgPool, fake: FakeLan
) -> None:
    write = await _run(
        pg_pool,
        fake,
        LanCommandInput(
            printer_id="",
            name="extrusion_cali_set",
            payload=extrusion_cali_set([ROW], "0.4", "0"),
            wait_s=LAN_REPLY_WAIT_WRITE,
            kind="write",
        ),
    )
    assert write.confirmed and write.reply is not None
    assert write.reply["result"] == "success"

    read = await _run(
        pg_pool,
        fake,
        LanCommandInput(
            printer_id="",
            name="extrusion_cali_get",
            payload=extrusion_cali_get("0.4", "0"),
            wait_s=LAN_REPLY_WAIT_READ,
            kind="read",
        ),
    )
    assert read.confirmed and read.reply is not None
    assert [(p["filament_id"], p["k_value"]) for p in read.reply["filaments"]] == [
        ("GFA00", "0.025000")
    ]

    # Each sent once, under the fresh sequence each reply echoed.
    sets, gets = _sent(fake, "extrusion_cali_set"), _sent(fake, "extrusion_cali_get")
    assert [s["sequence_id"] for s in sets] == [write.reply["sequence_id"]]
    assert [g["sequence_id"] for g in gets] == [read.reply["sequence_id"]]
    assert write.reply["sequence_id"] != "0"
