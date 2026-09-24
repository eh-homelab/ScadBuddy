"""Issue #80 — the provenance stamped into every output's 3MF."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import trimesh

from scadbuddy.library.deeplink import EDIT_NOTE, edit_url
from scadbuddy.render.bambu3mf import CORE_NS, PRODUCTION_NS, write_bambu_3mf
from scadbuddy.render.provenance import (
    NS_PREFIX,
    PROVENANCE_KEY,
    ROOT_MODEL,
    SCADBUDDY_NS,
    Provenance,
    read,
    source_version,
    stamp,
)
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.render.split import ColourPart
from scadbuddy.render.thumbnail import render_plate_thumbnails

PROVENANCE = Provenance(
    model="name-keychain",
    version="sha256:" + "ab" * 32,
    output="f" * 32,
    params={"name": "Reagan", "text_size": 14, "keyring_hole": True},
    edit_url="https://scadbuddy.test/edit/" + "f" * 32,
)

# The 3MF metadata Bambu Studio writes and reads; the stamp must not disturb them.
BAMBU_KEYS = {"Application", "Title"}


@pytest.fixture(params=[False, True], ids=["no-covers", "with-covers"])
def written(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """Both shapes a real output takes: #107 puts four cover PNGs in every 3MF, and
    the stamp has to leave them exactly as it found them."""
    out = tmp_path / "model.3mf"
    parts = [ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box(extents=(10, 10, 4)))]
    write_bambu_3mf(
        parts,
        out,
        thumbnails=render_plate_thumbnails(parts) if request.param else None,
        model_name="name-keychain",
    )
    return out


def _metadata(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("3D/3dmodel.model").decode("utf-8"))
    return {
        element.get("name", ""): element.text or ""
        for element in root.findall(f"{{{CORE_NS}}}metadata")
    }


def test_the_stamp_round_trips(written: Path) -> None:
    stamp(written, PROVENANCE)
    assert read(written) == PROVENANCE


def test_an_unstamped_3mf_reads_as_no_provenance(written: Path) -> None:
    assert read(written) is None


def test_something_that_is_not_a_3mf_reads_as_no_provenance(tmp_path: Path) -> None:
    junk = tmp_path / "model.3mf"
    junk.write_bytes(b"PK\x03\x04not a zip")
    assert read(junk) is None


def test_every_other_part_survives_byte_for_byte(written: Path) -> None:
    with zipfile.ZipFile(written) as archive:
        before = {name: archive.read(name) for name in archive.namelist()}

    stamp(written, PROVENANCE)

    with zipfile.ZipFile(written) as archive:
        after = {name: archive.read(name) for name in archive.namelist()}
    assert list(after) == list(before)
    assert {name: payload for name, payload in after.items() if name != "3D/3dmodel.model"} == {
        name: payload for name, payload in before.items() if name != "3D/3dmodel.model"
    }


def test_bambu_studio_still_reads_the_root_model(written: Path) -> None:
    before = _metadata(written)
    stamp(written, PROVENANCE)
    after = _metadata(written)

    assert set(before) >= BAMBU_KEYS
    assert {key: after[key] for key in BAMBU_KEYS} == {key: before[key] for key in BAMBU_KEYS}
    # The ScadBuddy keys are namespaced the way Bambu Studio namespaces its own.
    with zipfile.ZipFile(written) as archive:
        xml = archive.read("3D/3dmodel.model").decode("utf-8")
    assert f'xmlns:ScadBuddy="{SCADBUDDY_NS}"' in xml
    assert after["Designer"] == "ScadBuddy"
    # The same phrase Bambuddy's note uses; one constant, or the two drift apart.
    assert after["Description"].startswith(EDIT_NOTE)
    assert PROVENANCE.edit_url is not None
    assert PROVENANCE.edit_url in after["Description"]


def test_the_root_model_keeps_the_prefixes_bambu_studio_reads(written: Path) -> None:
    """Why the write side splices instead of round-tripping through ElementTree.

    Bambu Studio reads the production extension by prefix — ``p:path``, ``p:UUID``
    on every component and build item. Re-serializing the document is free to
    rename that prefix and rewrite the declaration; the splice cannot.
    """
    with zipfile.ZipFile(written) as archive:
        before = archive.read(ROOT_MODEL).decode("utf-8")
    stamp(written, PROVENANCE)
    with zipfile.ZipFile(written) as archive:
        after = archive.read(ROOT_MODEL).decode("utf-8")

    assert f'xmlns:p="{PRODUCTION_NS}"' in after
    # Only the <model> tag changes, and only by the ScadBuddy declaration; every
    # other line survives verbatim, prefixed attributes and all.
    kept = [line for line in before.splitlines() if not line.startswith("<model ")]
    assert all(line in after.splitlines() for line in kept)
    assert after.replace(f' xmlns:{NS_PREFIX}="{SCADBUDDY_NS}"', "").splitlines()[1] == next(
        line for line in before.splitlines() if line.startswith("<model ")
    )


def test_re_stamping_replaces_rather_than_appends(written: Path) -> None:
    stamp(written, PROVENANCE)
    once = written.read_bytes()
    stamp(written, PROVENANCE)
    assert written.read_bytes() == once

    stamp(written, PROVENANCE.model_copy(update={"params": {"name": "Nova"}}))
    with zipfile.ZipFile(written) as archive:
        xml = archive.read("3D/3dmodel.model").decode("utf-8")
    assert xml.count(f'xmlns:ScadBuddy="{SCADBUDDY_NS}"') == 1
    assert xml.count(f'name="{PROVENANCE_KEY}"') == 1
    stamped = read(written)
    assert stamped is not None
    assert stamped.params == {"name": "Nova"}


def test_the_version_is_a_content_hash_of_every_scad_in_the_model(tmp_path: Path) -> None:
    model = tmp_path / "keychain"
    (model / "parts").mkdir(parents=True)
    (model / "model.scad").write_text("cube(10);\n", encoding="utf-8")

    one_file = source_version(model)
    assert one_file.startswith("sha256:")
    assert source_version(model) == one_file

    (model / "model.scad").write_text("cube(20);\n", encoding="utf-8")
    edited = source_version(model)
    assert edited != one_file

    # An included source counts, and so does where it sits.
    (model / "parts" / "hole.scad").write_text("circle(3);\n", encoding="utf-8")
    included = source_version(model)
    assert included != edited
    (model / "parts" / "hole.scad").rename(model / "hole.scad")
    assert source_version(model) != included


def test_the_version_covers_what_a_model_imports(tmp_path: Path) -> None:
    """A model may import() or include an asset beside it (design doc §2), and an STL
    swapped under a .scad that never changed produces different geometry."""
    model = tmp_path / "keychain"
    (model / "parts").mkdir(parents=True)
    (model / "model.scad").write_text('import("parts/base.stl");\n', encoding="utf-8")
    (model / "parts" / "base.stl").write_bytes(b"solid base\nendsolid base\n")
    before = source_version(model)

    (model / "parts" / "base.stl").write_bytes(b"solid other\nendsolid other\n")
    assert source_version(model) != before


def test_the_version_ignores_what_the_renderer_writes_back(tmp_path: Path) -> None:
    """``model.json`` caches the schema, keyed off the .scad; a model whose source
    has not changed must not appear to be a new version because of it."""
    model = tmp_path / "keychain"
    model.mkdir()
    (model / "model.scad").write_text("cube(10);\n", encoding="utf-8")
    before = source_version(model)

    (model / "model.json").write_text('{"name": "Keychain"}\n', encoding="utf-8")
    (model / "thumbnail.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert source_version(model) == before


def test_a_stamp_that_does_not_parse_is_a_miss_not_a_crash(written: Path) -> None:
    """A 3MF stamped by a future ScadBuddy, or hand-edited, must degrade to the same
    404 as one carrying no stamp at all — not a 500 with a ValidationError in it."""
    stamp(written, PROVENANCE)
    with zipfile.ZipFile(written) as archive:
        entries = [(name, archive.read(name)) for name in archive.namelist()]
    broken = [
        (
            name,
            payload.replace(
                PROVENANCE.model_dump_json(exclude_none=True).encode("utf-8"),
                b'{"model": "keychain"}',
            )
            if name == "3D/3dmodel.model"
            else payload,
        )
        for name, payload in entries
    ]
    with zipfile.ZipFile(written, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in broken:
            archive.writestr(name, payload)

    assert read(written) is None


def test_a_stamp_that_is_not_json_at_all_is_a_miss(written: Path) -> None:
    stamp(written, PROVENANCE)
    with zipfile.ZipFile(written) as archive:
        entries = [(name, archive.read(name)) for name in archive.namelist()]
    broken = [
        (
            name,
            payload.replace(
                PROVENANCE.model_dump_json(exclude_none=True).encode("utf-8"), b"not json"
            )
            if name == "3D/3dmodel.model"
            else payload,
        )
        for name, payload in entries
    ]
    with zipfile.ZipFile(written, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in broken:
            archive.writestr(name, payload)

    assert read(written) is None


def test_the_version_ignores_the_renderers_transient_wrapper(tmp_path: Path) -> None:
    """``render_solids`` writes a per-colour wrapper .scad INTO the model directory
    and unlinks it afterwards. A render of this model running concurrently with a
    save must not put a random filename into the hash."""
    model = tmp_path / "keychain"
    model.mkdir()
    (model / "model.scad").write_text("cube(10);\n", encoding="utf-8")
    alone = source_version(model)

    (model / f"{WRAPPER_PREFIX}0123456789abcdef.scad").write_text(
        "include <model.scad>\n", encoding="utf-8"
    )
    assert source_version(model) == alone

    (model / f"{WRAPPER_PREFIX}fedcba9876543210.scad").write_text(
        "include <model.scad>\n", encoding="utf-8"
    )
    assert source_version(model) == alone


def test_a_root_model_the_stamp_cannot_splice_is_refused(written: Path) -> None:
    """The splice assumes the writer's shape; say so instead of emitting broken XML.

    ``_stamped_root_model`` opens the tag by replacing its final ``>``. Against a
    self-closing ``<model .../>`` that would eat the slash and produce XML no
    reader can parse — silently, since nothing re-parses on the write path.
    """
    with zipfile.ZipFile(written) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(written, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            if name == ROOT_MODEL:
                payload = re.sub(rb"<model\b[^>]*>.*", b"<model/>", payload, flags=re.DOTALL)
            archive.writestr(name, payload)

    with pytest.raises(ValueError, match="self-closing"):
        stamp(written, PROVENANCE)


# A customizer string parameter is arbitrary text: /models/{slug}/render takes params
# as a JSON body, so these reach the stamp without passing through the UI.
HOSTILE = {
    "markup": '</metadata><metadata name="Injected">pwned</metadata>',
    "entities": "a & b < c > d",
    "controls": "bell\x07 formfeed\x0c vtab\x0b unit\x1f",
    "quotes": "he said \"hi\" & 'bye'",
}


def test_a_hostile_parameter_value_cannot_break_the_root_model(written: Path) -> None:
    """The JSON blob is the only untrusted text in the file; pin what keeps it inert.

    Two layers do it, and both matter. Pydantic's serializer escapes the C0 control
    characters XML has no representation for — one raw byte would invalidate the
    whole document, geometry included, not just the stamp. ``escape`` then handles
    ``&``/``<``/``>``, which JSON leaves alone.
    """
    hostile = PROVENANCE.model_copy(update={"params": dict(HOSTILE)})
    stamp(written, hostile)

    with zipfile.ZipFile(written) as archive:
        xml = archive.read(ROOT_MODEL).decode("utf-8")
    root = ET.fromstring(xml)  # well-formed, not merely round-trippable
    assert [element.get("name") for element in root.findall(f"{{{CORE_NS}}}metadata")].count(
        "Injected"
    ) == 0
    assert not any(character in xml for character in "\x07\x0b\x0c\x1f")
    assert read(written) == hostile


def test_a_public_url_that_cannot_be_written_as_xml_yields_no_link(written: Path) -> None:
    """The one piece of settable text that is not JSON-encoded on its way in.

    ``params`` reach the file through ``model_dump_json``, which escapes what XML
    cannot represent; ``edit_url`` is spliced as prose with only ``escape()``. So a
    ``public_url`` holding a raw control character would corrupt every 3MF written
    after it was saved — the link is dropped instead, exactly as when none is set.
    """
    assert edit_url("https://scad\x0b.test/", "f" * 32) is None

    stamp(written, PROVENANCE.model_copy(update={"edit_url": None}))
    with zipfile.ZipFile(written) as archive:
        ET.fromstring(archive.read(ROOT_MODEL).decode("utf-8"))
    assert "Description" not in _metadata(written)


def test_a_failed_rewrite_leaves_the_original_3mf_alone(
    written: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 3MF is the deliverable; a half-written one is worse than an unstamped one."""
    original = written.read_bytes()
    calls = {"n": 0}
    real = zipfile.ZipFile.writestr

    def fail_partway(self: zipfile.ZipFile, *args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("no space left on device")
        real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_partway)
    with pytest.raises(OSError, match="no space"):
        stamp(written, PROVENANCE)

    assert written.read_bytes() == original
    assert list(written.parent.iterdir()) == [written]


def test_each_entry_keeps_the_compression_the_writer_chose(written: Path) -> None:
    """#107 stores the cover PNGs uncompressed on purpose; re-deflating them undoes it."""
    with zipfile.ZipFile(written) as archive:
        before = {info.filename: info.compress_type for info in archive.infolist()}

    stamp(written, PROVENANCE)

    with zipfile.ZipFile(written) as archive:
        after = {info.filename: info.compress_type for info in archive.infolist()}
    assert after == before


def test_per_object_metadata_is_not_collateral(written: Path) -> None:
    """The stamp owns three keys on the ROOT model. 3MF allows <metadata> inside an
    <object> too, and a Designer there belongs to whoever put it there."""
    with zipfile.ZipFile(written) as archive:
        entries = [
            (i.filename, i.compress_type, archive.read(i.filename)) for i in archive.infolist()
        ]
    inside = b'   <metadata name="Designer">someone else</metadata>\n'
    with zipfile.ZipFile(written, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, compression, payload in entries:
            if name == ROOT_MODEL:
                payload = payload.replace(b"   <components>", inside + b"   <components>")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = compression
            archive.writestr(info, payload)

    stamp(written, PROVENANCE)

    with zipfile.ZipFile(written) as archive:
        xml = archive.read(ROOT_MODEL).decode("utf-8")
    assert "someone else" in xml
