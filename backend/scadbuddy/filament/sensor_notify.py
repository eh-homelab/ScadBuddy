"""A location sensor's alert notifications, sent by the print worker (#6646; plan
`docs/superpowers/plans/2026-11-18-filament-weights-sensors.md` Decisions 29-50).

`sensors.SensorStore.record_on` queues a row of `filament_sensor_notifications`, with its
title and message, when a stored reading opens or closes the alert of a sensor with
``notify`` on. The ``sensor_notify_send`` activity, on the ``bambuddy`` queue every minute
(`workflows/sensor_notify.py`), claims the queued rows through SB's side
(`SensorNotificationsApi`: the CRUD routes ``…/print/filament/sensor-notifications/*``,
`crud/print.py` `RemoteSensorNotifications`, or the API's own pool in-process,
`sensors.PoolSensorNotifications`) and sends each through Bambuddy's
``POST /notifications/app-message``, so it reaches the channels the owner already set up in
Bambuddy (each with "Messages from connected apps" on), with the key ScadBuddy already
holds. The CRUD service makes no Bambuddy call for them (Decision 40).

A row is claimed (``sending``) before the call and ended ``sent`` or ``failed`` after it,
so it is sent at most once: a failed send is recorded with a fixed code
(`m.SendError`) and not tried again, and a row a crash left ``sending`` is ended
``interrupted`` by a later claim."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx
from temporalio import activity

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.errors import (
    NOT_CONFIGURED_PROBLEM,
    NOT_FOUND_PROBLEM,
    SCOPE_PROBLEM,
    UNAVAILABLE_PROBLEM,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.filament.models import ClaimedNotification, NotificationEnd, SendError
from scadbuddy.library.settings_store import StoredSettings

logger = logging.getLogger(__name__)

SENSOR_NOTIFY_ACTIVITY = "sensor_notify_send"

#: How many rows one pass sends: Bambuddy takes 20 app messages a minute per key
#: (`BambuddyClient.send_app_message`), and a pass runs every minute.
PASS_LIMIT = 10

#: Each phase (connect, write, read) of one send, in seconds: one send takes at most three
#: of them, so 15 s, not the client's 30 s per phase.
SEND_PHASE_TIMEOUT = 5.0

#: The pass starts no send after this many seconds. The workflow's `SEND_TIMEOUT` (120 s)
#: must outlast it plus one send (at most 3 x `SEND_PHASE_TIMEOUT`) and the ends, so the
#: activity is never cancelled while a row is ``sending``: a row it did not reach goes
#: back to ``queued`` instead of being ended ``interrupted`` 10 minutes later.
PASS_BUDGET = 75.0


class SensorNotificationsApi(Protocol):
    """SB's side of the send: claim queued rows, end a claimed one."""

    async def claim(self, limit: int) -> Sequence[ClaimedNotification]: ...

    async def end(self, row_id: UUID, end: NotificationEnd) -> None: ...


@dataclass(frozen=True)
class DeliveryReport:
    sent: int
    failed: int


def failure_of(error: Exception) -> SendError:
    """The fixed code a failed send records; never Bambuddy's text. A 401 is the key
    refused (``unauthorized``), a 403 the key's missing permission (``scope``), a 404 a
    Bambuddy older than 1.3.1 (``not_found``). A read or write timeout is ``interrupted``
    (it may have gone out, and is never sent again); a connect timeout is ``unavailable``."""
    if not isinstance(error, ApiError):
        return "internal"
    if isinstance(error.__cause__, (httpx.ReadTimeout, httpx.WriteTimeout)):
        # The request was (at least in part) sent: the message may have gone out.
        return "interrupted"
    status = error.extensions.get("bambuddy_status")
    if error.type == SCOPE_PROBLEM:
        return "unauthorized" if status == 401 else "scope"
    if error.type == NOT_CONFIGURED_PROBLEM:
        return "not_configured"
    if error.type == NOT_FOUND_PROBLEM:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if error.type == UNAVAILABLE_PROBLEM:
        return "unavailable"
    return "rejected"


async def deliver_pending(
    api: SensorNotificationsApi,
    load: Callable[[], Awaitable[StoredSettings]],
    *,
    limit: int = PASS_LIMIT,
    budget: float = PASS_BUDGET,
    phase_timeout: float = SEND_PHASE_TIMEOUT,
) -> DeliveryReport:
    """Send up to ``limit`` queued notifications through Bambuddy. With no Bambuddy URL
    nothing is claimed: the rows wait for one. The Bambuddy client is built before the
    claim, so a client that cannot be built claims nothing. A row whose send fails is
    ended ``failed`` with its code; the others are still sent. A failed end (SB out of
    reach) raises: the rows claimed and not ended are ended ``interrupted`` by a later
    claim. The pass starts no send after ``budget`` seconds; the rows it did not try go
    back to ``queued``."""
    settings = await load()
    if not settings.bambuddy_url:
        return DeliveryReport(sent=0, failed=0)
    sent = failed = 0
    # The client is built before the claim (Decision 50): a client that cannot be built
    # leaves every row queued for the next pass, never ``sending`` until it is ended
    # ``interrupted``.
    async with client_for(settings) as client:
        rows = await api.claim(limit)
        deadline = time.monotonic() + budget
        for row in rows:
            if time.monotonic() >= deadline:
                await api.end(row.id, NotificationEnd(requeue=True))
                continue
            try:
                channels = await client.send_app_message(
                    title=row.title, message=row.message, timeout=phase_timeout
                )
                end = (
                    NotificationEnd(channels=channels)
                    if channels > 0
                    else NotificationEnd(channels=0, error="no_channel")
                )
            except Exception as exc:
                end = NotificationEnd(error=failure_of(exc))
                logger.warning(
                    "could not send a sensor notification",
                    extra={"error": type(exc).__name__, "code": end.error},
                )
            await api.end(row.id, end)
            if end.error:
                failed += 1
            else:
                sent += 1
    return DeliveryReport(sent=sent, failed=failed)


class SensorNotifyActivities:
    """The ``sensor_notify_send`` activity. ``api`` is `sensors.PoolSensorNotifications` in
    the API's process and `crud/print.py` `RemoteSensorNotifications` on the print worker;
    ``load`` reads the stored settings (the Bambuddy URL and key)."""

    def __init__(
        self, api: SensorNotificationsApi, load: Callable[[], Awaitable[StoredSettings]]
    ) -> None:
        self.api = api
        self.load = load

    @activity.defn(name=SENSOR_NOTIFY_ACTIVITY)
    async def deliver(self) -> DeliveryReport:
        report = await deliver_pending(self.api, self.load)
        if report.sent or report.failed:
            logger.info(
                "sent sensor notifications", extra={"sent": report.sent, "failed": report.failed}
            )
        return report
