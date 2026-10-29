"""The tool-access rules (#2171, spec 2026-10-18 §4.1 to §4.3) as the agent's vectors pin them:
agent/test/accessVectors.test.ts writes the file, and this applies the same rules in Python."""

from __future__ import annotations

import dataclasses
import json
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from scadbuddy_durable.session import access, tools
from scadbuddy_durable.session.access import AccessRow

VECTORS: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[2] / "agent" / "test" / "fixtures" / "access-vectors.json"
    ).read_text(encoding="utf-8")
)


def _rows(raw: list[dict[str, str]]) -> list[AccessRow]:
    return [AccessRow(r["source"], r["item"], r["access"], r["updated_by"]) for r in raw]


@pytest.mark.parametrize("vector", VECTORS["registry"], ids=lambda v: v["name"])
def test_every_registry_vector_matches(vector: dict[str, Any]) -> None:
    entries = [
        tools.ManifestEntry(t["name"], t["name"], {"type": "object"}, t["risk"], t["hitl"])
        for t in vector["tools"]
    ]
    tiers, disabled = tools.sanitise_overrides(entries, _rows(vector["rows"]))
    assert {"tool_tiers": tiers, "disabled_tools": disabled} == vector["expect"]


@pytest.mark.parametrize("vector", VECTORS["sources"], ids=lambda v: v["name"])
def test_every_source_vector_matches(vector: dict[str, Any]) -> None:
    assert access.source_enabled(vector["source"], _rows(vector["rows"])) is vector["expect"]


@pytest.mark.parametrize(
    "vector", VECTORS["item_kinds"], ids=lambda v: f"{v['source']} {v['item']}"
)
def test_every_item_kind_vector_matches(vector: dict[str, str]) -> None:
    assert access.item_kind_in(vector["source"], vector["item"]) == vector["expect"]


def test_registry_sources_match() -> None:
    assert list(access.TOOL_ACCESS) == VECTORS["tool_access"]
    assert VECTORS["migrated_by"] == access.MIGRATED_BY
    for tool, source in VECTORS["registry_source"].items():
        assert access.registry_source(tool) == source


def _plugins(raw: list[dict[str, Any]]) -> list[access.CallPlugin]:
    return [
        access.CallPlugin(
            p["name"],
            p["source"],
            tuple(p["items"]),
            {k: tuple(v) for k, v in p["aliases"].items()},
        )
        for p in raw
    ]


@pytest.mark.parametrize("vector", VECTORS["calls"], ids=lambda v: v["name"])
def test_calls(vector: dict[str, Any]) -> None:
    """agent/src/access/items.ts offCallItem and offItemReason, the same answer and words."""
    off = access.off_call_item(
        vector["tool"], vector["input"], _plugins(vector["plugins"]), _rows(vector["rows"])
    )
    got = (
        None if off is None else {**dataclasses.asdict(off), "reason": access.off_item_reason(off)}
    )
    assert got == vector["expect"]


def _cn_ranges() -> list[list[int]]:
    out: list[list[int]] = []
    start = -1
    for cp in range(0x110001):
        cn = cp <= 0x10FFFF and not 0xD800 <= cp <= 0xDFFF and unicodedata.category(chr(cp)) == "Cn"
        if cn and start < 0:
            start = cp
        if not cn and start >= 0:
            out.append([start, cp - 1])
            start = -1
    return out


def test_unassigned_here_is_unassigned_in_node_or_in_its_doubt_table() -> None:
    """Task 6 re-review R6: the agent's UNICODE_DOUBT is exactly what this Python leaves
    unassigned and Node has assigned, so the call rule's unsure answer is one in both. A
    Python or Node bump that moves either fails here until the table is regenerated."""
    unicode = VECTORS["unicode"]

    def points(ranges: list[list[int]]) -> set[int]:
        return {cp for a, b in ranges for cp in range(a, b + 1)}

    node, doubt = points(unicode["cn"]), points(unicode["doubt"])
    here = points(_cn_ranges())
    assert node <= here, f"assigned here, unassigned in Node {unicode['node']}"
    assert here - node == doubt
