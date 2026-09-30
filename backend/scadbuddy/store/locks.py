"""One asyncio lock per key, so the unpacks of one key in a process run once: the
pieces of a template's first render all `prepare` at the same revision at once, and
a second unpack would swap the export out from under a render reading it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class KeyLocks:
    def __init__(self) -> None:
        # Never pruned: one small lock per snapshot or font key this process unpacked.
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        async with self._locks.setdefault(key, asyncio.Lock()):
            yield
