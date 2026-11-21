"""A location sensor's alert notifications, sent (#6646; plan
`docs/superpowers/plans/2026-11-18-filament-weights-sensors.md` Decisions 29-38).

`sensors.SensorStore.record_on` queues a row of `filament_sensor_notifications` when a
stored reading takes a sensor with ``notify`` on into or out of its alert band. This
module sends the queued rows through Bambuddy's ``POST /notifications/app-message``, so
they reach the channels the owner already set up in Bambuddy (each with "Messages from
connected apps" on), with the key ScadBuddy already holds: no new secret and no new
outbound path. The spool refresh's pull runs it (`bambuddy_pull.scheduled_pull`), in the
API process, which holds the store.

A row is claimed (``sending``) in its own committed statement before the call and ended
``sent`` or ``failed`` after it, so it is sent at most once: a failed send is recorded
with a fixed code (`NotificationError`) and not tried again, and a row a crash left
``sending`` stays so."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.errors import NOT_CONFIGURED_PROBLEM, SCOPE_PROBLEM, UNAVAILABLE_PROBLEM
from scadbuddy.core.problems import ApiError
from scadbuddy.filament.models import NotificationError
from scadbuddy.library.settings_store import StoredSettings

if TYPE_CHECKING:
    from scadbuddy.filament.component import Filament

logger = logging.getLogger(__name__)

#: How many rows one pass sends: Bambuddy takes 20 app messages a minute per key
#: (`BambuddyClient.send_app_message`), and the pass runs every 5 minutes by default.
PASS_LIMIT = 10

#: Bambuddy's ``AppMessage.title`` cap.
TITLE_MAX = 120

_KINDS = {"humidity": "Humidity", "temperature": "Temperature"}


@dataclass(frozen=True)
class DeliveryReport:
    sent: int
    failed: int


def _claim(store: Filament, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` queued rows, oldest first, marked ``sending`` in one committed
    statement, with what the message says. ``SKIP LOCKED``: two passes never take one row."""
    with store.spools.transaction() as conn:
        return conn.execute(
            "WITH claimed AS ("
            " UPDATE filament_sensor_notifications SET state = 'sending'"
            " WHERE id IN (SELECT id FROM filament_sensor_notifications WHERE state = 'queued'"
            " ORDER BY queued_at, id LIMIT %s FOR UPDATE SKIP LOCKED)"
            " RETURNING id, sensor_id, kind, value, read_at, queued_at)"
            " SELECT c.id, c.kind, c.value, c.read_at, s.name, s.kind AS sensor_kind, s.unit,"
            " s.alert_above, s.alert_below, l.name AS place"
            " FROM claimed c JOIN filament_location_sensors s ON s.id = c.sensor_id"
            " JOIN filament_locations l ON l.id = s.location_id ORDER BY c.queued_at, c.id",
            (limit,),
        ).fetchall()


def _end(store: Filament, row_id: UUID, channels: int | None, error: str | None) -> None:
    with store.spools.transaction() as conn:
        conn.execute(
            "UPDATE filament_sensor_notifications SET state = %s, channels = %s, error = %s,"
            " sent_at = now() WHERE id = %s AND state = 'sending'",
            ("failed" if error else "sent", channels, error, row_id),
        )


def _number(value: float, unit: str | None) -> str:
    text = f"{value:g}"
    return f"{text} {unit}" if unit else text


def message_of(row: dict[str, Any]) -> tuple[str, str]:
    """The notification's title and text: the sensor, its value and place, its band and
    when it was read (UTC)."""
    value, unit = row["value"], row["unit"]
    above, below = row["alert_above"], row["alert_below"]
    if row["kind"] == "started":
        over = above is not None and value > above
        title = f"{row['name']}: {'above' if over else 'below'} the limit"
    else:
        title = f"{row['name']}: back within its limits"
    band = ", ".join(
        part
        for part in (
            above is not None and f"above {_number(above, unit)}",
            below is not None and f"below {_number(below, unit)}",
        )
        if part
    )
    read_at: datetime = row["read_at"]
    text = (
        f"{_KINDS.get(row['sensor_kind'], 'The reading')} is {_number(value, unit)}"
        f" in {row['place']}."
        + (f" It alerts {band}." if band else "")
        + f" Read {read_at:%Y-%m-%d %H:%M} UTC. Sent by ScadBuddy."
    )
    return title[:TITLE_MAX], text


def failure_of(error: Exception) -> NotificationError:
    """The fixed code a failed send records; never Bambuddy's text."""
    if not isinstance(error, ApiError):
        return "internal"
    if error.type == SCOPE_PROBLEM:
        return "scope"
    if error.type == NOT_CONFIGURED_PROBLEM:
        return "not_configured"
    if error.extensions.get("bambuddy_status") == 429:
        return "rate_limited"
    if error.type == UNAVAILABLE_PROBLEM:
        return "unavailable"
    return "rejected"


async def deliver_pending(
    store: Filament, settings: StoredSettings, *, limit: int = PASS_LIMIT
) -> DeliveryReport:
    """Send up to ``limit`` queued notifications through Bambuddy. With no Bambuddy URL
    nothing is claimed: the rows wait for one. A row whose send fails is ended ``failed``
    with its code; the others are still sent."""
    if not settings.bambuddy_url:
        return DeliveryReport(sent=0, failed=0)
    rows = await asyncio.to_thread(_claim, store, limit)
    if not rows:
        return DeliveryReport(sent=0, failed=0)
    sent = failed = 0
    async with client_for(settings) as client:
        for row in rows:
            title, text = message_of(row)
            channels: int | None = None
            error: NotificationError | None
            try:
                channels = await client.send_app_message(title=title, message=text)
                error = None if channels > 0 else "no_channel"
            except Exception as exc:
                error = failure_of(exc)
                logger.warning(
                    "could not send a sensor notification",
                    extra={"error": type(exc).__name__, "code": error},
                )
            await asyncio.to_thread(_end, store, row["id"], channels, error)
            if error:
                failed += 1
            else:
                sent += 1
    return DeliveryReport(sent=sent, failed=failed)
