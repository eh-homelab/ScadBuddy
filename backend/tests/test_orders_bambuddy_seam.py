"""The orders Bambuddy seam (#4253, spec 2026-10-18 §8, Ruling 7): one file in
``orders/`` reaches the client, and the seam without a URL does nothing."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.bambuddy.models import Spool
from scadbuddy.orders import bambuddy as orders_bambuddy
from scadbuddy.orders.bambuddy import BambuddyIntegrations, spool_groups
from scadbuddy.orders.integrations import NOT_LINKED, NotLinkedError, choose

ORDERS = Path(orders_bambuddy.__file__).parent
CLIENT = "scadbuddy.bambuddy.client"


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
        seam = choose(url, None)  # type: ignore[arg-type]
        assert seam is NOT_LINKED
        assert seam.linked is False
    with pytest.raises(NotLinkedError):
        asyncio.run(NOT_LINKED.spool_groups())


def test_with_a_url_the_seam_is_bambuddys() -> None:
    seam = choose("https://bambuddy.test", None)  # type: ignore[arg-type]
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
