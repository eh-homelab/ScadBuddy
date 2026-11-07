"""#5298: the print activities read the settings off the event loop. On the print worker a
read is a blocking HTTP call (`crud/print.py` `RemoteSettingsStore`), which may retry for
seconds while the API rolls; on the loop it would stall every heartbeat and poller."""

from __future__ import annotations

import asyncio
import threading
from typing import cast

from scadbuddy.library.settings_store import SettingsReader, StoredSettings
from scadbuddy.workflows.print_activities import PrintActivities, PrintDeps


class SlowReader:
    """A reader whose `load` blocks until it is released."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def load(self, timeout: float | None = None) -> StoredSettings:
        assert self.release.wait(10), "never released"
        return StoredSettings(bambuddy_url="http://bambuddy.test")

    def plate_clear_required(self) -> bool:
        return False


async def test_a_slow_settings_read_does_not_block_the_loop() -> None:
    reader = SlowReader()
    deps = PrintDeps(
        settings_store=cast(SettingsReader, reader),
        outputs=None,  # type: ignore[arg-type]
        prints=None,
        uploads=None,  # type: ignore[arg-type]
        store=None,  # type: ignore[arg-type]
        observer=None,  # type: ignore[arg-type]
    )
    reading = asyncio.create_task(PrintActivities(deps)._settings())
    ticks = 0
    for _ in range(5):
        # The loop still runs other coroutines while the read waits.
        await asyncio.sleep(0.01)
        ticks += 1
    assert ticks == 5 and not reading.done()
    reader.release.set()
    assert (await reading).bambuddy_url == "http://bambuddy.test"
