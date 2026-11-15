"""Any Bambu Studio process setting on a print, a quote or a remembered choice (#2998).

``process_overrides`` is one generic field, ``{<Bambu Studio process key>: value}``. The
owner's decision on #2998 (2026-11-14): every key is allowed, with no allowlist, and no
check refuses a real key or value (the map's bounds, :data:`MAX_PROCESS_OVERRIDES` and
:data:`MAX_PROCESS_VALUE_LENGTH`, are past any real setting). The slicer is the only
judge: a slice it refuses fails the run, and a failure whose text names one of the
overrides is a 422 that names the key (:func:`named_key`, ``workflows/problems.py``
``setting_refused``). The named fields (``prime_tower``, ``supports``,
``print_sequence``) stay as shorthands for the same keys; an explicit override wins over
them and over the template's ``print_settings``.

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

One exception: the host-side keys (:data:`HOST_SIDE_KEYS`). The owner's "allow
everything" covers what the slicer does with the print, not commands or files on the
machine that slices (ScadBuddy's native slicer pod, Bambuddy's slicer sidecar). Those
keys are stripped wherever a setting comes in (this module's :func:`normalised_overrides`,
``PartSettings``' extra keys, a template's ``print_settings``) and again where the slice
is built (``slicing/presets.py`` ``build_presets``, ``dispatch.slice_request``), each
time logged with the key and the reason; a print's check and run name them in a
``host-setting-dropped`` warning. Custom G-code (``*_gcode``) stays allowed: it changes
what the printer does and runs nothing on our servers.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from typing import Annotated, Any

from pydantic import AfterValidator, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from scadbuddy.bambuddy.models import ProcessValue

#: The most keys one ``process_overrides`` holds. Every key is stored in the run key,
#: the workflow history, a quote's ``choices`` and the remembered choices, so the map is
#: bounded; the bound is far past any real preset (a Bambu process preset flattens to a
#: few hundred keys), so it never refuses a real setting.
MAX_PROCESS_OVERRIDES = 2000
#: The longest one string value, or one item of a list, in characters (16 Ki): longer
#: than any real Bambu Studio value, custom G-code included.
MAX_PROCESS_VALUE_LENGTH = 16 * 1024

#: One value as JSON gives it. Strict, so ``true`` stays a boolean (``"1"``) and is
#: never read as the number 1, and ``"5"`` stays the string the caller wrote.
ProcessScalar = (
    StrictBool
    | StrictInt
    | StrictFloat
    | Annotated[StrictStr, Field(max_length=MAX_PROCESS_VALUE_LENGTH)]
)
#: A setting's value: one value, or a list for a per-extruder-variant option.
ProcessInput = ProcessScalar | list[ProcessScalar]
#: The longest key, in characters.
MAX_PROCESS_KEY_LENGTH = 200
#: The most one map of settings may weigh as compact JSON (UTF-8). Past a real one (a
#: whole Bambu process preset flattens to about 80 KB), and well under Temporal's 2 MB
#: (https://docs.temporal.io/self-hosted-guide/defaults, "Blob size limit")
#: payload limit, so an oversized request is a 422 here, never an opaque failure at
#: the workflow's start.
MAX_PROCESS_OVERRIDES_BYTES = 256 * 1024


def within_bounds[M: Mapping[str, Any]](value: M) -> M:
    """``value``, once it has at most :data:`MAX_PROCESS_OVERRIDES` keys, each of 1 to
    :data:`MAX_PROCESS_KEY_LENGTH` characters, and weighs at most
    :data:`MAX_PROCESS_OVERRIDES_BYTES` as JSON; else a ``ValueError`` (a 422)."""
    if len(value) > MAX_PROCESS_OVERRIDES:
        raise ValueError(f"at most {MAX_PROCESS_OVERRIDES} settings")
    if any(not 0 < len(key) <= MAX_PROCESS_KEY_LENGTH for key in value):
        raise ValueError(f"each key is 1 to {MAX_PROCESS_KEY_LENGTH} characters")
    size = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str).encode())
    if size > MAX_PROCESS_OVERRIDES_BYTES:
        raise ValueError(f"the settings weigh {size} bytes; at most {MAX_PROCESS_OVERRIDES_BYTES}")
    return value


#: A Bambu Studio process key: any non-empty name.
ProcessKey = Annotated[str, Field(min_length=1, max_length=MAX_PROCESS_KEY_LENGTH)]
#: What a request's ``process_overrides`` holds.
ProcessOverrides = Annotated[
    dict[ProcessKey, ProcessInput],
    Field(max_length=MAX_PROCESS_OVERRIDES),
    AfterValidator(within_bounds),
]

logger = logging.getLogger(__name__)

_RUNS = "runs a command on the machine that slices"
_WRITES = "names a file or path on the machine that slices"
_HOST = "points the slicer at a print host to upload to"
_CREDENTIAL = "is a print host's credential"

#: The settings ScadBuddy never passes to a slicer, each with why: every option of
#: ``PrintConfigDef`` in Bambu Studio v02.08.02.61's ``src/libslic3r/PrintConfig.cpp``
#: (https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/libslic3r/PrintConfig.cpp,
#: read 2026-11-14) that runs a command, names a file or path, or reaches a print host.
#: ``post_process`` (line 4616: "list their absolute paths here … Scripts will be passed
#: the absolute path to the G-code file"); ``filename_format`` (line 4534, the exported
#: file's name); ``bed_custom_texture`` and ``bed_custom_model`` (lines 1006, 1012, file
#: paths); the print host's options (lines 1065-1138, 4467: ``print_host``,
#: ``print_host_webui``, ``printhost_*``, ``host_type``). The ``CLI*ConfigDef`` options
#: there (``export_*``, ``outputdir``, ``datadir`` …) are command-line flags, which no
#: preset file sets. Any other key whose name starts ``printhost_`` or ``print_host`` is
#: stripped too (:func:`is_host_side`).
HOST_SIDE_KEYS: dict[str, str] = {
    "post_process": _RUNS,
    "filename_format": _WRITES,
    "bed_custom_texture": _WRITES,
    "bed_custom_model": _WRITES,
    "printhost_cafile": _WRITES,
    "print_host": _HOST,
    "print_host_webui": _HOST,
    "printhost_port": _HOST,
    "printhost_ssl_ignore_revoke": _HOST,
    "printhost_authorization_type": _HOST,
    "host_type": _HOST,
    "printhost_apikey": _CREDENTIAL,
    "printhost_user": _CREDENTIAL,
    "printhost_password": _CREDENTIAL,
}
_HOST_SIDE_PREFIXES = ("printhost_", "print_host")


def is_host_side(key: str) -> bool:
    """Whether ``key`` is one :data:`HOST_SIDE_KEYS` strips, spelled in any case, with
    spaces around it, or as another ``printhost_``/``print_host`` option."""
    lowered = key.strip().lower()
    return lowered in HOST_SIDE_KEYS or lowered.startswith(_HOST_SIDE_PREFIXES)


def host_side_keys(keys: Iterable[str]) -> list[str]:
    """The keys of ``keys`` that :func:`is_host_side` strips, sorted."""
    return sorted(key for key in keys if is_host_side(key))


def host_side_reason(key: str) -> str:
    """Why ``key`` is stripped."""
    return HOST_SIDE_KEYS.get(key.strip().lower(), _HOST)


def host_side_message(keys: Iterable[str]) -> str | None:
    """One sentence naming each stripped key and why; ``None`` for none."""
    named = [f"{key} ({host_side_reason(key)})" for key in keys]
    if not named:
        return None
    return (
        f"Not sent to the slicer: {'; '.join(named)}. ScadBuddy passes every slicer"
        " setting except those that act on the machine that slices."
    )


def without_host_side[V](values: Mapping[str, V], *, where: str) -> dict[str, V]:
    """``values`` without its host-side keys (:func:`is_host_side`), each one dropped
    logged with ``where`` it came in and why."""
    dropped = host_side_keys(values)
    if dropped:
        logger.warning(
            "host-side slicer settings dropped",
            extra={"where": where, "keys": dropped, "reason": host_side_message(dropped)},
        )
    return {key: value for key, value in values.items() if not is_host_side(key)}


#: The keys a part's ``<metadata>`` in ``model_settings.config`` carries for the file's own
#: structure, never a slicer setting (``render/bambu3mf.py`` writes ``name`` and
#: ``extruder``; Bambu Studio saves the rest): a part setting of one of them would move
#: the part to another filament past the plan and the AMS mapping, or rename or reshape it.
PART_STRUCTURE_KEYS = frozenset(
    {
        "name",
        "extruder",
        "subtype",
        "matrix",
        "source_file",
        "source_object_id",
        "source_volume_id",
        "source_offset_x",
        "source_offset_y",
        "source_offset_z",
        "mesh_stat",
        "object_id",
        "instance_id",
    }
)

#: The ``project_settings.config`` keys ScadBuddy itself states in a file it writes
#: (``render/bambu3mf.py`` ``project_settings``): ``print_settings_id`` marks the layout
#: as ScadBuddy's (``layout_of``), and the rest are spec §3's required-or-segfault options
#: and the bed. A template's ``print_settings`` never sets them.
PROJECT_KEYS = frozenset(
    {
        "printer_settings_id",
        "print_settings_id",
        "filament_settings_id",
        "nozzle_diameter",
        "printable_height",
        "printable_area",
        "filament_colour",
        "different_settings_to_system",
    }
)


def without_reserved[V](
    values: Mapping[str, V], reserved: frozenset[str], *, where: str
) -> dict[str, V]:
    """``values`` without the ``reserved`` keys, any case, each one dropped logged with
    ``where`` it came in: they are ScadBuddy's file structure, not slicer settings."""
    dropped = sorted(key for key in values if key.strip().lower() in reserved)
    if dropped:
        logger.warning("reserved settings dropped", extra={"where": where, "keys": dropped})
    return {key: value for key, value in values.items() if key.strip().lower() not in reserved}


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
    sets nothing keys as one that sent no field (``runs.run_key``). A host-side key
    (:data:`HOST_SIDE_KEYS`) is dropped."""
    kept = without_host_side(value or {}, where="process_overrides")
    if not kept:
        return None
    return {key: value_text(kept[key]) for key in sorted(kept)}


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
