"""Issue #80 — the provenance stamped into every output's 3MF."""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import trimesh

from scadbuddy.render.bambu3mf import CORE_NS, write_bambu_3mf
from scadbuddy.render.provenance import (
    PROVENANCE_KEY,
    SCADBUDDY_NS,
    Provenance,
    read,
    source_version,
    stamp,
)
from scadbuddy.render.split import ColourPart

PROVENANCE = Provenance(
    model="name-keychain",
    version="sha256:" + "ab" * 32,
    output="f" * 32,
    params={"name": "Reagan", "text_size": 14, "keyring_hole": True},
    edit_url="https://scadbuddy.test/edit/" + "f" * 32,
)

# The 3MF metadata Bambu Studio writes and reads; the stamp must not disturb them.
BAMBU_KEYS = {"Application", "Title"}


@pytest.fixture
def written(tmp_path: Path) -> Path:
    out = tmp_path / "model.3mf"
    write_bambu_3mf(
        [ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box(extents=(10, 10, 4)))],
        out,
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
    assert PROVENANCE.edit_url is not None
    assert PROVENANCE.edit_url in after["Description"]


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
