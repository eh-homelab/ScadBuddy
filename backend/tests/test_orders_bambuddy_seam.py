"""The orders Bambuddy seam (#4253, spec 2026-10-18 §8, Ruling 7): one file in
``orders/`` reaches the client, and the seam without a URL does nothing."""

from __future__ import annotations

import ast
import asyncio
import json
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from scadbuddy.bambuddy.models import LocalPresetDetail, Spool
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.orders import bambuddy as orders_bambuddy
from scadbuddy.orders.bambuddy import (
    BambuddyIntegrations,
    density_text,
    preset_densities,
    spool_groups,
)
from scadbuddy.orders.integrations import NOT_LINKED, NotLinkedError, choose
from scadbuddy.slicing.profiles import Profiles

ORDERS = Path(orders_bambuddy.__file__).parent
CLIENT = "scadbuddy.bambuddy.client"
#: Never read: choosing a seam builds nothing.
NO_STORE = cast(SettingsStore, object())
#: The slicing tests' vendor tree (``tests/slicing/conftest.py``).
SLICING_PROFILES = Path(__file__).parent / "slicing" / "profiles"


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


def test_a_groups_preset_is_its_newest_spools() -> None:
    """#6168: the density comes from one preset per group, the newest spool's."""
    groups = spool_groups(
        [
            _spool(2, rgba="FFFFFFFF", slicer_filament="GFA00"),
            _spool(6, rgba="FFFFFFFF", slicer_filament="7"),
            _spool(4, rgba="FFFFFFFF", slicer_filament="GFG00"),
            _spool(5, rgba="000000FF"),
        ]
    )
    assert [(g.colour, g.preset, g.density_g_cm3) for g in groups] == [
        ("#000000", None, None),
        ("#FFFFFF", "7", None),
    ]


@pytest.mark.parametrize(
    ("value", "density"),
    [
        (["1.24"], "1.24"),
        (["1.24", "1.30"], "1.24"),
        ("1.275", "1.275"),
        ("1.2000", "1.2"),
        ("1.2345", "1.235"),
        (1.25, "1.25"),
        ([], None),
        (None, None),
        ("", None),
        ("nope", None),
        ("0", None),
        ("-1.2", None),
        ("1000", None),
        ("NaN", None),
        ({"a": 1}, None),
    ],
)
def test_a_presets_density_as_a_record_holds_it(value: object, density: str | None) -> None:
    assert density_text(value) == density


class _Presets:
    """The client's ``local_preset``, from a table; anything else is an ApiError."""

    def __init__(self, settings: dict[int, dict[str, Any]], preset_type: str = "filament"):
        self.settings = settings
        self.preset_type = preset_type
        self.asked: list[int] = []

    async def local_preset(self, preset_id: int) -> LocalPresetDetail:
        self.asked.append(preset_id)
        if preset_id not in self.settings:
            raise ApiError(404, f"no local preset {preset_id}")
        return LocalPresetDetail.model_validate(
            {
                "id": preset_id,
                "name": f"Preset {preset_id}",
                "preset_type": self.preset_type,
                "source": "orcaslicer",
                "setting": self.settings[preset_id],
            }
        )


def _tree_with_density(tmp_path: Path) -> Profiles:
    """The slicing tests' tree, its ``Bambu PLA Basic @base`` given a density."""
    root = tmp_path / "profiles"
    shutil.copytree(SLICING_PROFILES, root)
    base = root / "BBL" / "filament" / "Bambu PLA Basic @base.json"
    doc = json.loads(base.read_text(encoding="utf-8"))
    doc["filament_density"] = ["1.26"]
    base.write_text(json.dumps(doc), encoding="utf-8")
    return Profiles(root)


async def test_densities_read_each_preset_once(tmp_path: Path) -> None:
    """A local preset (all digits) from Bambuddy, any other id from the image's tree by
    ``setting_id`` or ``filament_id``; one read per distinct id."""
    client = _Presets({2: {"filament_density": ["1.24"]}, 3: {"filament_type": ["PLA"]}})
    profiles = _tree_with_density(tmp_path)

    found = await preset_densities(
        cast(Any, client),
        ["2", "2", "3", "4", "GFA00", "GFSA00", "GF_PLA", "GFNOPE"],
        lambda: profiles,
    )

    assert found == {"2": "1.24", "GFA00": "1.26", "GFSA00": "1.26"}
    assert sorted(client.asked) == [2, 3, 4]


async def test_without_the_tree_only_local_presets_are_read() -> None:
    client = _Presets({2: {"filament_density": "1.24"}})
    assert await preset_densities(cast(Any, client), ["2", "GFA00"], lambda: None) == {"2": "1.24"}
    assert await preset_densities(cast(Any, client), ["2", "GFA00"], None) == {"2": "1.24"}


async def test_a_local_preset_of_another_slot_gives_no_density() -> None:
    client = _Presets({2: {"filament_density": "1.24"}}, preset_type="process")
    assert await preset_densities(cast(Any, client), ["2"], None) == {}


async def test_a_broken_tree_gives_no_density(tmp_path: Path) -> None:
    root = tmp_path / "broken"
    shutil.copytree(SLICING_PROFILES, root)
    (root / "BBL" / "filament" / "Bambu PLA Basic @base.json").write_text("{", encoding="utf-8")
    profiles = Profiles(root)
    assert await preset_densities(cast(Any, _Presets({})), ["GFA00"], lambda: profiles) == {}
