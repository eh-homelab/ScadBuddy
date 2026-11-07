"""#5571: slicer settings per filament slot, as a request takes them, as a run keys
them and as the laid-out 3MF carries them."""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest
import trimesh
from pydantic import ValidationError

from scadbuddy.bambuddy.part_settings import (
    PartSettings,
    overrides_of,
    process_part_settings,
    unknown_slots,
)
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.runs import run_key
from scadbuddy.bambuddy.send import (
    PART_SETTINGS_AS_IS,
    PART_SETTINGS_UNWRITABLE,
    Target,
    _laid_out_for,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.render.bambu3mf import (
    MAX_MODEL_SETTINGS_BYTES,
    MODEL_SETTINGS_NAME,
    PROJECT_SETTINGS_NAME,
    PartSettingsUnwritableError,
    layout_of,
    part_settings_slots,
    printed_part_slots,
    rezipped,
    set_part_settings,
    with_part_settings,
    write_bambu_3mf,
)
from scadbuddy.render.plate import plate_for
from scadbuddy.render.split import ColourPart
from tests.support.fixtures import makerworld_path


def body(**extra: Any) -> dict[str, Any]:
    return {
        "printer_id": 1,
        "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
        "choices": {"nozzles": [{"size": "0.4"}], "tier": "standard"},
        **extra,
    }


def test_each_setting_is_written_as_the_slicer_stores_it() -> None:
    settings = PartSettings(
        top_shell_layers=8,
        bottom_shell_layers=4,
        wall_loops=3,
        sparse_infill_density=25,
        sparse_infill_pattern="gyroid",
        ironing_type="top",
    )
    assert settings.metadata() == (
        ("bottom_shell_layers", "4"),
        ("ironing_type", "top"),
        ("sparse_infill_density", "25%"),
        ("sparse_infill_pattern", "gyroid"),
        ("top_shell_layers", "8"),
        ("wall_loops", "3"),
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"top_shell_layers": -1},
        {"top_shell_layers": 1001},
        {"wall_loops": 1001},
        {"sparse_infill_density": 101},
        {"sparse_infill_pattern": "rectilinear"},
        {"ironing_type": "all"},
        {"infill_wall_overlap": "15%"},
    ],
)
def test_an_unknown_key_or_a_value_out_of_range_is_refused(settings: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PrintRunRequest.model_validate(body(part_settings={"2": settings}))


def test_the_process_values_are_read_as_the_settings_take_them() -> None:
    """#5627: a preset's strings (a vector option a list), the density with its ``%``; a
    value out of the bounds, of another shape or left out is not read."""
    read = process_part_settings(
        {
            "top_shell_layers": " 5 ",
            "bottom_shell_layers": "1001",
            "wall_loops": ["3", "3"],
            "sparse_infill_density": "15%",
            "sparse_infill_pattern": "gyroid",
            "ironing_type": 4,
        }
    )

    assert read.model_dump(exclude_none=True) == {
        "top_shell_layers": 5,
        "wall_loops": 3,
        "sparse_infill_density": 15,
        "sparse_infill_pattern": "gyroid",
    }
    assert process_part_settings({}) == PartSettings()


def test_slot_zero_is_refused() -> None:
    with pytest.raises(ValidationError):
        PrintRunRequest.model_validate(body(part_settings={"0": {"wall_loops": 3}}))


def test_part_settings_are_part_of_the_key_only_when_they_set_something() -> None:
    plain = PrintRunRequest.model_validate(body())
    empty = PrintRunRequest.model_validate(body(part_settings={}))
    unset = PrintRunRequest.model_validate(body(part_settings={"2": {}}))
    eight = PrintRunRequest.model_validate(body(part_settings={"2": {"top_shell_layers": 8}}))
    nine = PrintRunRequest.model_validate(body(part_settings={"2": {"top_shell_layers": 9}}))
    assert empty.part_settings is None and unset.part_settings is None
    key = run_key("a" * 32, plain)
    assert run_key("a" * 32, empty) == key
    assert run_key("a" * 32, unset) == key
    assert len({key, run_key("a" * 32, eight), run_key("a" * 32, nine)}) == 3


def test_overrides_are_in_slot_order_and_unknown_slots_are_named() -> None:
    request = PrintRunRequest.model_validate(
        body(part_settings={"3": {"wall_loops": 4}, "1": {"top_shell_layers": 6}})
    )
    assert overrides_of(request.part_settings) == (
        (1, (("top_shell_layers", "6"),)),
        (3, (("wall_loops", "4"),)),
    )
    assert unknown_slots(request.part_settings, {1, 2}) == [3]
    assert unknown_slots(request.part_settings, {1, 3}) == []


def test_a_copy_with_part_settings_keys_apart_from_one_without() -> None:
    plate = plate_for("H2C")
    plain = Target(plate, "0.4", colours=("#FFFFFF", "#800080"))
    eight = Target(
        plate, "0.4", colours=("#FFFFFF", "#800080"), part_settings=((2, (("wall_loops", "3"),)),)
    )
    # Without settings the key is the one copies were recorded under before #5571.
    assert "&parts:" not in plain.key
    assert plain.key != eight.key
    # The uncoloured key a project's Generate file is found by carries them too.
    assert plain.uncoloured_key != eight.uncoloured_key
    assert eight.key.startswith(eight.uncoloured_key)


SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<config>
 <object id="4">
  <metadata key="name" value="m"/>
  <metadata key="extruder" value="2"/>
  <part id="1" subtype="normal_part">
   <metadata key="name" value="Color 1"/>
   <metadata key="extruder" value="1"/>
   <metadata key="wall_loops" value="5"/>
  </part>
  <part id="2" subtype="normal_part">
   <metadata key="name" value="Color 2"/>
  </part>
  <part id="3" subtype="negative_part">
   <metadata key="name" value="Negative part"/>
   <metadata key="extruder" value="1"/>
  </part>
 </object>
 <plate>
  <metadata key="plater_id" value="1"/>
 </plate>
</config>
"""


def test_each_printed_part_of_a_slot_gets_its_settings() -> None:
    written = set_part_settings(
        SETTINGS,
        (
            (1, (("top_shell_layers", "8"), ("wall_loops", "3"))),
            (2, (("ironing_type", "top"),)),
        ),
    )
    assert written == SETTINGS.replace(
        '   <metadata key="wall_loops" value="5"/>\n  </part>',
        '   <metadata key="top_shell_layers" value="8"/>\n'
        '   <metadata key="wall_loops" value="3"/>\n  </part>',
    ).replace(
        '   <metadata key="name" value="Color 2"/>\n  </part>',
        # No extruder of its own: the object's, slot 2.
        '   <metadata key="name" value="Color 2"/>\n'
        '   <metadata key="ironing_type" value="top"/>\n  </part>',
    )


def test_a_slot_no_part_prints_with_changes_nothing() -> None:
    assert set_part_settings(SETTINGS, ((7, (("wall_loops", "3"),)),)) == SETTINGS


def test_only_the_slots_printed_parts_are_made_in_count() -> None:
    # Slot 1 and the object's slot 2; the negative part's slot 1 adds nothing.
    # A slot only painted on a part's faces (the file has 3 filaments, say) is no
    # part's, so it is not among them (#5571 review).
    assert printed_part_slots(SETTINGS) == {1, 2}


SELF_CLOSING = """<config>
 <object id="4">
  <metadata key="extruder" value="2"/>
  <part id="1" subtype="normal_part"/>
  <part id="2" subtype="normal_part">
   <metadata key="extruder" value="1"/>
  </part>
 </object>
</config>
"""


def test_a_self_closing_part_takes_its_settings_and_leaves_the_next_part_alone() -> None:
    assert printed_part_slots(SELF_CLOSING) == {1, 2}
    written = set_part_settings(SELF_CLOSING, ((2, (("wall_loops", "3"),)),))
    assert written == SELF_CLOSING.replace(
        '  <part id="1" subtype="normal_part"/>',
        '  <part id="1" subtype="normal_part">\n'
        '   <metadata key="wall_loops" value="3"/>\n'
        "  </part>",
    )
    # Slot 1's part keeps its own body; the self-closing one is not swallowed into it.
    written = set_part_settings(SELF_CLOSING, ((1, (("wall_loops", "4"),)),))
    assert written == SELF_CLOSING.replace(
        '   <metadata key="extruder" value="1"/>\n  </part>',
        '   <metadata key="extruder" value="1"/>\n'
        '   <metadata key="wall_loops" value="4"/>\n  </part>',
    )


def _two_colours(tmp_path: Path) -> bytes:
    base = trimesh.creation.box((40, 40, 4))
    block = trimesh.creation.box((20, 20, 6))
    block.apply_translation((0, 0, 5))
    out = tmp_path / "model.3mf"
    write_bambu_3mf(
        [ColourPart(1, "Color 1", "#FFFFFF", base), ColourPart(2, "Color 2", "#800080", block)],
        out,
        thumbnails=None,
    )
    return out.read_bytes()


def _model_settings(payload: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return archive.read(MODEL_SETTINGS_NAME).decode()


def test_the_laid_out_copy_carries_the_settings_and_nothing_else_changes(
    tmp_path: Path,
) -> None:
    payload = _two_colours(tmp_path)
    plate = plate_for("H2C")
    plain = _laid_out_for(payload, Target(plate, "0.4"))
    parts = _laid_out_for(
        payload, Target(plate, "0.4", part_settings=((2, (("top_shell_layers", "8"),)),))
    )
    settings = _model_settings(parts)
    assert settings == _model_settings(plain).replace(
        '   <metadata key="extruder" value="2"/>\n  </part>',
        '   <metadata key="extruder" value="2"/>\n'
        '   <metadata key="top_shell_layers" value="8"/>\n  </part>',
    )
    with zipfile.ZipFile(io.BytesIO(plain)) as a, zipfile.ZipFile(io.BytesIO(parts)) as b:
        for name in a.namelist():
            if name != MODEL_SETTINGS_NAME:
                assert a.read(name) == b.read(name), name
    assert with_part_settings(plain, None) == plain


def without_project_settings(payload: bytes) -> bytes:
    """``payload`` with no ``project_settings.config``: ``layout_of`` says ``as_is``."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as source, zipfile.ZipFile(out, "w") as copy:
        for info in source.infolist():
            if info.filename != PROJECT_SETTINGS_NAME:
                copy.writestr(info, source.read(info.filename))
    return out.getvalue()


def test_a_file_that_prints_as_it_is_refuses_part_settings(tmp_path: Path) -> None:
    """#5571 review: nothing is written into an ``as_is`` file, so a target with settings
    per part is refused rather than laid out under a key that names them."""
    payload = without_project_settings(_two_colours(tmp_path))
    assert layout_of(payload) == "as_is"
    plate = plate_for("H2C")
    assert _laid_out_for(payload, Target(plate, "0.4")) == payload
    with pytest.raises(ApiError) as refused:
        _laid_out_for(payload, Target(plate, "0.4", part_settings=((2, (("wall_loops", "3"),)),)))
    assert refused.value.status == 422
    assert refused.value.detail == PART_SETTINGS_AS_IS


# ── #5628: a library file's parts; #5637: one pass over the archive ─────────────────


def author_two_colours(tmp_path: Path, model_settings: str | None = None) -> bytes:
    """:func:`_two_colours` as its author saved it in Bambu Studio (a real process
    preset, so ``layout_of`` says ``author``), its ``model_settings.config`` replaced by
    ``model_settings`` when given."""
    payload = _two_colours(tmp_path)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        settings = json.loads(archive.read(PROJECT_SETTINGS_NAME))
    settings["print_settings_id"] = "0.20mm Standard @BBL H2C"
    changed = {PROJECT_SETTINGS_NAME: json.dumps(settings).encode()}
    if model_settings is not None:
        changed[MODEL_SETTINGS_NAME] = model_settings.encode()
    return rezipped(payload, changed)


def without_model_settings(payload: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as source, zipfile.ZipFile(out, "w") as copy:
        for info in source.infolist():
            if info.filename != MODEL_SETTINGS_NAME:
                copy.writestr(info, source.read(info.filename))
    return out.getvalue()


def test_the_layout_and_the_parts_are_read_in_one_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5637: the run's check opens the 3MF once for its layout and its parts' slots."""
    payload = author_two_colours(tmp_path)
    assert layout_of(payload) == "author"
    opened: list[object] = []
    real = zipfile.ZipFile

    def counted(*args: Any, **kwargs: Any) -> zipfile.ZipFile:
        opened.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", counted)
    assert part_settings_slots(payload) == {1, 2}
    assert len(opened) == 1


def test_a_file_nothing_can_be_written_into_has_no_slots(tmp_path: Path) -> None:
    payload = _two_colours(tmp_path)
    assert part_settings_slots(payload) == {1, 2}
    # `as_is`: the run refuses it (PART_SETTINGS_AS_IS).
    assert part_settings_slots(without_project_settings(payload)) is None
    assert part_settings_slots(b"not a zip") is None
    # No model_settings.config: no part carries settings, so every slot is unknown.
    assert part_settings_slots(without_model_settings(payload)) == set()


def _part_values(model_settings: str, key: str) -> list[tuple[int | None, list[str]]]:
    """Each printed part's slot (its ``extruder``, else its object's) and its values for
    ``key``, read with ElementTree rather than the writer's own regexes."""
    found: list[tuple[int | None, list[str]]] = []
    for obj in ET.fromstring(model_settings).iter("object"):
        own_metadata = obj.findall("metadata")
        own = next(
            (int(m.get("value", "")) for m in own_metadata if m.get("key") == "extruder"), None
        )
        for part in obj.iter("part"):
            if part.get("subtype", "normal_part") != "normal_part":
                continue
            metadata = part.findall("metadata")
            slot = next(
                (int(m.get("value", "")) for m in metadata if m.get("key") == "extruder"), own
            )
            found.append((slot, [m.get("value", "") for m in metadata if m.get("key") == key]))
    return found


def test_an_authors_file_keeps_its_layout_and_its_parts_take_the_settings(
    tmp_path: Path,
) -> None:
    """#5628: an author's file is not re-placed; only its parts of the slot change."""
    payload = author_two_colours(tmp_path)
    plate = plate_for("H2C")
    target = Target(plate, "0.4", part_settings=((2, (("top_shell_layers", "8"),)),))

    laid = _laid_out_for(payload, target)
    plain = _laid_out_for(payload, Target(plate, "0.4"))

    assert layout_of(laid) == "author"
    assert _part_values(_model_settings(laid), "top_shell_layers") == [(1, []), (2, ["8"])]
    with zipfile.ZipFile(io.BytesIO(plain)) as a, zipfile.ZipFile(io.BytesIO(laid)) as b:
        for name in a.namelist():
            if name != MODEL_SETTINGS_NAME:
                assert a.read(name) == b.read(name), name


def test_the_requests_setting_replaces_the_parts_own_per_key(tmp_path: Path) -> None:
    """#5628: a Bambu Studio file can set a key on a part already; the request's value
    replaces it, and the part's other keys stay."""
    own = _model_settings(author_two_colours(tmp_path)).replace(
        '   <metadata key="extruder" value="2"/>\n',
        '   <metadata key="extruder" value="2"/>\n'
        '   <metadata key="top_shell_layers" value="3"/>\n'
        '   <metadata key="wall_loops" value="2"/>\n',
    )
    payload = author_two_colours(tmp_path, own)
    target = Target(plate_for("H2C"), "0.4", part_settings=((2, (("top_shell_layers", "8"),)),))

    written = _model_settings(_laid_out_for(payload, target))

    assert _part_values(written, "top_shell_layers") == [(1, []), (2, ["8"])]
    assert _part_values(written, "wall_loops") == [(1, []), (2, ["2"])]


@pytest.mark.requires_fixtures
@pytest.mark.parametrize(
    "name",
    [
        # Colours by object: each part takes its object's extruder.
        "400-blaubeere-fur-spielzeug-kaufladen-deko-blaubeere.3mf",
        # 47 parts with their own extruders, painted, and object-wide process keys.
        "2069-fidget-cubes-mc-edition.3mf",
        # Painted, one part.
        "2167-pumpkin-fidget-twist.3mf",
        # ScadBuddy's own writer, saved in the library.
        "411-broccoli.3mf",
    ],
)
def test_a_real_library_files_parts_take_their_slots_settings(name: str) -> None:
    """#5628 on MakerWorld files (Git LFS): every printed part of each slot carries that
    slot's setting once, and nothing but ``model_settings.config`` differs from the copy
    laid out without them."""
    payload = makerworld_path(name).read_bytes()
    slots = part_settings_slots(payload)
    assert slots
    overrides = tuple((slot, (("top_shell_layers", str(10 + slot)),)) for slot in sorted(slots))
    plate = plate_for("H2C")

    laid = _laid_out_for(payload, Target(plate, "0.4", part_settings=overrides))
    plain = _laid_out_for(payload, Target(plate, "0.4"))

    assert layout_of(laid) == layout_of(plain)
    parts = _part_values(_model_settings(laid), "top_shell_layers")
    assert parts
    assert all(values == [str(10 + (slot or 0))] for slot, values in parts), parts
    with zipfile.ZipFile(io.BytesIO(plain)) as a, zipfile.ZipFile(io.BytesIO(laid)) as b:
        assert a.namelist() == b.namelist()
        for entry in a.namelist():
            if entry != MODEL_SETTINGS_NAME:
                assert a.read(entry) == b.read(entry), entry


# ── #5628 review: an untrusted model_settings.config costs linear time, or is refused ──

#: Many tags opened and never closed, in a comment so the file is still well-formed XML:
#: the patterns of #5571 scanned to the end from each one, quadratic in the file.
UNCLOSED = "<part " * 150_000 + "<object " * 150_000 + "<part id='1'>" * 50_000


def _crafted(where: str) -> str:
    """A well-formed config whose one real part is slot 1's, with :data:`UNCLOSED` in a
    comment at the top level (``top``) or inside the object (``object``)."""
    junk = f"<!-- {UNCLOSED} -->"
    return (
        "<config>\n"
        + (junk if where == "top" else "")
        + '<object id="1">\n  <metadata key="extruder" value="1"/>\n'
        + (junk if where == "object" else "")
        + '  <part id="1" subtype="normal_part">\n'
        + '   <metadata key="name" value="a"/>\n  </part>\n</object>\n</config>\n'
    )


@pytest.mark.parametrize("where", ["top", "object"])
def test_many_unclosed_tags_are_read_and_written_in_linear_time(where: str) -> None:
    """At the top level the real part is written. Inside the object the ``<object``
    openings stop the object's pattern before its ``</object>``, so its part is not
    written, and the check after the write refuses it: never a copy without them."""
    text = _crafted(where)
    assert len(text) < MAX_MODEL_SETTINGS_BYTES
    started = time.monotonic()

    slots = printed_part_slots(text)
    try:
        written: str | None = set_part_settings(text, ((1, (("wall_loops", "3"),)),))
    except PartSettingsUnwritableError:
        written = None

    assert time.monotonic() - started < 5, "the patterns are not linear"
    assert slots == {1}
    if where == "top":
        assert written is not None
        assert written.count('<metadata key="wall_loops" value="3"/>') == 1
    else:
        assert written is None


def test_unclosed_tags_that_are_not_xml_are_refused_at_once() -> None:
    text = "<config><object id='1'>" + "<part " * 300_000
    started = time.monotonic()
    with pytest.raises(PartSettingsUnwritableError):
        printed_part_slots(text)
    with pytest.raises(PartSettingsUnwritableError):
        set_part_settings(text, ((1, (("wall_loops", "3"),)),))
    assert time.monotonic() - started < 5


def test_a_config_past_its_cap_or_with_a_doctype_takes_no_settings(tmp_path: Path) -> None:
    """Past :data:`MAX_MODEL_SETTINGS_BYTES` (read in chunks, never whole), with a
    DOCTYPE (an entity bomb), not UTF-8 or not XML: the slots are refused, so the run
    says why (#5646), and ``_laid_out_for`` refuses with the same 422."""
    padded = SETTINGS.replace("<config>", "<config>" + " " * MAX_MODEL_SETTINGS_BYTES)
    bomb = '<?xml version="1.0"?>\n<!DOCTYPE c [<!ENTITY a "aaaa">]>\n' + SETTINGS.split("\n", 1)[1]
    plate = plate_for("H2C")
    payloads = [author_two_colours(tmp_path, config) for config in (padded, bomb, "<config>")]
    not_utf8 = rezipped(author_two_colours(tmp_path), {MODEL_SETTINGS_NAME: b"<config>\xff"})
    for payload in [*payloads, not_utf8]:
        with pytest.raises(PartSettingsUnwritableError):
            part_settings_slots(payload)
        with pytest.raises(PartSettingsUnwritableError):
            with_part_settings(payload, ((2, (("wall_loops", "3"),)),))
        with pytest.raises(ApiError) as refused:
            _laid_out_for(
                payload, Target(plate, "0.4", part_settings=((2, (("wall_loops", "3"),)),))
            )
        assert (refused.value.status, refused.value.detail) == (422, PART_SETTINGS_UNWRITABLE)
    for config in (padded, bomb):
        with pytest.raises(PartSettingsUnwritableError):
            printed_part_slots(config)


def test_a_part_the_writer_cannot_read_is_refused_not_left_without_its_settings() -> None:
    """ElementTree reads a single-quoted ``extruder``; the writer's patterns do not. The
    check after the write refuses it, so no copy's key names settings its bytes lack."""
    text = (
        '<config>\n <object id="1">\n  <part id="1" subtype="normal_part">\n'
        "   <metadata key='extruder' value='2'/>\n  </part>\n </object>\n</config>\n"
    )
    assert printed_part_slots(text) == {2}
    with pytest.raises(PartSettingsUnwritableError):
        set_part_settings(text, ((2, (("wall_loops", "3"),)),))


def test_an_objects_extruder_after_its_parts_is_its_slot() -> None:
    """#5646: ElementTree takes an object's ``extruder`` from any of its own
    ``<metadata>`` children, before its parts or after, and so does the writer."""
    text = (
        '<config>\n <object id="1">\n  <part id="1" subtype="normal_part">\n'
        '   <metadata key="name" value="a"/>\n  </part>\n'
        '  <metadata key="extruder" value="2"/>\n </object>\n</config>\n'
    )
    assert printed_part_slots(text) == {2}
    written = set_part_settings(text, ((2, (("wall_loops", "3"),)),))
    assert _part_values(written, "wall_loops") == [(2, ["3"])]


def test_a_part_the_writer_reads_as_another_slot_is_refused() -> None:
    """#5646: the writer reads the first ``extruder`` tag in the text, a comment's too;
    ElementTree skips the comment. The first object's part is slot 1's, but the writer
    reads it as slot 2's, so the check after the write refuses the part that took slot
    2's settings, rather than slicing it with them."""
    text = (
        '<config>\n <object id="1">\n  <!-- <metadata key="extruder" value="2"/> -->\n'
        '  <metadata key="extruder" value="1"/>\n'
        '  <part id="1" subtype="normal_part">\n   <metadata key="name" value="a"/>\n'
        '  </part>\n </object>\n <object id="2">\n  <metadata key="extruder" value="2"/>\n'
        '  <part id="2" subtype="normal_part">\n   <metadata key="name" value="b"/>\n'
        "  </part>\n </object>\n</config>\n"
    )
    assert printed_part_slots(text) == {1, 2}
    with pytest.raises(PartSettingsUnwritableError):
        set_part_settings(text, ((2, (("wall_loops", "3"),)),))


def test_a_nested_object_with_a_part_outside_any_object_is_refused() -> None:
    """#5656 (the review of PR #5657): a nested object counts its part twice in the walk
    object by object, and a part outside any object counts once in document order only.
    Together the two counts match, and the old check zipped the written part ``a`` with
    the old part ``b`` and passed. The check now compares the walk with the document
    order part by part and refuses the file."""
    from xml.etree import ElementTree as ET

    from scadbuddy.render.bambu3mf import _check_written

    text = (
        '<config><object id="1"><metadata key="extruder" value="2"/>'
        '<object id="2"><part id="a" subtype="normal_part">'
        '<metadata key="wall_loops" value="3"/></part></object></object>'
        '<part id="b" subtype="normal_part"><metadata key="wall_loops" value="3"/></part>'
        "</config>"
    )
    with pytest.raises(PartSettingsUnwritableError, match="exactly one object"):
        _check_written(ET.fromstring(text), ET.fromstring(text), {2: (("wall_loops", "3"),)})
