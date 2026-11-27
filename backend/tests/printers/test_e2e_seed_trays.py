"""The real-stack seed (`e2e-stack/seed.sh`, #7143) makes a spool for each untagged
filled tray of the fake printer's report (`e2e-stack/fake-printer/report.json`) with
``new-spool``, which answers 409 for a tray the report does not show filled. Its list is
the trays the app reads as filled and untagged from that report (hot-path-e2e run
45980008386: ``AMS 128 tray 0`` answered 409 sixty times, since the report's
``tray_exist_bits`` has no AMS-HT bit)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from scadbuddy.bambuddy.trays import seen_trays
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.status import status_from_report
from scadbuddy.spools.models import clean_tag_uid, clean_tray_uuid

STACK = Path(__file__).resolve().parents[3] / "e2e-stack"
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


def _seeded() -> list[tuple[int, int]]:
    script = (STACK / "seed.sh").read_text()
    loop = re.search(r'^for tray in ((?:"\d+ \d+" ?)+); do$', script, re.M)
    assert loop is not None, "seed.sh has no `for tray in ...` loop"
    return [(int(ams), int(tray)) for ams, tray in re.findall(r'"(\d+) (\d+)"', loop.group(1))]


def test_the_seed_makes_a_spool_for_each_untagged_filled_tray_of_the_fake() -> None:
    report = json.loads((STACK / "fake-printer" / "report.json").read_text())
    data = report.get("print", report)
    row = PrinterRow(id="p1", serial="01P00FAKE000001", name="Fake", created_at=NOW, updated_at=NOW)
    status = status_from_report(
        row, LanReport(serial=row.serial, received_at=NOW, print=data), now=NOW
    )
    untagged = [
        (t.ams_id, t.tray_id)
        for t in seen_trays(status)
        if clean_tag_uid(t.tag_uid) is None and clean_tray_uuid(t.tray_uuid) is None
    ]
    assert _seeded() == untagged
