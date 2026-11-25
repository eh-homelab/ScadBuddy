"""The camera processes' follower of the API's change stream
(``GET …/crud/camera[-relay]/changes``, `api/crud_camera.py`).

The stream is filtered, and a dropped live change moves no place, so a long quiet run usually
ends in ``ready {resync}``. Callers re-read their full list in ``on_resync``; they never
depend on replay. Back-off, ``Last-Event-ID`` and the silence timeout mirror
``agent/src/events/changeStream.ts``."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable

from scadbuddy.crud.client import CrudClient

logger = logging.getLogger(__name__)

RETRY_MIN = 0.5
RETRY_MAX = 30.0
#: The API sends a keepalive every 15 s: three of them without a byte mean a dead stream.
IDLE = 45.0
LINE_MAX = 1024 * 1024
CHANGE_KINDS = frozenset({"printer.changed", "camera.reconnect"})
#: The frames whose ``id`` is the stream's place.
PLACED = frozenset({"change", "ready", "reconnected"})


class ChangeFollower:
    def __init__(
        self,
        client: CrudClient,
        path: str,
        on_change: Callable[[str], Awaitable[None]],
        on_resync: Callable[[], Awaitable[None]],
        *,
        retry_min: float = RETRY_MIN,
        retry_max: float = RETRY_MAX,
        idle: float = IDLE,
        resync_first: bool = False,
    ) -> None:
        self._client = client
        self._path = path
        self._on_change = on_change
        self._on_resync = on_resync
        self._retry_min = retry_min
        self._retry_max = retry_max
        self._idle = idle
        self._last_id: str | None = None
        #: A callback failed: the next ``ready`` re-reads, until one ``on_resync`` returns.
        #: ``resync_first`` sets it at the start: a first stream starts at the newest event
        #: with no ``resync``, so a change between the caller's first read and that stream
        #: would be lost.
        self._pending = resync_first

    async def run(self) -> None:
        """Follow the stream until cancelled, opening it again whenever it drops."""
        delay = self._retry_min
        while True:
            opened = await self._once()
            if opened:
                delay = self._retry_min
            await asyncio.sleep(delay * (0.5 + random.random() / 2))
            delay = min(delay * 2, self._retry_max)

    async def _once(self) -> bool:
        """One stream, until it ends; whether it opened (sent ``ready``)."""
        opened = False
        stream = None
        try:
            async with asyncio.timeout(self._idle):
                headers = {"Accept": "text/event-stream"}
                if self._last_id is not None:
                    headers["Last-Event-ID"] = self._last_id
                stream = await self._client.stream("GET", self._path, headers=headers)
            frame: dict[str, str] = {}
            buffer = ""
            chunks = stream.aiter_bytes()
            while True:
                try:
                    async with asyncio.timeout(self._idle):
                        chunk = await anext(chunks)
                except StopAsyncIteration:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                while (newline := buffer.find("\n")) >= 0:
                    line, buffer = buffer[:newline].rstrip("\r"), buffer[newline + 1 :]
                    if line == "":
                        if "data" in frame:
                            opened = await self._dispatch(frame) or opened
                        frame = {}
                    elif not line.startswith(":"):
                        name, _, value = line.partition(":")
                        value = value.removeprefix(" ")
                        frame[name] = (
                            f"{frame[name]}\n{value}" if name == "data" and name in frame else value
                        )
                if len(buffer) > LINE_MAX:
                    raise ValueError(f"a change stream line passed {LINE_MAX} characters")
            logger.warning("camera change stream ended by the API; reconnecting")
        except TimeoutError:
            logger.warning("camera change stream went silent; reconnecting")
        except Exception as error:
            logger.warning(
                "camera change stream dropped (%s: %s); reconnecting", type(error).__name__, error
            )
        finally:
            if stream is not None:
                await stream.aclose()
        return opened

    async def _resync(self) -> None:
        self._pending = True
        await self._on_resync()
        self._pending = False

    async def _dispatch(self, frame: dict[str, str]) -> bool:
        """Handle one frame; true for ``ready``."""
        event = frame.get("event", "message")
        try:
            data = json.loads(frame["data"])
        except ValueError:
            logger.warning("camera change stream: ignored a malformed %s frame", event)
            return False
        if "id" in frame and event in PLACED:
            self._last_id = frame["id"]
        if event == "ready":
            if self._pending or data.get("resync") or data.get("reconnect"):
                await self._resync()
            return True
        if event == "reconnected":
            await self._resync()
        elif event == "change":
            printer_id = self._printer_id(data)
            if printer_id is not None:
                try:
                    await self._on_change(printer_id)
                except Exception:
                    self._pending = True
                    raise
        return False

    @staticmethod
    def _printer_id(data: object) -> str | None:
        if not isinstance(data, dict) or not isinstance(data.get("p"), str):
            return None
        try:
            payload = json.loads(data["p"])
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("kind") not in CHANGE_KINDS:
            return None
        printer_id = payload.get("printer_id")
        return printer_id if isinstance(printer_id, str) else None
