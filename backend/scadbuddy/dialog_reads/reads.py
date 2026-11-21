"""The print dialog's Bambuddy reads, kept for a few seconds (#4196 lever 4).

The dialog's ``choices`` read and the check that follows it a moment later read the same
printer list, printer row, library file and its plates. Each of those reads is kept for
`DIALOG_TTL` seconds, by Bambuddy's base URL and the API key's scope, so the check uses
what ``choices`` read. Only reads that cannot go stale in a way that harms a print are
kept: a printer's name, model and nozzle count, and a library file's metadata and
plates. The printer's status, its AMS trays, its inventory and the spool assignments are
always read again, and so is the file's download. A check only judges: the run reads
everything again, with a client that keeps nothing. A failed read is not kept. Two
callers that miss one key at once share one read.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from pydantic import BaseModel

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.models import LibraryFile, LibraryPlates, Printer
from scadbuddy.library.settings_store import StoredSettings

#: Long enough for the check the dialog sends once ``choices`` answered, short enough
#: that a printer or file changed in Bambuddy shows in the next dialog.
DIALOG_TTL = 5.0
#: A few dialogs on a few Bambuddys: past it the oldest read is dropped.
MAX_ENTRIES = 256

type _Key = tuple[str, str, str, int]
type _Value = BaseModel | list[Printer]


class DialogReads:
    """The kept reads. ``clock`` is injected so a test moves time by hand."""

    def __init__(
        self,
        ttl: float = DIALOG_TTL,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._max_entries = max_entries
        self._entries: dict[_Key, tuple[float, _Value]] = {}
        #: The read under way for each key, and what it ended with: the answer, the
        #: error it raised, or ``None`` when its own caller was cancelled.
        self._reading: dict[_Key, asyncio.Future[_Value | Exception | None]] = {}

    async def get[T: BaseModel | list[Printer]](
        self, config: BambuddyConfig, kind: str, item: int, read: Callable[[], Awaitable[T]]
    ) -> T:
        """What ``read`` answered for this key less than the TTL ago, else a new read,
        kept if it succeeds. A caller that misses while another reads the key waits for
        that read (#6618) and gets its answer or its error; when that read's own caller
        was cancelled, the waiters share one new read (#6622). Each caller gets its own
        copy, so one caller's change to an answer never reaches another's."""
        key = (config.base_url, _scope(config.api_key), kind, item)
        hit = self._entries.get(key)
        if hit is not None and hit[0] > self._clock():
            return _copy(hit[1])  # type: ignore[return-value]
        while (under_way := self._reading.get(key)) is not None:
            # The shield keeps a waiter's cancel from cancelling the shared read.
            shared = await asyncio.shield(under_way)
            if isinstance(shared, Exception):
                # The same Bambuddy and key: a read of its own would fail the same way.
                own = _own_copy(shared)
                if own is None:
                    # This waiter shares the error object, and so its traceback, with
                    # the others (#6635): the case #6627 fixes only for an error that
                    # can be rebuilt.
                    raise shared
                raise own from shared
            if shared is not None:
                return _copy(shared)  # type: ignore[return-value]
        reading: asyncio.Future[_Value | Exception | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._reading[key] = reading
        try:
            value = await read()
        except Exception as error:
            reading.set_result(error)
            raise
        except BaseException:
            reading.set_result(None)
            raise
        else:
            self._put(key, value)
            reading.set_result(value)
        finally:
            if self._reading.get(key) is reading:
                del self._reading[key]
        return _copy(value)

    def _put(self, key: _Key, value: _Value) -> None:
        now = self._clock()
        self._entries.pop(key, None)
        if len(self._entries) >= self._max_entries:
            for stale in [k for k, (expires, _) in self._entries.items() if expires <= now]:
                del self._entries[stale]
        while len(self._entries) >= self._max_entries:
            # Oldest first: a dict keeps insertion order, and a new read re-inserts.
            del self._entries[next(iter(self._entries))]
        self._entries[key] = (now + self._ttl, _copy(value))


def _scope(api_key: str | None) -> str:
    """The key's scope as a digest: two keys never share a read, and the key itself is
    not kept a second time."""
    return hashlib.sha256((api_key or "").encode()).hexdigest()


def _own_copy(error: Exception) -> Exception | None:
    """A new ``error`` of the same class and args, with a shallow copy of each field, for
    one waiter to raise: a shared object would collect every waiter's frames in one
    traceback (#6627), and a shared field would carry one waiter's change to the others.
    ``copy.copy`` cannot do it, because it calls ``__init__`` with ``args`` alone, which
    `ApiError` refuses. ``None`` when the class cannot be rebuilt this way (a pydantic
    ``ValidationError``'s ``__new__`` needs more than its args, and it has no
    ``__dict__``): the waiter then raises the shared error itself."""
    try:
        own = type(error).__new__(type(error), *error.args)
        own.args = error.args
        own.__dict__.update({name: copy.copy(field) for name, field in vars(error).items()})
    except Exception:
        return None
    return own


def _copy[T: BaseModel | list[Printer]](value: T) -> T:
    if isinstance(value, list):
        return [row.model_copy(deep=True) for row in value]  # type: ignore[return-value]
    return value.model_copy(deep=True)


class DialogClient(BambuddyClient):
    """A client whose printer and library file reads go through `DialogReads`. Every
    other read and every write is the plain client's."""

    def __init__(
        self, config: BambuddyConfig, reads: DialogReads, http: httpx.AsyncClient | None = None
    ) -> None:
        super().__init__(config, http)
        self._reads = reads

    async def printers(self) -> list[Printer]:
        return await self._reads.get(self.config, "printers", 0, super().printers)

    async def printer(self, printer_id: int) -> Printer:
        return await self._reads.get(
            self.config,
            "printer",
            printer_id,
            lambda: super(DialogClient, self).printer(printer_id),
        )

    async def library_file(self, file_id: int) -> LibraryFile:
        return await self._reads.get(
            self.config,
            "library_file",
            file_id,
            lambda: super(DialogClient, self).library_file(file_id),
        )

    async def library_plates(self, file_id: int) -> LibraryPlates:
        return await self._reads.get(
            self.config,
            "library_plates",
            file_id,
            lambda: super(DialogClient, self).library_plates(file_id),
        )


@asynccontextmanager
async def dialog_client(
    settings: StoredSettings, reads: DialogReads
) -> AsyncIterator[BambuddyClient]:
    """As `client_for`, with the dialog's kept reads."""
    async with DialogClient(BambuddyConfig.from_settings(settings), reads) as client:
        yield client


@asynccontextmanager
async def maybe_dialog_client(
    settings: StoredSettings, reads: DialogReads, *, native: bool
) -> AsyncIterator[BambuddyClient | None]:
    """As `maybe_client`, with the dialog's kept reads: ``None`` on a native run."""
    if native:
        yield None
        return
    async with dialog_client(settings, reads) as client:
        yield client
