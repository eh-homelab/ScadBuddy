"""The orders Bambuddy seam (#4253, spec 2026-10-18 §8, Ruling 7): one file in
``orders/`` reaches the client, and the seam without a URL does nothing."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import cast

import pytest

from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.orders import bambuddy as orders_bambuddy
from scadbuddy.orders.bambuddy import BambuddyIntegrations
from scadbuddy.orders.integrations import NOT_LINKED, NotLinkedError, choose

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
    path.write_text("from scadbuddy.bambuddy.farm import ArchiveOutcome\n", encoding="utf-8")
    assert not _imports_client(path)


def test_without_a_url_the_seam_is_not_linked() -> None:
    for url in (None, ""):
        seam = choose(url, NO_STORE)
        assert seam is NOT_LINKED
        assert seam.linked is False
    with pytest.raises(NotLinkedError):
        asyncio.run(NOT_LINKED.local_densities())


def test_with_a_url_the_seam_is_bambuddys() -> None:
    seam = choose("https://bambuddy.test", NO_STORE)
    assert isinstance(seam, BambuddyIntegrations)
    assert seam.linked is True
