"""`SLICING`: the native slicer's API side (plan 2026-10-16-native-slicer-rust-worker
Task 11). Building it does no I/O, as `PRINT_LIBRARY`'s: the content store is set on the
core only after every component is built (`main.py` `lifespan`), so the transfers are
built on first use."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cached_property
from typing import Annotated

from temporalio.client import Client

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.library.settings_store import settings_kek
from scadbuddy.slicing.catalogue import slices_native, warm_catalogue
from scadbuddy.slicing.held_presets import HeldPresetStore
from scadbuddy.slicing.profiles import Profiles
from scadbuddy.slicing.start import NativeSliceStarter, native_slice_starter
from scadbuddy.slicing.store import NativeSliceStore
from scadbuddy.slicing.transfer import TransferPuts, Transfers
from scadbuddy.slicing.transfer_token import transfer_key
from scadbuddy.store.refs import BlobRefs

#: Why `transfers` fails on the local store (Ruling: native slicing needs a ContentStore).
NO_CONTENT_STORE = (
    "the native slicer keeps its slices in the blob store: set store_backend to s3 or"
    " bambuddy first"
)


class Slicing:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.store = NativeSliceStore(core.settings_store.pool)
        self.puts = TransferPuts(core.projection.pool)
        self.refs = BlobRefs(core.projection.pool)
        self._transfers: Transfers | None = None

    @cached_property
    def held(self) -> HeldPresetStore:
        """The kept copy of Bambuddy's local presets (#4645)."""
        return HeldPresetStore(self.core.projection.pool)

    @cached_property
    def profiles(self) -> Profiles | None:
        """The image's tree, with the kept copy of Bambuddy's local presets (#4645)."""
        return Profiles.installed(self.core.settings.slicer_profiles_dir, held=self.held)

    @cached_property
    def starter(self) -> NativeSliceStarter | None:
        return native_slice_starter(self.core.settings, self.profiles)

    def transfers(self) -> Transfers:
        """`RuntimeError` without a content store (``store_backend`` local)."""
        if self._transfers is None:
            content = self.core.store.content
            if content is None:
                raise RuntimeError(NO_CONTENT_STORE)
            kek = settings_kek(self.core.settings.secret_key_file)
            self._transfers = Transfers(
                content,
                self.puts,
                internal_base_url=self.core.settings.api_internal_url,
                secret=None if kek is None else transfer_key(kek),
            )
        return self._transfers

    def temporal(self) -> Client:
        """The API's client, the one print runs start on."""
        return self.core.print_runs.client


SLICING: Key[Slicing] = Key("slicing")


def _build(core: Core, components: Components) -> Slicing:
    return Slicing(core)


@asynccontextmanager
async def _run(slicing: Slicing) -> AsyncIterator[None]:
    """Build the image's preset catalogue in the background while the app runs, so the
    first native print dialog does not wait on it (#3818 tier A); the start never does.
    Only with ``slicer_backend`` ``native`` at start (#4348): a later switch builds it on
    the first dialog that needs it.

    With the spool providers' refresh off (``spool_overlay_refresh_seconds`` 0, which
    applies at a start) nothing keeps the copy of Bambuddy's local presets current, so it
    is forgotten here (#4645 review): a ``local`` preset is read from Bambuddy on every
    slice again rather than from a copy that only gets older."""
    if slicing.core.settings.spool_overlay_refresh_seconds == 0:
        await slicing.held.clear()
    warming = warm_catalogue(
        slicing.profiles,
        slices_native(slicing.core.settings_store),
    )
    try:
        yield
    finally:
        if warming is not None:
            warming.cancel()
            await asyncio.gather(warming, return_exceptions=True)


COMPONENT = Component(SLICING, build=_build, run=_run)

SlicingDep = Annotated[Slicing, component_dep(SLICING)]
