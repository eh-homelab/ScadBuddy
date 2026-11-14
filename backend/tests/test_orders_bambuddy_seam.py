"""The orders Bambuddy seam (#4253, spec 2026-10-18 §8, Ruling 7): one file in
``orders/`` reaches the client, and the seam without a URL does nothing."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from scadbuddy.bambuddy.models import CatalogFilament, Spool
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.orders import bambuddy as orders_bambuddy
from scadbuddy.orders.bambuddy import BambuddyIntegrations, spool_groups
from scadbuddy.orders.integrations import NOT_LINKED, LocalDensity, NotLinkedError, choose

ORDERS = Path(orders_bambuddy.__file__).parent
CLIENT = "scadbuddy.bambuddy.client"
#: Never read: choosing a seam builds nothing.
NO_STORE = cast(SettingsStore, object())


def _imports_client(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == CLIENT or alias.name.startswith(CLIENT + ".") for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            if node.module == CLIENT or node.module.startswith(CLIENT + "."):
                return True
            if node.module == "scadbuddy.bambuddy" and any(
                alias.name == "client" for alias in node.names
            ):
                return True
    return False


def test_only_orders_bambuddy_imports_the_client() -> None:
    importers = sorted(
        path.relative_to(ORDERS).as_posix()
        for path in ORDERS.rglob("*.py")
        if _imports_client(path)
    )
    assert importers == ["bambuddy.py"]


def test_the_rule_sees_each_spelling(tmp_path: Path) -> None:
    for text in (
        "import scadbuddy.bambuddy.client\n",
        "from scadbuddy.bambuddy.client import client_for\n",
        "from scadbuddy.bambuddy import client\n",
        "def f():\n    from scadbuddy.bambuddy.client import maybe_client\n",
    ):
        path = tmp_path / "m.py"
        path.write_text(text, encoding="utf-8")
        assert _imports_client(path), text
    path = tmp_path / "ok.py"
    path.write_text("from scadbuddy.bambuddy.farm import inventory_view\n", encoding="utf-8")
    assert not _imports_client(path)


def test_without_a_url_the_seam_is_not_linked() -> None:
    for url in (None, ""):
        seam = choose(url, NO_STORE)
        assert seam is NOT_LINKED
        assert seam.linked is False
    with pytest.raises(NotLinkedError):
        asyncio.run(NOT_LINKED.spool_groups())


def test_with_a_url_the_seam_is_bambuddys() -> None:
    seam = choose("https://bambuddy.test", NO_STORE)
    assert isinstance(seam, BambuddyIntegrations)
    assert seam.linked is True


def _spool(spool_id: int, **fields: Any) -> Spool:
    return Spool.model_validate({"id": spool_id, "material": "PLA", **fields})


def test_spools_group_by_filament_key() -> None:
    groups = spool_groups(
        [
            _spool(3, brand="Bambu", subtype="Basic", rgba="ffffffff", color_name="White"),
            _spool(7, brand="bambu ", subtype="basic", rgba="FFFFFFFF", color_name="Jade White"),
            # Known only by name: never the same colour as the rgba one (#2918).
            _spool(4, brand="Bambu", subtype="Basic", color_name="White"),
            # Archived: not stock.
            _spool(
                9,
                brand="Bambu",
                subtype="Basic",
                rgba="FFFFFFFF",
                archived_at="2026-10-01T00:00:00Z",
            ),
            _spool(5, material="PETG", rgba="00AE42FF", cost_per_kg=20.0),
        ]
    )
    by_ids = {tuple(spool_id for spool_id, _ in g.spools): g for g in groups}
    assert set(by_ids) == {(3, 7), (4,), (5,)}
    white = by_ids[(3, 7)]
    # The most recent spool's names.
    assert (white.brand, white.subtype, white.colour_name, white.colour) == (
        "bambu",
        "basic",
        "Jade White",
        "#FFFFFF",
    )
    assert by_ids[(4,)].colour is None
    assert by_ids[(5,)].spools == [(5, 20.0)]


def test_a_group_carries_the_newest_spools_alpha_stops_and_effect() -> None:
    """#6209: Bambuddy's ``rgba`` alpha, ``extra_colors`` and ``effect_type`` (at
    https://github.com/maziggy/bambuddy/blob/1aa7bce166682fde929fec0d535bdaaa22685aad/backend/app/models/spool.py),
    from the newest spool; a stop or an effect Bambuddy would refuse is dropped."""
    groups = spool_groups(
        [
            _spool(3, rgba="00000000", color_name="Clear", effect_type="translucent"),
            _spool(5, rgba="ff0000", extra_colors="ff0000, #00ff0080,zz,123,0x1234,+12345,12_345"),
            _spool(6, rgba="00ff00ff", effect_type="Dual Color"),
            _spool(7, rgba="0000ffff", effect_type="shiny"),
            _spool(8, rgba="ffffffff", extra_colors=",".join(["abcdef"] * 9)),
        ]
    )
    by_ids = {tuple(spool_id for spool_id, _ in g.spools): g for g in groups}
    clear = by_ids[(3,)]
    assert (clear.colour, clear.alpha, clear.extra_colours, clear.effect) == (
        "#000000",
        0,
        [],
        "translucent",
    )
    red = by_ids[(5,)]
    assert (red.alpha, red.extra_colours, red.effect) == (255, ["#FF0000", "#00FF0080"], None)
    assert by_ids[(6,)].effect == "dual-color"
    assert by_ids[(7,)].effect is None
    assert by_ids[(8,)].extra_colours == ["#ABCDEF"] * 8


def _entry(entry_id: int, kind: str, brand: str, colour: str, density: float) -> CatalogFilament:
    return CatalogFilament(
        id=entry_id,
        name=f"{brand} {kind}",
        type=kind,
        brand=brand,
        color_hex=colour,
        density=density,
    )


def test_a_group_carries_what_gives_its_density() -> None:
    """#6136: the newest spool's preset id, every K-profile's, the tray's, the local
    preset its id names and the catalogue's one density of its material, brand and
    colour; never a name."""
    groups = spool_groups(
        [
            _spool(2, rgba="FFFFFFFF", slicer_filament="7", k_profiles=[{"setting_id": "GFSA05"}]),
            _spool(6, rgba="FFFFFFFF", k_profiles=[{"setting_id": "GFSA00"}, {}]),
            _spool(8, rgba="FFFFFFFF", slicer_filament=" "),
            _spool(5, material="PETG", brand="Insignia", rgba="00AE42FF"),
            _spool(9, material="PETG", brand="Insignia", rgba="000000FF"),
            _spool(11, material="PETG", brand=None, rgba="333333FF"),
        ],
        local={"7": LocalDensity(name="My PLA", density_g_cm3="1.25")},
        trays={5: "GFG99"},
        catalogue=[
            CatalogFilament(
                id=1, name="x", type="petg", brand="insignia", color_hex="#00ae42", density=1.27
            ),
            CatalogFilament(
                id=2, name="y", type="PETG", brand="Insignia", color_hex="#000000", density=1.27
            ),
            CatalogFilament(
                id=3, name="z", type="PETG", brand="Insignia", color_hex="#000000", density=1.3
            ),
            CatalogFilament(
                id=4, name="w", type="PETG", brand=None, color_hex="#333333", density=1.27
            ),
        ],
    )
    by_ids = {tuple(spool_id for spool_id, _ in g.spools): g for g in groups}
    white = by_ids[(2, 6, 8)]
    assert (white.slicer_filament, white.k_profile_settings, white.tray_info_idx) == (
        "7",
        ["GFSA00", "GFSA05"],
        None,
    )
    assert white.local_preset == LocalDensity(name="My PLA", density_g_cm3="1.25")
    green = by_ids[(5,)]
    assert (green.tray_info_idx, green.catalogue_density) == ("GFG99", "1.27")
    # Two catalogue entries of one colour that disagree give none.
    assert by_ids[(9,)].catalogue_density is None
    # A spool with no brand matches no catalogue entry: no brand is not a brand.
    assert by_ids[(11,)].catalogue_density is None


def test_a_catalogue_entry_must_name_the_spools_subtype() -> None:
    """#6148 review: a PLA Basic spool never takes the one PLA Matte entry of its
    colour; an entry whose name holds each word of the subtype does."""
    matte = CatalogFilament(
        id=1, name="Bambu PLA Matte", type="PLA", brand="Bambu", color_hex="#FFFFFF", density=1.32
    )
    basic = CatalogFilament(
        id=2, name="Bambu PLA Basic", type="PLA", brand="Bambu", color_hex="#000000", density=1.26
    )
    groups = spool_groups(
        [
            _spool(1, brand="Bambu", subtype="Basic", rgba="FFFFFFFF"),
            _spool(2, brand="Bambu", subtype="basic", rgba="000000FF"),
            # No subtype: the material, brand and colour are enough.
            _spool(3, brand="Bambu", rgba="FFFFFFFF"),
        ],
        catalogue=[matte, basic],
    )
    by_ids = {tuple(spool_id for spool_id, _ in g.spools): g for g in groups}
    assert by_ids[(1,)].catalogue_density is None
    assert by_ids[(2,)].catalogue_density == "1.26"
    assert by_ids[(3,)].catalogue_density == "1.32"


def test_spools_of_one_record_are_one_group() -> None:
    """#5159 review: the alpha byte and a 6-digit spelling land on one ``#RRGGBB`` record,
    so they are one group, priced once from the newest priced spool."""
    groups = spool_groups(
        [
            _spool(2, rgba="FFFFFFFF", cost_per_kg=20.0),
            _spool(6, rgba="ffffff80", cost_per_kg=30.0),
            _spool(4, rgba="FFFFFF", cost_per_kg=25.0),
        ]
    )
    assert [(g.colour, g.spools) for g in groups] == [
        ("#FFFFFF", [(2, 20.0), (4, 25.0), (6, 30.0)])
    ]
