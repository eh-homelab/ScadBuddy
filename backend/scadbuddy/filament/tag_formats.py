"""The two spool-tag formats ScadBuddy reads and writes (#7273): OpenSpool and OpenTag3D.

OpenSpool (https://github.com/spuder/OpenSpool/blob/c060a06c/README.md, "OpenSpool
Protocol"; https://openspool.io/rfid.html) is one NDEF record of type ``application/json``
whose object has ``"protocol": "openspool"``; every value is a string. ScadBuddy writes the
published keys (``protocol``, ``version``, ``type``, ``color_hex``, ``brand``, ``min_temp``,
``max_temp``), then each extended key the spool has a value for (#8364), from
https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/8cb0b4e1b1d8f235ca067c0e8a5cabc0197aa11f/docs/design/openspool.md:
``subtype``, ``bed_min_temp``, ``bed_max_temp``, ``alpha``, ``additional_color_hexes``,
``weight`` and ``diameter``. It reads every one of them, as a string or a number, and keeps
any other key as its JSON text in ``DecodedSpool.other_keys``.

OpenTag3D (https://github.com/queengooborg/OpenTag3D at
e14ee7ee5ff34629eb41f9f80d4830eac073cb4a: ``_data/spec.json`` v2.004, ``spec.md`` "Reader
Implementation Guidelines", ``assets/scripts/opentag3d.js``) is one NDEF record of type
``application/opentag3d``: a fixed layout of unsigned big-endian integers, NUL-padded
UTF-8 strings and RGBA bytes. A reader takes the first such record, reads missing bytes
as 0, refuses a newer major version and warns of a newer minor one. ScadBuddy reads only
major version 2: an older major has its own layout (`spec_v1.json`), so it is not read
with this one (R16).

A write replaces the tag's whole message with one record of one format (plan ruling R3).
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final, Literal

from scadbuddy.codes.parse import format_code
from scadbuddy.filament import models as m
from scadbuddy.filament.colour import canonical_rgba

TagFormat = Literal["openspool", "opentag3d"]
OPENSPOOL_MEDIA: Final = "application/json"
OPENTAG3D_MEDIA: Final = "application/opentag3d"
#: The spec version this writes: 2.004, stored as the integer times 1000.
OPENTAG3D_VERSION: Final = 2004
#: The core address range 0x00-0xDF; every payload this writes is this long.
OPENTAG3D_SIZE: Final = 0xE0

KIND_OF: Final[dict[TagFormat, m.TagKind]] = {
    "openspool": "ntag_openspool",
    "opentag3d": "ntag_opentag",
}
#: The spool's ``data_origin`` after a write of each format (R11): ``opentag3d`` is
#: Bambuddy's own word; Bambuddy has none for OpenSpool, so ``openspool`` is ScadBuddy's.
ORIGIN_OF: Final[dict[TagFormat, str]] = {"openspool": "openspool", "opentag3d": "opentag3d"}


@dataclass(frozen=True)
class SpoolFacts:
    """What a tag write says about a spool: its effective values and names."""

    material: str | None
    subtype: str | None
    brand: str | None
    colour_name: str | None
    #: ``#RRGGBBAA``.
    rgba: str | None
    extra_colours: tuple[str, ...]
    diameter_mm: Decimal | None
    density_g_cm3: Decimal | None
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None
    bed_temp_min: int | None
    bed_temp_max: int | None
    drying_temp_c: int | None
    drying_hours: Decimal | None
    label_weight_g: int | None
    empty_weight_g: int | None
    #: The spool's code, ``S-12``.
    code: str


@dataclass(frozen=True)
class EncodedTag:
    format: TagFormat
    records: list[m.NdefRecord]
    #: The fields the format needs that the spool has no value for.
    warnings: list[str]


def facts_of(spool: m.Spool, empty_weight_g: int | None) -> SpoolFacts:
    """``spool``'s facts from its effective values and names."""
    e, names = spool.effective, spool.names
    return SpoolFacts(
        material=names.material,
        subtype=names.subtype,
        brand=names.brand,
        colour_name=e.colour_name,
        rgba=e.rgba,
        extra_colours=tuple(e.extra_colours or ()),
        diameter_mm=e.diameter_mm,
        density_g_cm3=e.density_g_cm3,
        nozzle_temp_min=e.nozzle_temp_min,
        nozzle_temp_max=e.nozzle_temp_max,
        bed_temp_min=e.bed_temp_min,
        bed_temp_max=e.bed_temp_max,
        drying_temp_c=e.drying_temp_c,
        drying_hours=e.drying_hours,
        label_weight_g=e.label_weight_g,
        empty_weight_g=empty_weight_g,
        code=format_code("S", spool.number),
    )


def encode(fmt: TagFormat, facts: SpoolFacts) -> EncodedTag:
    """The one record that holds ``facts`` in ``fmt``."""
    if fmt == "openspool":
        return _openspool_encode(facts)
    return _opentag3d_encode(facts)


def format_of(records: Sequence[m.NdefRecord]) -> TagFormat | None:
    """The format of the first record that holds one, in record order."""
    found = _first(records)
    return None if found is None else found[0]


def pick_format(records: Sequence[m.NdefRecord], default: TagFormat) -> TagFormat:
    """The format to write: the tag's own, else ``default`` (the setting)."""
    return format_of(records) or default


def decode(records: Sequence[m.NdefRecord]) -> m.DecodedSpool | None:
    """The spool the first record of a known format describes; ``None`` when none does."""
    found = _first(records)
    if found is None:
        return None
    fmt, payload = found
    if fmt == "openspool":
        assert isinstance(payload, dict)
        return _openspool_decode(payload)
    assert isinstance(payload, bytes)
    return _opentag3d_decode(payload)


def _media(record: m.NdefRecord) -> str | None:
    if record.record_type != "mime" or record.media_type is None:
        return None
    return record.media_type.split(";", 1)[0].strip().lower()


class _Number(str):
    """A JSON number's text, so `_json_text` can write it back unquoted."""


def _openspool_object(record: m.NdefRecord) -> dict[str, Any] | None:
    """The record's JSON object when it is an OpenSpool one. A tag is untrusted, so every
    number is kept as its text (no ``int()`` of a huge literal, which Python refuses past
    4300 digits); `_whole` bounds it before it is converted. ``NaN`` and ``Infinity`` stay
    floats, so `_text` and `_whole` refuse them."""
    try:
        obj = json.loads(
            bytes.fromhex(record.data_hex).decode("utf-8"),
            parse_int=_Number,
            parse_float=_Number,
            parse_constant=float,
        )
    except (ValueError, RecursionError):  # JSONDecodeError and UnicodeDecodeError among them
        return None
    if isinstance(obj, dict) and obj.get("protocol") == "openspool":
        return obj
    return None


def _first(
    records: Sequence[m.NdefRecord],
) -> tuple[TagFormat, dict[str, Any] | bytes] | None:
    for record in records:
        media = _media(record)
        if media == OPENTAG3D_MEDIA:
            return "opentag3d", bytes.fromhex(record.data_hex)
        if media == OPENSPOOL_MEDIA:
            obj = _openspool_object(record)
            if obj is not None:
                return "openspool", obj
    return None


def _mime(media: str, payload: bytes) -> m.NdefRecord:
    return m.NdefRecord(record_type="mime", media_type=media, data_hex=payload.hex())


def _half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


# --- OpenSpool -------------------------------------------------------------------------


#: The most extra colours ``additional_color_hexes`` holds (the extended spec's "up to 4").
_OPENSPOOL_EXTRA_MAX: Final = 4
#: Every key `_openspool_decode` reads into a field; any other is kept in ``other_keys``.
_OPENSPOOL_KEYS: Final = frozenset(
    {
        "protocol",
        "version",
        "type",
        "color_hex",
        "brand",
        "min_temp",
        "max_temp",
        "subtype",
        "bed_min_temp",
        "bed_max_temp",
        "alpha",
        "additional_color_hexes",
        "weight",
        "diameter",
    }
)
#: The most unknown keys a read keeps; the rest are named in one warning.
_OTHER_KEYS_MAX: Final = 32


def _whole_text(value: int | None) -> str | None:
    return None if value is None else str(int(value))


def _decimal_text(value: Decimal | None) -> str | None:
    """``1.75`` for ``Decimal("1.750")``, never an exponent."""
    return None if value is None else format(value.normalize(), "f")


def _openspool_encode(facts: SpoolFacts) -> EncodedTag:
    obj: dict[str, str | list[str]] = {"protocol": "openspool", "version": "1.0"}
    warnings = []
    if facts.material:
        obj["type"] = facts.material
    else:
        warnings.append("type")
    rgba = canonical_rgba(facts.rgba)
    if rgba is not None:
        obj["color_hex"] = rgba[1:7]
    obj["brand"] = facts.brand or "Generic"
    extras = [c[1:7] for c in map(canonical_rgba, facts.extra_colours) if c is not None]
    if len(extras) > _OPENSPOOL_EXTRA_MAX:
        # The extended spec holds 4; the colours after them are not written.
        warnings.append("additional_color_hexes")
    rest: dict[str, str | list[str] | None] = {
        "min_temp": _whole_text(facts.nozzle_temp_min),
        "max_temp": _whole_text(facts.nozzle_temp_max),
        "subtype": facts.subtype or None,
        "bed_min_temp": _whole_text(facts.bed_temp_min),
        "bed_max_temp": _whole_text(facts.bed_temp_max),
        # The extended spec's default is FF, so an opaque colour has no key.
        "alpha": None if rgba is None or rgba[7:9] == "FF" else rgba[7:9],
        "additional_color_hexes": extras[:_OPENSPOOL_EXTRA_MAX] or None,
        "weight": _whole_text(facts.label_weight_g),
        "diameter": _decimal_text(facts.diameter_mm),
    }
    obj |= {k: v for k, v in rest.items() if v is not None}
    text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    return EncodedTag("openspool", [_mime(OPENSPOOL_MEDIA, text.encode("utf-8"))], warnings)


#: The most characters a string read from an OpenSpool tag keeps, as a spool's names hold
#: (`models.Name`).
_TEXT_MAX_CHARS: Final = 128


def _text(value: object) -> str | None:
    """A string, or a finite number as its text. A tag is untrusted: ``NaN`` and
    ``Infinity`` are not text."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return str(value).strip() or None


def _capped(value: object, key: str, warnings: list[str]) -> str | None:
    """`_text`, cut to `_TEXT_MAX_CHARS` characters. A cut adds ``"<key> cut to 128
    characters"`` to ``warnings``, so it does not read as a field that could not be read."""
    text = _text(value)
    if text is not None and len(text) > _TEXT_MAX_CHARS:
        warnings.append(f"{key} cut to {_TEXT_MAX_CHARS} characters")
        text = text[:_TEXT_MAX_CHARS].rstrip()
    return text


#: The longest number text `_whole` reads, and its largest magnitude: under 10,000,000
#: (a ``Decimal``'s ``adjusted()`` exponent of at most 6).
_WHOLE_MAX_CHARS: Final = 16
_WHOLE_MAX_EXPONENT: Final = 6


def _whole(value: object) -> int:
    """A whole number given as a number or a string, ``220``, ``"220"`` or ``"220.0"``.
    `ValueError` when it is not one, or when it is longer than 16 characters or at least
    10,000,000: a tag is untrusted, so the bound comes before any ``int()``."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(value)
    if isinstance(value, int) and abs(value) >= 10 ** (_WHOLE_MAX_EXPONENT + 1):
        raise ValueError("too big")
    text = str(value).strip()
    if len(text) > _WHOLE_MAX_CHARS:
        raise ValueError("too long")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError(value) from None
    if not number.is_finite() or (number and number.adjusted() > _WHOLE_MAX_EXPONENT):
        raise ValueError(value)
    if number != number.to_integral_value():
        raise ValueError(value)
    return int(number)


def _decimal(value: object) -> Decimal:
    """A positive finite number given as a number or a string, ``1.75`` or ``"1.75"``.
    `ValueError` otherwise, or for text longer than 16 characters or at least 10,000,000."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(value)
    text = str(value).strip()
    if len(text) > _WHOLE_MAX_CHARS:
        raise ValueError("too long")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError(value) from None
    if not number.is_finite() or number <= 0 or number.adjusted() > _WHOLE_MAX_EXPONENT:
        raise ValueError(value)
    return number


def _alpha(value: object) -> str:
    """The alpha byte, ``00`` to ``FF``, as two upper-case hex digits. A JSON number is
    read as its digits, as hex: ``80`` is ``"80"``."""
    text = value.strip() if isinstance(value, str) else ""
    if len(text) not in (1, 2) or any(c not in "0123456789abcdefABCDEF" for c in text):
        raise ValueError(value)
    return f"{int(text, 16):02X}"


#: How deep `_json_text` writes a nested value; deeper is ``…``.
_JSON_TEXT_DEPTH: Final = 8


def _json_text(value: object, depth: int = 0) -> str:
    """``value`` as JSON text, a number as the tag wrote it. A tag is untrusted, so a value
    nested deeper than `_JSON_TEXT_DEPTH` is written ``…``."""
    if isinstance(value, _Number):
        return str(value)
    if isinstance(value, (list, dict)) and depth >= _JSON_TEXT_DEPTH:
        return "…"
    if isinstance(value, list):
        return "[" + ", ".join(_json_text(v, depth + 1) for v in value) + "]"
    if isinstance(value, dict):
        items = [
            f"{json.dumps(k, ensure_ascii=False)}: {_json_text(v, depth + 1)}"
            for k, v in value.items()
        ]
        return "{" + ", ".join(items) + "}"
    return json.dumps(value, ensure_ascii=False)


def _colours(value: object) -> list[str]:
    """``additional_color_hexes``: a list of ``RRGGBB`` strings, as ``#RRGGBBFF``; the first
    4 are read. `ValueError` when it is not a list or a colour in it is not one."""
    if not isinstance(value, list):
        raise ValueError(value)
    picked = value[:_OPENSPOOL_EXTRA_MAX]
    colours = [canonical_rgba(c) if isinstance(c, str) else None for c in picked]
    if any(c is None for c in colours):
        raise ValueError(value)
    return [c for c in colours if c is not None]


def _other_keys(obj: dict[str, Any], warnings: list[str]) -> dict[str, str] | None:
    """The keys ScadBuddy does not read, each value as its JSON text. A tag is untrusted:
    a key or a value is cut to 128 characters and named, and only the first 32 are kept."""
    other: dict[str, str] = {}
    unknown = [k for k in obj if k not in _OPENSPOOL_KEYS]
    for key in unknown[:_OTHER_KEYS_MAX]:
        name = key
        if len(name) > _TEXT_MAX_CHARS:
            name = name[:_TEXT_MAX_CHARS]
            warnings.append(f"{name} cut to {_TEXT_MAX_CHARS} characters")
        text = _json_text(obj[key])
        if len(text) > _TEXT_MAX_CHARS:
            text = text[:_TEXT_MAX_CHARS]
            warnings.append(f"{name} cut to {_TEXT_MAX_CHARS} characters")
        other[name] = text
    if len(unknown) > _OTHER_KEYS_MAX:
        warnings.append(f"{len(unknown) - _OTHER_KEYS_MAX} more unknown keys not kept")
    return other or None


def _openspool_decode(obj: dict[str, Any]) -> m.DecodedSpool:
    hex_ = obj.get("color_hex")
    warnings: list[str] = []
    wholes: dict[str, int | None] = {}
    for key in ("min_temp", "max_temp", "bed_min_temp", "bed_max_temp", "weight"):
        wholes[key] = None
        if obj.get(key) is None:
            continue
        try:
            number = _whole(obj[key])
        except ValueError:
            warnings.append(key)
            continue
        wholes[key] = number if number > 0 else None
    version = _capped(obj.get("version"), "version", warnings)
    material = _capped(obj.get("type"), "type", warnings)
    brand = _capped(obj.get("brand"), "brand", warnings)
    subtype = _capped(obj.get("subtype"), "subtype", warnings)
    rgba = canonical_rgba(hex_) if isinstance(hex_, str) else None
    if obj.get("alpha") is not None:
        try:
            alpha = _alpha(obj["alpha"])
        except ValueError:
            warnings.append("alpha")
        else:
            rgba = None if rgba is None else rgba[:7] + alpha
    extra: list[str] | None = None
    if obj.get("additional_color_hexes") is not None:
        try:
            extra = _colours(obj["additional_color_hexes"]) or None
        except ValueError:
            warnings.append("additional_color_hexes")
    diameter: Decimal | None = None
    if obj.get("diameter") is not None:
        try:
            diameter = _decimal(obj["diameter"])
        except ValueError:
            warnings.append("diameter")
    return m.DecodedSpool(
        format="openspool",
        version=version or "",
        material=material,
        subtype=subtype,
        brand=brand,
        rgba=rgba,
        extra_colours=extra,
        diameter_mm=diameter,
        nozzle_temp_min=wholes["min_temp"],
        nozzle_temp_max=wholes["max_temp"],
        bed_temp_min=wholes["bed_min_temp"],
        bed_temp_max=wholes["bed_max_temp"],
        label_weight_g=wholes["weight"],
        other_keys=_other_keys(obj, warnings),
        warnings=warnings,
    )


# --- OpenTag3D -------------------------------------------------------------------------

_FieldKind = Literal["int", "utf8", "rgba"]

#: The core fields ScadBuddy reads or writes, as ``_data/spec.json`` lists them at
#: https://github.com/queengooborg/OpenTag3D/blob/e14ee7ee5ff34629eb41f9f80d4830eac073cb4a/_data/spec.json
#: (v2.004): ``(id, start, length, kind, scale)``, where the stored integer times
#: ``scale`` is the value (°C for scale 5, mm or g/cm³ for scale 0.001). Every other byte is 0.
_LAYOUT: Final[tuple[tuple[str, int, int, _FieldKind, Decimal], ...]] = (
    ("tag_version", 0x00, 2, "int", Decimal("0.001")),
    ("material", 0x02, 5, "utf8", Decimal(1)),
    ("material_mod", 0x07, 5, "utf8", Decimal(1)),
    ("manufacturer", 0x0C, 16, "utf8", Decimal(1)),
    ("color_name", 0x1C, 32, "utf8", Decimal(1)),
    ("color_1", 0x3C, 4, "rgba", Decimal(1)),
    ("color_2", 0x40, 4, "rgba", Decimal(1)),
    ("color_3", 0x44, 4, "rgba", Decimal(1)),
    ("color_4", 0x48, 4, "rgba", Decimal(1)),
    ("serial", 0x4C, 32, "utf8", Decimal(1)),
    ("diameter", 0x8C, 2, "int", Decimal("0.001")),
    ("print_temp", 0x90, 1, "int", Decimal(5)),
    ("min_print_temp", 0x91, 1, "int", Decimal(5)),
    ("max_print_temp", 0x92, 1, "int", Decimal(5)),
    ("chamber_temp", 0x93, 1, "int", Decimal(5)),
    ("bed_temp", 0x94, 1, "int", Decimal(5)),
    ("min_bed_temp", 0x95, 1, "int", Decimal(5)),
    ("max_bed_temp", 0x96, 1, "int", Decimal(5)),
    ("max_dry_temp", 0x9A, 1, "int", Decimal(5)),
    ("dry_time", 0x9B, 1, "int", Decimal(1)),
    ("density", 0x9C, 2, "int", Decimal("0.001")),
    ("weight", 0x9E, 2, "int", Decimal(1)),
    ("empty_spool_weight", 0xA0, 2, "int", Decimal(1)),
)
#: The fields `spec.json` marks required. ``manufacturer`` falls back to "Generic" and
#: ``chamber_temp`` is always 0, so neither is ever missing.
_REQUIRED: Final = frozenset(
    {
        "material",
        "manufacturer",
        "color_1",
        "diameter",
        "print_temp",
        "chamber_temp",
        "bed_temp",
        "density",
        "weight",
    }
)
_MAJOR: Final = OPENTAG3D_VERSION // 1000


def _cut(text: str, length: int) -> bytes:
    """``text`` as UTF-8 of at most ``length`` bytes, cut at a character boundary."""
    raw = text.encode("utf-8")[:length]
    return raw.decode("utf-8", errors="ignore").encode("utf-8")


def _middle(low: int | None, high: int | None) -> Decimal | None:
    present = [Decimal(v) for v in (low, high) if v is not None]
    return sum(present, Decimal(0)) / len(present) if present else None


def _stored(value: Decimal | int | None, scale: Decimal) -> int | None:
    return None if value is None else _half_up(Decimal(value) / scale)


def _material_and_mod(facts: SpoolFacts) -> tuple[str | None, str | None]:
    """The ``material`` and ``material_mod`` to write. A material too long for its 5 bytes,
    with no subtype, is split at its first ``-``: ``PETG-CF`` is ``PETG`` and ``CF`` (R17)."""
    material, mod = facts.material or None, facts.subtype or None
    if material and mod is None and len(material.encode("utf-8")) > 5 and "-" in material:
        head, tail = material.split("-", 1)
        material, mod = head or None, tail or None
    return material, mod


def _opentag3d_values(facts: SpoolFacts) -> dict[str, int | str | bytes | None]:
    """The value each field stores: an integer before packing, a string or RGBA bytes."""
    material, mod = _material_and_mod(facts)
    colours: list[bytes | None] = [None] * 4
    for i, colour in enumerate((facts.rgba, *facts.extra_colours[:3])):
        rgba = canonical_rgba(colour)
        colours[i] = None if rgba is None else bytes.fromhex(rgba[1:9])
    scale = {f[0]: f[4] for f in _LAYOUT}
    return {
        "tag_version": OPENTAG3D_VERSION,
        "material": material,
        "material_mod": mod,
        "manufacturer": facts.brand or "Generic",
        "color_name": facts.colour_name or None,
        "color_1": colours[0],
        "color_2": colours[1],
        "color_3": colours[2],
        "color_4": colours[3],
        "serial": facts.code,
        "diameter": _stored(facts.diameter_mm, scale["diameter"]),
        "print_temp": _stored(_middle(facts.nozzle_temp_min, facts.nozzle_temp_max), Decimal(5)),
        "min_print_temp": _stored(facts.nozzle_temp_min, Decimal(5)),
        "max_print_temp": _stored(facts.nozzle_temp_max, Decimal(5)),
        "chamber_temp": 0,
        "bed_temp": _stored(_middle(facts.bed_temp_min, facts.bed_temp_max), Decimal(5)),
        "min_bed_temp": _stored(facts.bed_temp_min, Decimal(5)),
        "max_bed_temp": _stored(facts.bed_temp_max, Decimal(5)),
        "max_dry_temp": _stored(facts.drying_temp_c, Decimal(5)),
        "dry_time": _stored(facts.drying_hours, Decimal(1)),
        "density": _stored(facts.density_g_cm3, scale["density"]),
        "weight": facts.label_weight_g,
        "empty_spool_weight": facts.empty_weight_g,
    }


def _opentag3d_encode(facts: SpoolFacts) -> EncodedTag:
    payload = bytearray(OPENTAG3D_SIZE)
    warnings: list[str] = []
    values = _opentag3d_values(facts)
    for field_id, start, length, kind, _ in _LAYOUT:
        value = values[field_id]
        if isinstance(value, int) and not 0 <= value < 256**length:
            # It does not fit the field: written as 0 and named, as a missing value is.
            warnings.append(field_id)
            continue
        if value is None:
            if field_id in _REQUIRED:
                warnings.append(field_id)
            continue
        if kind == "utf8":
            assert isinstance(value, str)
            raw = _cut(value, length)
            if len(raw) < len(value.encode("utf-8")):
                warnings.append(field_id)  # cut to fit (R17)
        elif kind == "rgba":
            assert isinstance(value, bytes)
            raw = value
        else:
            assert isinstance(value, int)
            raw = value.to_bytes(length, "big")
        payload[start : start + len(raw)] = raw
    if len(facts.extra_colours) > 3:
        # color_4 is the last colour field; the extra colours after it are not written.
        warnings.append("color_4")
    return EncodedTag("opentag3d", [_mime(OPENTAG3D_MEDIA, bytes(payload))], warnings)


def _opentag3d_decode(payload: bytes) -> m.DecodedSpool:
    if len(payload) < 2:
        # Not even a whole tag_version: no version to name, and nothing to read.
        return m.DecodedSpool(format="opentag3d", version="", warnings=["payload too short"])
    data = payload.ljust(OPENTAG3D_SIZE, b"\0")
    raw: dict[str, bytes] = {f[0]: data[f[1] : f[1] + f[2]] for f in _LAYOUT}

    def number(field_id: str) -> int | None:
        return int.from_bytes(raw[field_id], "big") or None

    def text(field_id: str) -> str | None:
        # A partial or bad UTF-8 sequence is dropped, so no U+FFFD reaches a draft.
        return raw[field_id].rstrip(b"\0").decode("utf-8", errors="ignore") or None

    def colour(field_id: str) -> str | None:
        value = raw[field_id]
        return None if not any(value) else "#" + value.hex().upper()

    def temp(field_id: str) -> int | None:
        stored = number(field_id)
        return None if stored is None else stored * 5

    def milli(field_id: str) -> Decimal | None:
        stored = number(field_id)
        return None if stored is None else Decimal(stored).scaleb(-3)

    tag_version = int.from_bytes(raw["tag_version"], "big")
    major, minor = divmod(tag_version, 1000)
    version = f"{major}.{minor:03d}"
    if major != _MAJOR:
        # Another major has a layout of its own (`spec_v1.json` for 1.x, which the reference
        # reader loads), so only the v2 layout is read (R16).
        newer = "newer" if major > _MAJOR else "older"
        return m.DecodedSpool(
            format="opentag3d", version=version, warnings=[f"{newer} major version"]
        )
    warnings = []
    if minor > OPENTAG3D_VERSION % 1000:
        warnings.append("newer minor version")
    target = temp("print_temp")
    extra = [c for c in (colour("color_2"), colour("color_3"), colour("color_4")) if c]
    hours = number("dry_time")
    return m.DecodedSpool(
        format="opentag3d",
        version=version,
        material=text("material"),
        subtype=text("material_mod"),
        brand=text("manufacturer"),
        colour_name=text("color_name"),
        rgba=colour("color_1"),
        extra_colours=extra or None,
        diameter_mm=milli("diameter"),
        density_g_cm3=milli("density"),
        nozzle_temp_min=temp("min_print_temp") or target,
        nozzle_temp_max=temp("max_print_temp") or target,
        bed_temp_min=temp("min_bed_temp"),
        bed_temp_max=temp("max_bed_temp"),
        drying_temp_c=temp("max_dry_temp"),
        drying_hours=None if hours is None else Decimal(hours),
        label_weight_g=number("weight"),
        empty_weight_g=number("empty_spool_weight"),
        serial=text("serial"),
        warnings=warnings,
    )
