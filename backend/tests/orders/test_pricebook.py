"""The price book (#4250, spec 2026-10-18 §5.1, plan Ruling 4): the filament records,
their identity, and the policy read with them in one place."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import SettingsPatch, SettingsStore
from scadbuddy.orders.models import FilamentInput, FilamentPatch, PricingPolicy
from scadbuddy.orders.pricebook import PriceBookReader
from scadbuddy.orders.store import FilamentExistsError, OrderStore
from tests.conftest import UNUSED_TEMPORAL_ADDRESS

pytestmark = pytest.mark.requires_postgres

PLA = FilamentInput(
    material="PLA",
    subtype="Basic",
    brand="Bambu",
    colour_name="Jade White",
    colour="#FFFFFF",
    cost_per_kg="25.50",
    density_g_cm3="1.24",
)


@pytest.fixture
def settings_store(tmp_path: Path, pg_conninfo: str) -> Iterator[SettingsStore]:
    opened = SettingsStore(
        Settings(
            data_dir=tmp_path, database_url=pg_conninfo, temporal_address=UNUSED_TEMPORAL_ADDRESS
        )
    )
    opened.open()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def book(settings_store: SettingsStore, rows: OrderStore) -> PriceBookReader:
    return PriceBookReader(settings_store, rows)


def test_a_filament_round_trips_with_decimals_as_strings(rows: OrderStore) -> None:
    made = rows.insert_filament(PLA)
    assert (made.material, made.subtype, made.brand, made.colour) == (
        "PLA",
        "Basic",
        "Bambu",
        "#FFFFFF",
    )
    # numeric(12,4) and numeric(6,3) read back as the shortest string of the same number.
    assert (made.cost_per_kg, made.density_g_cm3) == ("25.5", "1.24")
    assert rows.filament(made.id) == made

    edited = rows.update_filament(made.id, FilamentPatch(cost_per_kg="30", density_g_cm3=None))
    assert edited is not None
    assert (edited.cost_per_kg, edited.density_g_cm3, edited.colour_name) == (
        "30",
        None,
        "Jade White",
    )
    assert rows.update_filament("nobody", FilamentPatch(cost_per_kg="1")) is None


def test_a_cost_finer_than_its_column_is_refused() -> None:
    with pytest.raises(ValueError, match="decimal places"):
        FilamentInput(material="PLA", colour="#000000", cost_per_kg="1.00001")
    with pytest.raises(ValueError, match="at least 0"):
        FilamentInput(material="PLA", colour="#000000", cost_per_kg="-1")
    with pytest.raises(ValueError, match="more than 0"):
        FilamentInput(material="PLA", colour="#000000", cost_per_kg="1", density_g_cm3="0")
    with pytest.raises(ValueError, match="not a decimal"):
        FilamentInput(material="PLA", colour="#000000", cost_per_kg="1.5e")


def test_a_duplicate_identity_is_409(rows: OrderStore) -> None:
    """The identity is material, subtype, brand and colour, in any case (Ruling 4)."""
    made = rows.insert_filament(PLA)
    again = PLA.model_copy(
        update={"material": "pla", "subtype": "BASIC", "brand": "bambu", "colour": "#ffffff"}
    )
    with pytest.raises(FilamentExistsError) as raised:
        rows.insert_filament(again)
    assert raised.value.existing_id == made.id
    # No subtype and no brand are an identity too: coalesced, so two such never both pass.
    plain = FilamentInput(material="PETG", colour="#000000", cost_per_kg="20")
    rows.insert_filament(plain)
    with pytest.raises(FilamentExistsError):
        rows.insert_filament(plain)
    # An edit onto another record's identity is refused the same way.
    other = rows.insert_filament(PLA.model_copy(update={"colour": "#000000"}))
    with pytest.raises(FilamentExistsError) as raised:
        rows.update_filament(other.id, FilamentPatch(colour="#FFFFFF"))
    assert raised.value.existing_id == made.id


def test_archiving_hides_a_filament_from_the_book(rows: OrderStore, book: PriceBookReader) -> None:
    kept = rows.insert_filament(PLA)
    gone = rows.insert_filament(PLA.model_copy(update={"colour": "#000000"}))
    archived = rows.archive_filament(gone.id)
    assert archived is not None and archived.archived_at is not None
    assert [f.id for f in book.snapshot().filaments] == [kept.id]
    assert {f.id for f in rows.filaments(include_archived=True)} == {kept.id, gone.id}
    # Still readable by id: an item may name it.
    assert rows.filament(gone.id) is not None
    assert rows.archive_filament("nobody") is None


def test_an_archived_filament_frees_its_identity(rows: OrderStore, book: PriceBookReader) -> None:
    """The identity index covers the records in the book only: archive "PLA / Basic /
    Bambu / #FFFFFF" and the same filament can be added again, and the book shows the
    new record."""
    first = rows.insert_filament(PLA)
    rows.archive_filament(first.id)
    second = rows.insert_filament(PLA)
    assert second.id != first.id
    assert [f.id for f in book.snapshot().filaments] == [second.id]
    # Only the record in the book holds the identity against a third.
    with pytest.raises(FilamentExistsError) as raised:
        rows.insert_filament(PLA)
    assert raised.value.existing_id == second.id


def test_an_archived_filament_is_unarchived(rows: OrderStore, book: PriceBookReader) -> None:
    """#4977: the same record back in the book, its id (and so its quotes and items)
    kept; a record never archived comes back as it is."""
    made = rows.insert_filament(PLA)
    rows.archive_filament(made.id)
    back = rows.unarchive_filament(made.id)
    assert back is not None and back.id == made.id and back.archived_at is None
    assert [f.id for f in book.snapshot().filaments] == [made.id]
    again = rows.unarchive_filament(made.id)
    assert again is not None and again.archived_at is None
    assert rows.unarchive_filament("nobody") is None


def test_an_unarchive_refuses_an_identity_the_book_holds(rows: OrderStore) -> None:
    """Archived, then the same filament added again: the old record cannot come back
    beside the new one, and the refusal names the record in the book."""
    first = rows.insert_filament(PLA)
    rows.archive_filament(first.id)
    second = rows.insert_filament(PLA)
    with pytest.raises(FilamentExistsError) as raised:
        rows.unarchive_filament(first.id)
    assert raised.value.existing_id == second.id
    still = rows.filament(first.id)
    assert still is not None and still.archived_at is not None


def test_the_price_book_reads_policy_and_filaments_together(
    rows: OrderStore, book: PriceBookReader, settings_store: SettingsStore
) -> None:
    empty = book.snapshot()
    assert (empty.currency, empty.minor_exponent, empty.rough_estimate_factor) == (
        "USD",
        2,
        "1.15",
    )
    assert empty.policy == PricingPolicy()
    assert empty.filaments == []

    made = rows.insert_filament(PLA)
    settings_store.save(
        SettingsPatch(
            orders_currency="JPY",
            pricing_machine_rate_per_hour="120",
            pricing_setup_fee="50",
            pricing_labour_per_item="10.5",
            pricing_markup_percent="15",
            pricing_rounding="to_major",
            rough_estimate_factor="1.3",
        )
    )
    now = book.snapshot()
    assert (now.currency, now.minor_exponent, now.rough_estimate_factor) == ("JPY", 0, "1.3")
    assert now.policy == PricingPolicy(
        machine_rate_per_hour="120",
        setup_fee="50",
        labour_per_item="10.5",
        markup_percent="15",
        rounding="to_major",
    )
    assert [f.id for f in now.filaments] == [made.id]
