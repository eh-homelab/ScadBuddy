"""The camera processes' feed-state reports (``POST …/printers/{printer_id}/feed``).

A process records the state it wants the API to hold for each printer (`FeedSender.want`).
A sender posts it in the background, on change only, and again with back-off until it lands;
the newest state wins. A refusal (4xx) stops the sender until the next state. A failed post
forgets the reported state, since the post may have reached the API before its answer was
lost, so the next state is always posted."""

from __future__ import annotations

import asyncio
import logging
from typing import Final, Literal
from urllib.parse import quote

from scadbuddy.camera.models import FeedReport, FeedState
from scadbuddy.crud.client import CrudClient, CrudRefusedError

logger = logging.getLogger(__name__)

#: The back-off of a feed report that did not land.
RETRY_MIN: Final = 0.5
RETRY_MAX: Final = 30.0


class FeedSender:
    def __init__(
        self,
        crud: CrudClient,
        base: str,
        part: Literal["relay", "recorder"],
        *,
        recorder: str | None = None,
        retry_min: float = RETRY_MIN,
        retry_max: float = RETRY_MAX,
    ) -> None:
        self._crud = crud
        self._base = base
        self._part = part
        self._recorder = recorder
        self._who = f"camera {part}"
        #: The feed state the process wants the API to hold, per printer.
        self._desired: dict[str, FeedState] = {}
        #: An error every report carries while it is set (`set_error`).
        self._error: str | None = None
        #: The last feed state and error the API took, per printer.
        self._reported: dict[str, tuple[FeedState, str | None]] = {}
        #: One sender per printer whose desired state has not landed yet.
        self._senders: dict[str, asyncio.Task[None]] = {}
        self._retry_min = retry_min
        self._retry_max = retry_max

    def desired(self, printer_id: str) -> FeedState | None:
        return self._desired.get(printer_id)

    def printers(self) -> set[str]:
        """The printers with a desired state."""
        return set(self._desired)

    def want(self, printer_id: str, state: FeedState) -> None:
        """Record the state the API should hold. A sender posts it in the background,
        again with back-off until it lands; a newer state replaces one still waiting."""
        self._desired[printer_id] = state
        sender = self._senders.get(printer_id)
        if self._reported.get(printer_id) != (state, self._error) and (
            sender is None or sender.done()
        ):
            self._senders[printer_id] = asyncio.create_task(self._send(printer_id))

    def set_error(self, error: str | None) -> None:
        """Set (or clear, with ``None``) the error every report carries. A change posts each
        printer's desired state again, but not ``stopped``: that printer is gone."""
        if error == self._error:
            return
        self._error = error
        for printer_id, state in list(self._desired.items()):
            if state != "stopped":
                self.want(printer_id, state)

    def forget(self, printer_id: str) -> None:
        """Drop the printer's desired state and cancel its sender, with any post it has in
        flight: a state still waiting is not posted again. A recorder calls it when it no
        longer leads the printer, so a former leader does not overwrite the new leader's
        report. A post the API already took before the cancel can still land."""
        self._desired.pop(printer_id, None)
        self._reported.pop(printer_id, None)
        sender = self._senders.pop(printer_id, None)
        if sender is not None:
            sender.cancel()

    async def _send(self, printer_id: str) -> None:
        delay = self._retry_min
        path = f"{self._base}/printers/{quote(printer_id, safe='')}/feed"
        while (state := self._desired.get(printer_id)) is not None and (
            sent := (state, self._error)
        ) != self._reported.get(printer_id):
            error = self._error
            report = FeedReport(part=self._part, state=state, error=error, recorder=self._recorder)
            body = report.model_dump(mode="json", exclude_none=True)
            try:
                await self._crud.request("POST", path, json=body)
            except CrudRefusedError as refused:
                # A refusal does not pass with time: stop. A later state or resync tries
                # again, since the API's state is no longer known.
                self._reported.pop(printer_id, None)
                logger.warning(
                    "%s: the API refused feed state %s for %s (%s)",
                    self._who,
                    state,
                    printer_id,
                    refused.status,
                )
                return
            except Exception as error:
                # The post may have reached the API before it failed (a lost answer): its
                # state is no longer known, so the next desired state is always posted.
                self._reported.pop(printer_id, None)
                logger.warning(
                    "%s: feed state %s for %s did not land (%s); retrying",
                    self._who,
                    state,
                    printer_id,
                    type(error).__name__,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._retry_max)
                continue
            self._reported[printer_id] = sent
            delay = self._retry_min

    async def flushed(self) -> None:
        """Wait until every desired feed state has landed."""
        while pending := [t for t in self._senders.values() if not t.done()]:
            # `asyncio.wait`, not `gather`: a sender `forget` cancels must not raise here.
            await asyncio.wait(pending)

    async def aclose(self) -> None:
        """Stop the senders."""
        for sender in self._senders.values():
            sender.cancel()
        await asyncio.gather(*self._senders.values(), return_exceptions=True)
