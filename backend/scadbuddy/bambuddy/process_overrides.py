"""Any Bambu Studio process setting on a print, a quote or a remembered choice (#2998).

``process_overrides`` is one generic field, ``{<Bambu Studio process key>: value}``. The
owner's decision on #2998 (2026-11-14): every key is allowed, with no allowlist, and no
check refuses a key or a value. The slicer is the only judge: a slice it refuses fails
the run, and a failure whose text names one of the overrides is a 422 that names the key
(:func:`named_key`, ``workflows/problems.py`` ``setting_refused``). The named fields
(``prime_tower``, ``supports``, ``print_sequence``) stay as shorthands for the same keys;
an explicit override wins over them and over the template's ``print_settings``.

A value is taken as JSON gives it (a string, a number, a boolean, or a list of them for
a per-extruder option) and sent in the shape a Bambu process preset stores it
(:data:`~scadbuddy.bambuddy.models.ProcessValue`): a string, or a list of strings. A
boolean is Bambu's ``"1"``/``"0"``.

Bambu Studio's option definitions (``PrintConfigDef``, ``src/libslic3r/PrintConfig.cpp``)
are compiled into its binary: neither the image (``/opt/slicer-profiles`` holds the BBL
profiles only, Dockerfile ``slicer-dist``) nor the repo has them as data. So the only
check is against the resolved process preset's own values (:func:`override_warnings`): a
key the preset has no value for, or a list over a single value, is a warning, never a
refusal.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr

from scadbuddy.bambuddy.models import ProcessValue

#: One value as JSON gives it. Strict, so ``true`` stays a boolean (``"1"``) and is
#: never read as the number 1, and ``"5"`` stays the string the caller wrote.
ProcessScalar = StrictBool | StrictInt | StrictFloat | StrictStr
#: A setting's value: one value, or a list for a per-extruder-variant option.
ProcessInput = ProcessScalar | list[ProcessScalar]
#: A Bambu Studio process key: any non-empty name.
ProcessKey = Annotated[str, Field(min_length=1, max_length=200)]
#: What a request's ``process_overrides`` holds.
ProcessOverrides = dict[ProcessKey, ProcessInput]


def scalar_text(value: Any) -> str:
    """One value as a Bambu config stores it: a boolean as ``"1"``/``"0"``, a number with
    no exponent and no trailing ``.0``, a string as it is."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        if "e" in text or "E" in text:
            text = f"{value:.15f}".rstrip("0").rstrip(".")
        return text.removesuffix(".0")
    return str(value)


def value_text(value: Any) -> ProcessValue:
    """A setting as a process preset stores it: a string, or a list of strings."""
    if isinstance(value, list):
        return [scalar_text(item) for item in value]
    return scalar_text(value)


def normalised_overrides(value: Mapping[str, Any] | None) -> dict[str, ProcessValue] | None:
    """``value`` in the stored shape, sorted by key; ``None`` for none, so a request that
    sets nothing keys as one that sent no field (``runs.run_key``)."""
    if not value:
        return None
    return {key: value_text(value[key]) for key in sorted(value)}


def joined_text(value: Any) -> str:
    """A setting as one string, a list joined with commas, as Bambu Studio's JSON loader
    reads a list for a single-value option (``ConfigBase::load_from_json``)."""
    text = value_text(value)
    return ",".join(text) if isinstance(text, list) else text


def override_warnings(
    overrides: Mapping[str, ProcessValue] | None, values: Mapping[str, Any] | None
) -> str | None:
    """What to say about ``overrides`` against the resolved process preset's ``values``
    (``None`` when they could not be read: nothing is said), as one warning's text; the
    check gives it as a ``process-override-unchecked`` warning. Never a refusal: the
    setting goes as it is, and the slicer judges it."""
    if not overrides or values is None:
        return None
    unknown = [key for key in overrides if key not in values]
    shaped = [
        key
        for key, value in overrides.items()
        if isinstance(value, list) and key in values and not isinstance(values[key], list)
    ]
    said: list[str] = []
    if unknown:
        said.append(
            f"the process preset has no value for {', '.join(unknown)}: sent as set, and"
            " Bambu Studio judges whether it knows the key"
        )
    if shaped:
        said.append(
            f"{', '.join(shaped)} is a list, but the process preset stores one value:"
            " Bambu Studio reads the list joined with commas"
        )
    if not said:
        return None
    text = "; ".join(said) + "."
    return text[0].upper() + text[1:]


def named_key(text: str, overrides: Mapping[str, Any] | None) -> str | None:
    """The first of ``overrides``' keys (the longest first) that ``text`` names as a whole
    word; ``None`` when it names none."""
    for key in sorted(overrides or {}, key=len, reverse=True):
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", text):
            return key
    return None
