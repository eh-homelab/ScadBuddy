"""The OpenSpool and OpenTag3D tag formats (#7273 Task 2). No database."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from decimal import Decimal
from typing import Any

from scadbuddy.filament import models as m
from scadbuddy.filament.models import NdefRecord
from scadbuddy.filament.tag_formats import (
    OPENTAG3D_SIZE,
    SpoolFacts,
    decode,
    encode,
    facts_of,
    format_of,
    pick_format,
)

# The README's sample record: https://github.com/spuder/OpenSpool/blob/c060a06c/README.md
# ("OpenSpool Protocol", "NDEF Record"), type `application/json`.
OPENSPOOL_SAMPLE = {
    "protocol": "openspool",
    "version": "1.0",
    "type": "PLA",
    "color_hex": "FFAABB",
    "brand": "Generic",
    "min_temp": "220",
    "max_temp": "240",
}

_NONE: dict[str, Any] = {
    "material": None,
    "subtype": None,
    "brand": None,
    "colour_name": None,
    "rgba": None,
    "extra_colours": (),
    "diameter_mm": None,
    "density_g_cm3": None,
    "nozzle_temp_min": None,
    "nozzle_temp_max": None,
    "bed_temp_min": None,
    "bed_temp_max": None,
    "drying_temp_c": None,
    "drying_hours": None,
    "label_weight_g": None,
    "empty_weight_g": None,
}
FACTS_EMPTY = SpoolFacts(**_NONE, code="S-1")
FACTS_PLA = replace(
    FACTS_EMPTY,
    material="PLA",
    brand="Generic",
    rgba="#FFAABBFF",
    nozzle_temp_min=220,
    nozzle_temp_max=240,
)
FACTS_FULL = SpoolFacts(
    material="PETG",
    subtype="HF",
    brand="Polar",
    colour_name="Blue",
    rgba="#112233FF",
    extra_colours=("#445566FF",),
    diameter_mm=Decimal("1.75"),
    density_g_cm3=Decimal("1.27"),
    nozzle_temp_min=230,
    nozzle_temp_max=250,
    bed_temp_min=70,
    bed_temp_max=80,
    drying_temp_c=65,
    drying_hours=Decimal("6"),
    label_weight_g=1000,
    empty_weight_g=180,
    code="S-12",
)


def _json_record(obj: object) -> NdefRecord:
    return NdefRecord(
        record_type="mime", media_type="application/json", data_hex=json.dumps(obj).encode().hex()
    )


def _ot3d_record(payload: bytes) -> NdefRecord:
    return NdefRecord(
        record_type="mime", media_type="application/opentag3d", data_hex=payload.hex()
    )


# Each core field's `examples[0]`, at its `start` and `length`, from
# https://github.com/queengooborg/OpenTag3D/blob/e14ee7ee5ff34629eb41f9f80d4830eac073cb4a/_data/spec.json
# (v2.004). Integers are big-endian; strings UTF-8, NUL-padded. The one exception is
# `tag_version`: its example is 1234 (1.234), and a v1 tag has a layout of its own (R16),
# so the payload claims 2004, the version whose layout the examples follow.
_EXAMPLES: list[tuple[int, int, object]] = [
    (0x00, 2, 2004),  # tag_version
    (0x02, 5, "PLA"),  # material
    (0x07, 5, "CF"),  # material_mod
    (0x0C, 16, "Example Brand"),  # manufacturer
    (0x1C, 32, "Orange"),  # color_name
    (0x3C, 4, bytes([255, 166, 77, 255])),  # color_1
    (0x40, 4, bytes([0, 0, 0, 0])),  # color_2
    (0x44, 4, bytes([0, 0, 0, 0])),  # color_3
    (0x48, 4, bytes([0, 0, 0, 0])),  # color_4
    (0x4C, 32, "1234-ABCD"),  # serial
    (0x8C, 2, 1750),  # diameter
    (0x90, 1, 42),  # print_temp
    (0x91, 1, 38),  # min_print_temp
    (0x92, 1, 45),  # max_print_temp
    (0x93, 1, 0),  # chamber_temp
    (0x94, 1, 12),  # bed_temp
    (0x95, 1, 8),  # min_bed_temp
    (0x96, 1, 12),  # max_bed_temp
    (0x9A, 1, 10),  # max_dry_temp
    (0x9B, 1, 4),  # dry_time
    (0x9C, 2, 1240),  # density
    (0x9E, 2, 1000),  # weight
    (0xA0, 2, 105),  # empty_spool_weight
]


def _ot3d_from_examples() -> bytes:
    payload = bytearray(OPENTAG3D_SIZE)
    for start, length, value in _EXAMPLES:
        if isinstance(value, int):
            raw = value.to_bytes(length, "big")
        elif isinstance(value, str):
            raw = value.encode().ljust(length, b"\0")
        else:
            assert isinstance(value, bytes)
            raw = value
        payload[start : start + length] = raw
    return bytes(payload)


def test_openspool_decodes_the_published_sample() -> None:
    got = decode([_json_record(OPENSPOOL_SAMPLE)])
    assert got is not None
    assert (got.format, got.version, got.material, got.brand, got.rgba) == (
        "openspool",
        "1.0",
        "PLA",
        "Generic",
        "#FFAABBFF",
    )
    assert (got.nozzle_temp_min, got.nozzle_temp_max) == (220, 240)


def test_openspool_reads_numbers_as_well_as_strings() -> None:
    got = decode([_json_record(OPENSPOOL_SAMPLE | {"min_temp": 200, "max_temp": "x"})])
    assert got is not None and (got.nozzle_temp_min, got.nozzle_temp_max) == (200, None)
    assert got.warnings == ["max_temp"]


def test_openspool_reads_a_whole_number_written_with_a_point() -> None:
    got = decode([_json_record(OPENSPOOL_SAMPLE | {"min_temp": "220.0", "max_temp": 240.0})])
    assert got is not None and (got.nozzle_temp_min, got.nozzle_temp_max) == (220, 240)
    assert got.warnings == []
    half = decode([_json_record(OPENSPOOL_SAMPLE | {"min_temp": "220.5", "max_temp": [1]})])
    assert half is not None and (half.nozzle_temp_min, half.nozzle_temp_max) == (None, None)
    assert half.warnings == ["min_temp", "max_temp"]


def test_openspool_refuses_a_huge_number_quickly() -> None:
    # A tag is untrusted: an exponent this big must never reach int().
    started = time.monotonic()
    got = decode(
        [_json_record(OPENSPOOL_SAMPLE | {"min_temp": "1e999999999", "max_temp": 10_000_000})]
    )
    assert time.monotonic() - started < 1
    assert got is not None and (got.nozzle_temp_min, got.nozzle_temp_max) == (None, None)
    assert got.warnings == ["min_temp", "max_temp"]
    long = decode([_json_record(OPENSPOOL_SAMPLE | {"min_temp": "0" * 16 + "1"})])
    assert long is not None and long.warnings == ["min_temp"]  # over 16 characters


def test_openspool_names_a_json_integer_literal_too_long_for_python() -> None:
    # 5000 digits is over Python's int max-str-digits (4300), where int() raises. The
    # record is built unvalidated: it is over NTAG216's 888 bytes, but the decoder must
    # still answer a warning, not raise.
    text = '{"protocol":"openspool","version":"1.0","type":"PLA","min_temp":' + "9" * 5000 + "}"
    record = NdefRecord.model_construct(
        record_type="mime", media_type="application/json", data_hex=text.encode().hex()
    )
    got = decode([record])
    assert got is not None and got.material == "PLA" and got.nozzle_temp_min is None
    assert got.warnings == ["min_temp"]


def test_openspool_reads_no_text_from_a_non_finite_number() -> None:
    # json.dumps writes NaN and Infinity as bare tokens, which Python's json reads.
    record = _json_record(
        OPENSPOOL_SAMPLE | {"type": float("nan"), "brand": float("inf"), "version": float("-inf")}
    )
    got = decode([record])
    assert got is not None and (got.material, got.brand, got.version) == (None, None, "")


def test_openspool_cuts_a_long_string_and_names_it() -> None:
    text = json.dumps(
        OPENSPOOL_SAMPLE | {"brand": "B" * 300, "type": "é" * 200}, ensure_ascii=False
    )
    # Built unvalidated: the decoder must hold whatever a tag gives, over 888 bytes or not.
    record = NdefRecord.model_construct(
        record_type="mime", media_type="application/json", data_hex=text.encode().hex()
    )
    got = decode([record])
    assert got is not None and got.brand == "B" * 128 and got.material == "é" * 128
    # A field read and then cut says so; it is not a field that could not be read.
    assert got.warnings == ["type cut to 128 characters", "brand cut to 128 characters"]


def test_openspool_encodes_back_to_the_sample_keys_and_strings() -> None:
    enc = encode("openspool", FACTS_PLA)
    assert len(enc.records) == 1 and enc.warnings == []
    record = enc.records[0]
    assert record.media_type == "application/json" and record.record_type == "mime"
    text = bytes.fromhex(record.data_hex).decode()
    assert json.loads(text) == OPENSPOOL_SAMPLE
    assert list(json.loads(text)) == list(OPENSPOOL_SAMPLE)


def test_openspool_names_a_missing_material_and_writes_generic_for_no_brand() -> None:
    enc = encode("openspool", FACTS_EMPTY)
    body = json.loads(bytes.fromhex(enc.records[0].data_hex))
    assert enc.warnings == ["type"]
    assert body == {"protocol": "openspool", "version": "1.0", "brand": "Generic"}


def test_opentag3d_decodes_a_payload_built_from_spec_json_examples() -> None:
    got = decode([_ot3d_record(_ot3d_from_examples())])
    assert got is not None
    assert got.format == "opentag3d" and got.version == "2.004"
    assert (got.material, got.subtype, got.brand) == ("PLA", "CF", "Example Brand")
    assert got.rgba == "#FFA64DFF" and got.diameter_mm == Decimal("1.750")
    assert got.density_g_cm3 == Decimal("1.240")
    assert (got.nozzle_temp_min, got.nozzle_temp_max, got.label_weight_g, got.empty_weight_g) == (
        190,
        225,
        1000,
        105,
    )
    assert (got.bed_temp_min, got.bed_temp_max, got.drying_temp_c, got.drying_hours) == (
        40,
        60,
        50,
        Decimal(4),
    )
    assert (got.colour_name, got.serial, got.extra_colours) == ("Orange", "1234-ABCD", None)
    assert got.warnings == []


def test_opentag3d_round_trips_a_spool() -> None:
    enc = encode("opentag3d", FACTS_FULL)
    assert len(enc.records) == 1 and enc.records[0].media_type == "application/opentag3d"
    assert len(bytes.fromhex(enc.records[0].data_hex)) == 0xE0 and enc.warnings == []
    got = decode(enc.records)
    assert got is not None and got.version == "2.004"
    assert (got.material, got.brand, got.rgba, got.label_weight_g, got.serial) == (
        "PETG",
        "Polar",
        "#112233FF",
        1000,
        "S-12",
    )
    assert (got.subtype, got.colour_name, got.extra_colours) == ("HF", "Blue", ["#445566FF"])
    assert (got.diameter_mm, got.density_g_cm3) == (Decimal("1.750"), Decimal("1.270"))
    assert (got.nozzle_temp_min, got.nozzle_temp_max, got.bed_temp_min, got.bed_temp_max) == (
        230,
        250,
        70,
        80,
    )
    assert (got.drying_temp_c, got.drying_hours, got.empty_weight_g) == (65, Decimal(6), 180)
    payload = bytes.fromhex(enc.records[0].data_hex)
    assert (payload[0x90], payload[0x93], payload[0x94]) == (48, 0, 15)  # 240, 0, 75 °C


def test_opentag3d_rounds_temperatures_half_up() -> None:
    payload = bytes.fromhex(
        encode("opentag3d", replace(FACTS_FULL, nozzle_temp_min=212, nozzle_temp_max=213))
        .records[0]
        .data_hex
    )
    assert (payload[0x90], payload[0x91], payload[0x92]) == (43, 42, 43)  # 212.5, 212, 213


def test_opentag3d_names_each_missing_required_field() -> None:
    assert set(encode("opentag3d", FACTS_EMPTY).warnings) == {
        "material",
        "color_1",
        "diameter",
        "print_temp",
        "bed_temp",
        "density",
        "weight",
    }


def test_opentag3d_writes_generic_for_no_brand_and_never_warns_of_it() -> None:
    enc = encode("opentag3d", replace(FACTS_FULL, brand=None))
    assert enc.warnings == []
    got = decode(enc.records)
    assert got is not None and got.brand == "Generic"


def test_opentag3d_cuts_utf8_at_a_character_boundary() -> None:
    enc = encode("opentag3d", replace(FACTS_FULL, brand="Ünïcødé Filament Co"))  # 16-byte field
    assert enc.warnings == ["manufacturer"]  # R17: a cut string is named
    got = decode(enc.records)
    assert got is not None and got.brand is not None
    assert "�" not in got.brand and len(got.brand.encode()) <= 16
    assert got.brand == "Ünïcødé Fila"
    split = decode(encode("opentag3d", replace(FACTS_FULL, brand="ABCDEFGHIJKLMNOé")).records)
    assert split is not None and split.brand == "ABCDEFGHIJKLMNO"  # é would end at byte 17


def test_a_newer_major_version_is_not_read() -> None:
    payload = bytearray(_ot3d_from_examples())
    payload[0:2] = (3000).to_bytes(2, "big")
    got = decode([_ot3d_record(bytes(payload))])
    assert got is not None and got.material is None and "newer major version" in got.warnings
    assert got.version == "3.000"


def test_an_older_major_version_is_not_read_with_the_v2_layout() -> None:
    # spec.json's own tag_version example, 1234: a v1 tag, whose layout is spec_v1.json's.
    payload = bytearray(_ot3d_from_examples())
    payload[0:2] = (1234).to_bytes(2, "big")
    got = decode([_ot3d_record(bytes(payload))])
    assert got is not None and (got.format, got.version) == ("opentag3d", "1.234")
    assert got.warnings == ["older major version"]
    assert got.model_dump(exclude={"format", "version", "warnings"}, exclude_none=True) == {}


def test_opentag3d_splits_a_long_material_at_its_dash_into_the_subtype() -> None:
    enc = encode("opentag3d", replace(FACTS_FULL, material="PETG-CF", subtype=None))
    got = decode(enc.records)
    assert enc.warnings == [] and got is not None and (got.material, got.subtype) == ("PETG", "CF")
    # A subtype of its own keeps the material whole, cut to 5 bytes and named (R17).
    kept = encode("opentag3d", replace(FACTS_FULL, material="PETG-CF", subtype="HF"))
    got = decode(kept.records)
    assert kept.warnings == ["material"] and got is not None
    assert (got.material, got.subtype) == ("PETG-", "HF")


def test_opentag3d_names_the_colours_it_cannot_hold() -> None:
    extra = ("#000001FF", "#000002FF", "#000003FF", "#000004FF")
    enc = encode("opentag3d", replace(FACTS_FULL, extra_colours=extra))
    got = decode(enc.records)
    assert enc.warnings == ["color_4"] and got is not None
    assert got.extra_colours == list(extra[:3])


def test_opentag3d_names_a_value_too_big_for_its_field() -> None:
    enc = encode("opentag3d", replace(FACTS_FULL, label_weight_g=70000))
    got = decode(enc.records)
    assert enc.warnings == ["weight"] and got is not None and got.label_weight_g is None


def test_opentag3d_drops_a_partial_character_from_a_read_string() -> None:
    payload = bytearray(_ot3d_from_examples())
    payload[0x0C : 0x0C + 16] = b"Ab\xc3".ljust(16, b"\0")  # the first byte of a 2-byte character
    got = decode([_ot3d_record(bytes(payload))])
    assert got is not None and got.brand == "Ab"


def test_a_newer_minor_version_warns_and_is_read() -> None:
    payload = bytearray(_ot3d_from_examples())
    payload[0:2] = (2005).to_bytes(2, "big")
    got = decode([_ot3d_record(bytes(payload))])
    assert got is not None and got.material == "PLA" and got.warnings == ["newer minor version"]


def test_opentag3d_takes_the_target_for_a_missing_min_or_max_print_temp() -> None:
    payload = bytearray(_ot3d_from_examples())
    payload[0x91] = 0
    payload[0x92] = 0
    got = decode([_ot3d_record(bytes(payload))])
    assert got is not None and (got.nozzle_temp_min, got.nozzle_temp_max) == (210, 210)


def test_an_opentag3d_payload_without_a_whole_version_is_too_short() -> None:
    for payload in (b"", b"\x07"):
        got = decode([_ot3d_record(payload)])
        assert got is not None and (got.version, got.warnings) == ("", ["payload too short"])
        assert got.model_dump(exclude={"format", "version", "warnings"}, exclude_none=True) == {}


def test_a_short_opentag3d_payload_reads_its_missing_bytes_as_zero() -> None:
    got = decode([_ot3d_record(_ot3d_from_examples()[:0x20])])
    assert got is not None and got.material == "PLA" and got.label_weight_g is None


def test_pick_format_keeps_the_tags_format_and_else_takes_the_default() -> None:
    ot3d_record = _ot3d_record(_ot3d_from_examples())
    assert pick_format([_json_record(OPENSPOOL_SAMPLE)], "opentag3d") == "openspool"
    assert pick_format([ot3d_record], "openspool") == "opentag3d"
    assert pick_format([], "opentag3d") == "opentag3d"
    assert pick_format([NdefRecord(record_type="url", data_hex="00")], "openspool") == "openspool"
    assert pick_format([_json_record({"protocol": "other"})], "opentag3d") == "opentag3d"


def test_the_first_matching_record_wins() -> None:
    ot3d_record = _ot3d_record(_ot3d_from_examples())
    assert format_of([ot3d_record, _json_record(OPENSPOOL_SAMPLE)]) == "opentag3d"
    assert format_of([_json_record([1]), _json_record(OPENSPOOL_SAMPLE), ot3d_record]) == (
        "openspool"
    )


def test_a_json_record_that_is_not_json_decodes_to_nothing() -> None:
    assert (
        decode([NdefRecord(record_type="mime", media_type="application/json", data_hex="7b7b")])
        is None
    )


def test_facts_of_reads_the_effective_values_and_names() -> None:
    spool = m.Spool.model_construct(
        number=12,
        effective=m.EffectiveValues(
            colour_name="Blue",
            rgba="#112233FF",
            extra_colours=["#445566FF"],
            diameter_mm=Decimal("1.75"),
            nozzle_temp_min=230,
            label_weight_g=1000,
        ),
        names=m.SpoolNames(material="PETG", subtype="HF", brand="Polar"),
    )
    facts = facts_of(spool, 180)
    assert (facts.material, facts.subtype, facts.brand, facts.code) == (
        "PETG",
        "HF",
        "Polar",
        "S-12",
    )
    assert (facts.rgba, facts.extra_colours, facts.empty_weight_g) == (
        "#112233FF",
        ("#445566FF",),
        180,
    )
    assert (facts.nozzle_temp_min, facts.label_weight_g, facts.density_g_cm3) == (230, 1000, None)
