"""ScadBuddy's own spool inventory, the native spool provider (#4643, tier B): its records
in ``native_spools``, and the provider that the refresh copies them through."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

import scadbuddy
from scadbuddy.core.components import Components, feature_exports
from scadbuddy.library.settings_store import SettingsStore, StoredSettings
from scadbuddy.printers.models import PrinterRow
from scadbuddy.spools.activities import refresh_provider
from scadbuddy.spools.component import Spools
from scadbuddy.spools.models import SeenTray
from scadbuddy.spools.native import (
    NativeSpoolCreate,
    NativeSpoolExistsError,
    NativeSpoolPatch,
    NativeSpoolStore,
)
from scadbuddy.spools.providers import (
    PROVIDER_ATTR,
    PROVIDER_MODULE,
    SettingsOnly,
    discover_providers,
)
from scadbuddy.spools.spool_providers import SPOOL_PROVIDERS, NativeProvider, native_provider
from scadbuddy.spools.store import SpoolStore
from tests.conftest import PgPool

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
UUID_A = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
PRINTER = uuid.UUID("5d0c8f1e-0000-4000-8000-000000000643")


def _core() -> SettingsOnly:
    """A worker's core: the provider is built over the settings store's pool, which it
    does not open until it reads."""
    return SettingsOnly(cast(SettingsStore, SimpleNamespace(pool=None, load=StoredSettings)))


def test_the_native_provider_is_discovered_ahead_of_bambuddy() -> None:
    assert SPOOL_PROVIDERS in feature_exports(PROVIDER_MODULE, PROVIDER_ATTR, scadbuddy)
    names = [provider.name for provider in discover_providers(_core(), None)]
    assert names[:2] == ["native", "bambuddy"]


def test_a_create_cleans_the_tags_and_refuses_what_no_spool_reports() -> None:
    spool = NativeSpoolCreate(tray_uuid="a1b2c3d4-e5f6-0718-293a-4b5c6d7e8f90", tag_uid="0" * 16)
    assert (spool.tray_uuid, spool.tag_uid) == (UUID_A, None)
    with pytest.raises(ValidationError):
        NativeSpoolCreate(tray_uuid="not a tag")
    with pytest.raises(ValidationError):
        NativeSpoolCreate(colour="red")
    with pytest.raises(ValidationError):
        NativeSpoolCreate(remaining_g=-1)
    assert NativeSpoolCreate(colour="#ff8800").colour == "#FF8800"


pg = pytest.mark.requires_postgres


@pytest.fixture
def native(pg_pool: PgPool) -> NativeSpoolStore:
    return NativeSpoolStore(pg_pool)


@pg
def test_a_spool_is_kept_listed_patched_and_archived(native: NativeSpoolStore) -> None:
    made = native.create(
        NativeSpoolCreate(
            material="PLA",
            colour="#FF0000",
            brand="Bambu",
            name="Red",
            label_weight_g=1000,
            remaining_g=812.5,
        )
    )
    assert native.get(made.id) == made
    assert [s.id for s in native.list()] == [made.id]

    patched = native.update(made.id, NativeSpoolPatch(remaining_g=700, brand=None))
    assert patched is not None
    assert (patched.remaining_g, patched.brand, patched.name) == (700, None, "Red")
    assert patched.updated_at >= made.updated_at

    archived = native.update(made.id, NativeSpoolPatch(archived=True))
    assert archived is not None and archived.archived_at is not None
    assert native.list() == []
    assert [s.id for s in native.list(include_archived=True)] == [made.id]
    restored = native.update(made.id, NativeSpoolPatch(archived=False))
    assert restored is not None and restored.archived_at is None

    assert native.delete(made.id) is True
    assert native.get(made.id) is None
    assert native.delete(made.id) is False
    assert native.update(made.id, NativeSpoolPatch(name="x")) is None


@pg
def test_one_unarchived_spool_per_tag(native: NativeSpoolStore) -> None:
    first = native.create(NativeSpoolCreate(tray_uuid=UUID_A))
    with pytest.raises(NativeSpoolExistsError):
        native.create(NativeSpoolCreate(tray_uuid=UUID_A))
    other = native.create(NativeSpoolCreate(material="PETG"))
    with pytest.raises(NativeSpoolExistsError):
        native.update(other.id, NativeSpoolPatch(tray_uuid=UUID_A))
    # A spool used up and archived lets its tag go to the next record of it.
    native.update(first.id, NativeSpoolPatch(archived=True))
    native.create(NativeSpoolCreate(tray_uuid=UUID_A))
    with pytest.raises(NativeSpoolExistsError):
        native.update(first.id, NativeSpoolPatch(archived=False))


@pg
async def test_the_refresh_lays_native_records_over_their_tray(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        conn.execute("INSERT INTO printers (id, serial, name) VALUES (%s, 'T643', 'P')", (PRINTER,))
    provider = NativeProvider(NativeSpoolStore(pg_pool))
    assert await provider.configured() is False
    spools = Spools(store=SpoolStore(pg_pool), providers=[provider])
    assert await refresh_provider(spools, "native") == "skipped"

    made = provider.store.create(
        NativeSpoolCreate(
            material="PLA",
            colour="#FF0000",
            remaining_g=400,
            label_weight_g=1000,
            tray_uuid=UUID_A,
            preset="GFA00",
        )
    )
    provider.store.create(NativeSpoolCreate(material="PETG", colour="#00FF00", remaining_g=900))
    gone = provider.store.create(NativeSpoolCreate(material="ABS"))
    provider.store.update(gone.id, NativeSpoolPatch(archived=True))
    assert await provider.configured() is True
    assert native_provider(spools) is provider
    assert await refresh_provider(spools, "native", now=lambda: NOW) == "refreshed"

    tagged = SeenTray(ams_id=0, tray_id=0, tray_uuid=UUID_A, material="PLA", colour="#FF0000")
    untagged = SeenTray(ams_id=0, tray_id=1, material="PETG", colour="#00FF00")
    overlay = await spools.store.overlay(PRINTER, [tagged, untagged], now=NOW, providers=["native"])
    only, unknown = overlay.trays
    assert unknown.records == {}
    record = only.records["native"]
    assert record.record_id == str(made.id)
    assert (record.remaining_g, record.label_weight_g, record.preset) == (400, 1000, "GFA00")
    assert [shelf.records["native"].material for shelf in overlay.shelf] == ["PETG"]
    assert overlay.stale == {}


def test_a_worker_with_no_components_still_finds_it() -> None:
    (build,) = SPOOL_PROVIDERS
    built = build(_core(), cast(Components, None))
    assert built is not None
    row = cast(PrinterRow, SimpleNamespace(id=str(PRINTER), bambuddy_printer_id=1))
    assert (built.name, built.label, built.printer_key(row)) == ("native", "ScadBuddy", None)
