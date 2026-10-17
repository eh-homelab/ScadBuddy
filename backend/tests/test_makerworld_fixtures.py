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

from scadbuddy.render.objects3mf import read_objects
from scadbuddy.render.read_budget import ReadBudget
from tests.support.fixtures import makerworld, makerworld_path

FIXTURES = makerworld()
# What a manifest's `arrange_reads` records of a fixture the reader reads.
READ_KEYS = {"objects", "copies", "colours", "painted_parts", "negative_parts", "notes"}


def ids(entry: dict[str, Any]) -> str:
    return str(entry["file"]).removesuffix(".3mf")


@pytest.mark.requires_fixtures
@pytest.mark.parametrize("entry", FIXTURES, ids=ids)
def test_a_fixture_is_the_file_its_manifest_names(entry: dict[str, Any]) -> None:
    # Hashed in chunks: the files run to 98 MB, and xdist workers each hold one (#2490).
    path = makerworld_path(entry["file"])
    assert path.stat().st_size == entry["bytes"]
    with path.open("rb") as f:
        assert hashlib.file_digest(f, "sha256").hexdigest() == entry["sha256"]


@pytest.mark.requires_fixtures
@pytest.mark.parametrize("entry", FIXTURES, ids=ids)
def test_arranges_reader_reads_each_fixture_as_recorded(entry: dict[str, Any]) -> None:
    # Every fixture reads since #2337; a refusal is the reader's own tests' to pin (#2490).
    # Read whole, as read_objects takes bytes: unlike the hash above, this holds a file
    # of up to 98 MB in memory, one per xdist worker.
    expected = entry["arrange_reads"]
    # Exactly what a read records, so a refusal recorded any other way fails here too
    # (#3579): nothing writes a `refused` key, which the old guard looked for.
    assert set(expected) == READ_KEYS, (
        f"{entry['file']}: the manifest records something other than a read; "
        "pin it in the reader's tests"
    )
    objects = read_objects(makerworld_path(entry["file"]).read_bytes(), ReadBudget())
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
        "200 objects (Arrange's max_objects)": any(e["objects"] >= 200 for e in FIXTURES),
    }
    assert all(kinds.values()), [k for k, v in kinds.items() if not v]
