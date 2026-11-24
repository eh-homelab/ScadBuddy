"""The OpenSpool and OpenTag3D tag formats (#7273 Task 2). No database."""

from __future__ import annotations

import json
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
# (v2.004). Integers are big-endian; strings UTF-8, NUL-padded.
_EXAMPLES: list[tuple[int, int, object]] = [
    (0x00, 2, 1234),  # tag_version
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
    assert got.format == "opentag3d" and got.version == "1.234"  # tag_version example 1234
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
    assert got.warnings == []  # an older major gets no version warning


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
