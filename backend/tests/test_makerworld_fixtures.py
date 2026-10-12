"""Real MakerWorld 3MFs from the user's Bambuddy library (#2146): single and several
colours, by part and by object, painted and not, negative and modifier parts, support
blockers, 200 objects, and files to 98 MB. `test-fixtures/makerworld/manifest.json`
says what each covers and records what Arrange's reader (`read_objects`) makes of it,
so a change to the reader shows up here as a changed expectation. The files are Git
LFS; without them these tests skip (`requires_fixtures`)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from scadbuddy.render.objects3mf import UnreadableObjectsError, read_objects
from scadbuddy.render.read_budget import ReadBudget
from tests.support.fixtures import makerworld, makerworld_path

FIXTURES = makerworld()


def ids(entry: dict[str, Any]) -> str:
    return str(entry["file"]).removesuffix(".3mf")


@pytest.mark.requires_fixtures
@pytest.mark.parametrize("entry", FIXTURES, ids=ids)
def test_a_fixture_is_the_file_its_manifest_names(entry: dict[str, Any]) -> None:
    data = makerworld_path(entry["file"]).read_bytes()
    assert len(data) == entry["bytes"]
    assert hashlib.sha256(data).hexdigest() == entry["sha256"]


@pytest.mark.requires_fixtures
@pytest.mark.parametrize("entry", FIXTURES, ids=ids)
def test_arranges_reader_reads_each_fixture_as_recorded(entry: dict[str, Any]) -> None:
    expected = entry["arrange_reads"]
    data = makerworld_path(entry["file"]).read_bytes()
    if "refused" in expected:
        with pytest.raises(UnreadableObjectsError) as refused:
            read_objects(data, ReadBudget())
        assert str(refused.value) == expected["refused"]
        return
    objects = read_objects(data, ReadBudget())
    assert {
        "objects": len(objects),
        "copies": sum(o.count for o in objects),
        "colours": sorted({p.colour for o in objects for p in o.parts}),
        "painted_parts": sum(p.paint is not None for o in objects for p in o.parts),
        "negative_parts": sum(p.negative for o in objects for p in o.parts),
        "notes": sorted({n for o in objects for n in o.notes})[:5],
    } == expected


def test_the_fixtures_cover_every_kind_of_file() -> None:
    """The matrix the fixtures exist for: if one is dropped, its kind must be covered
    by another. Only the manifest is read (not LFS), so this runs on any checkout."""
    if not FIXTURES:
        pytest.skip("test-fixtures/makerworld/manifest.json is not here")

    def colours(e: dict[str, Any]) -> int:
        return len(e["extruders"])

    kinds = {
        "one colour": any(colours(e) == 1 and not e["painted_triangles"] for e in FIXTURES),
        "colours by part": any(e["parts"] > e["objects"] and colours(e) > 1 for e in FIXTURES),
        "colours by object": any(e["objects"] > 1 and colours(e) > 1 for e in FIXTURES),
        "painted": any(e["painted_triangles"] for e in FIXTURES),
        "negative part": any("negative_part" in e["part_subtypes"] for e in FIXTURES),
        "modifier part": any("modifier_part" in e["part_subtypes"] for e in FIXTURES),
        "support blocker": any("support_blocker" in e["part_subtypes"] for e in FIXTURES),
        "several plates": any(e["plates"] > 1 for e in FIXTURES),
        "over 50 MB": any(e["bytes"] > 50_000_000 for e in FIXTURES),
    }
    assert all(kinds.values()), [k for k, v in kinds.items() if not v]
