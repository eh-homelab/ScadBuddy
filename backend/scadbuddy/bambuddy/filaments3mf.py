"""A library 3MF's filament slots, read from the file itself (#2550).

Bambuddy's ``GET /library/files/{id}/filament-requirements`` reads the same file, but for
an unsliced project with a ``plate_id`` it runs a preview slice to say which slots the
plate uses, and on a large file that outlasts ScadBuddy's 30 s budget: the print dialog's
``/choices`` then failed whole. ScadBuddy downloads the file anyway, for the preview, so
the slots are read here, the way Bambuddy reads them, and Bambuddy is asked only for a
file this cannot read.

What Bambuddy answers, and so what this answers (its ``library.py``):

* A file with per-plate filaments in ``Metadata/slice_info.config`` (a project Bambu
  Studio sliced): the filaments with ``used_g > 0``, of the plate asked for, or of every
  plate. ``slot_id`` is the filament's ``id``.
* Otherwise, every project filament in ``Metadata/project_settings.config``, in order:
  ``slot_id`` is its 1-based position in ``filament_colour``/``filament_type``, with
  ``used_grams: 0`` (unknown, see :mod:`scadbuddy.bambuddy.filaments`). Which slots a
  plate uses is read from ``Metadata/model_settings.config`` (each part's extruder, or its
  object's) rather than from a slice. Where that cannot be known (painting, layer ranges,
  no plate listing) every slot counts as used, as Bambuddy's own answer does when its
  preview slice gives nothing: an extra slot offered is visible, a missing one is not.

Only the settings entries are inflated, each capped, plus a scan of the model files for
painting within the read budget; a file past the budget is read as "painting unknown".

A sliced file (``gcode.3mf``, #2671) is read with ``sliced=True``: its slots are
``slice_info.config``'s, as above, and :class:`SlicedProfile` says what it was sliced
for (the printer, the nozzle, the process and plate type) and which plates it holds
G-code for, which is what decides whether it can print as it is.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, BinaryIO

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.models import FilamentRequirement, FilamentRequirements
from scadbuddy.render.bambu3mf import (
    _GCODE,
    MAX_SETTINGS_BYTES,
    MODEL_SETTINGS_NAME,
    PROJECT_SETTINGS_NAME,
    ArchiveTooLargeError,
    _read_capped,
)
from scadbuddy.render.read_budget import ReadBudget
from scadbuddy.render.split import PAINT_ATTRIBUTE

SLICE_INFO_NAME = "Metadata/slice_info.config"
#: Bambu Studio's prefix on a printer model in ``project_settings.config``.
_VENDOR = "BAMBU LAB"
#: Every name a model goes by that is not Bambuddy's short name for it, compared as
#: :func:`model_key` leaves them (#2956). From Bambuddy's own tables, at
#: maziggy/bambuddy@943f3587 ``backend/app/utils/printer_models.py``:
#: ``PRINTER_MODEL_MAP`` (Bambu Studio's ``printer_model`` names) and
#: ``PRINTER_MODEL_ID_MAP`` (the codes a printer announces over SSDP, which Bambuddy
#: may keep as ``Printer.model``), lines 8-64 of
#: https://github.com/maziggy/bambuddy/blob/943f35871289938781bad9129dba73dd6997ec2f/backend/app/utils/printer_models.py#L8-L64
#: Copied by hand (#3897): when Bambuddy adds a model, add its Studio name and its SSDP
#: code here, and check ``GCODE_COMPAT_FAMILIES`` (line 446 there) against
#: :data:`_X1_FAMILY`.
_MODEL_ALIASES = {
    "X1CARBON": "X1C",
    "A1M": "A1MINI",
    "BLP001": "X1C",
    "BLP002": "X1",
    "C13": "X1E",
    "C11": "P1P",
    "C12": "P1S",
    "N7": "P2S",
    "N6": "X2D",
    "N9": "A2L",
    "A11": "A1",
    "A12": "A1MINI",
    "N1": "A1MINI",
    "N2S": "A1",
    "A04": "A1MINI",
    "O1D": "H2D",
    "O1E": "H2DPRO",
    "O2D": "H2DPRO",
    "O1C": "H2C",
    "O1C2": "H2C",
    "O1S": "H2S",
}
#: Models whose G-code Bambuddy dispatches to one another (``GCODE_COMPAT_FAMILIES``,
#: same file): the X1 and P1 series, one 256 mm CoreXY single-nozzle family. Every
#: other model takes only its own G-code.
_X1_FAMILY = frozenset({"X1", "X1C", "X1E", "P1P", "P1S"})
#: Where a plate's index sits in ``Metadata/plate_<n>.gcode``.
_PLATE_PREFIX = len("Metadata/plate_")
_GCODE_SUFFIX = -len(".gcode")
#: Moves when what is read from the same bytes changes, so an older cached read is not
#: served.
FILAMENTS_VERSION = 1
#: Parts that never print in their extruder's filament.
_NO_FILAMENT = {"negative_part", "support_blocker", "support_enforcer"}
#: Project settings naming a filament for supports: a slot they name may print on any
#: plate. ``0`` is "the object's own".
_SUPPORT_FILAMENTS = ("support_filament", "support_interface_filament")
_PAINT = PAINT_ATTRIBUTE.encode()
_CHUNK = 1024 * 1024


class UnreadableFilamentsError(ValueError):
    """A file whose slots cannot be read here; Bambuddy is asked instead."""


class SlicedPlate(BaseModel):
    """One plate a sliced file holds G-code for, as ``slice_info.config`` states it."""

    index: int
    #: Bambu Studio's estimate (``prediction``), seconds.
    print_seconds: int | None = None
    #: Filament it uses (``weight``), grams.
    weight_g: float | None = None
    #: The nozzle diameters it was sliced for (``nozzle_diameters``), as written.
    nozzle_diameters: list[str] = Field(default_factory=list)


class SlicedProfile(BaseModel):
    """What a sliced file was sliced for (#2671), read from the file itself: none of it
    can change without slicing again, so the print dialog shows it read-only, and the
    run warns of a printer it does not fit rather than refusing it."""

    #: ``project_settings.config``'s ``printer_model`` ("Bambu Lab H2C").
    printer_model: str | None = None
    #: Its printer and process presets' names, and the plate type.
    printer_preset: str | None = None
    process: str | None = None
    bed_type: str | None = None
    #: Every nozzle diameter a plate was sliced for, else the project's.
    nozzle_diameters: list[str] = Field(default_factory=list)
    #: The plates with G-code in the file, in index order.
    plates: list[SlicedPlate] = Field(default_factory=list)

    def plate(self, index: int) -> SlicedPlate | None:
        return next((plate for plate in self.plates if plate.index == index), None)

    def diameters_for(self, indices: list[int]) -> list[str]:
        """The nozzle diameters ``indices`` were sliced for, each once, normalised."""
        found: list[str] = []
        for index in indices:
            plate = self.plate(index)
            for value in (plate.nozzle_diameters if plate else []) or self.nozzle_diameters:
                size = diameter(value)
                if size is not None and size not in found:
                    found.append(size)
        return found


def diameter(value: str | None) -> str | None:
    """A nozzle diameter as Bambuddy writes one ("0.4"), whatever the file wrote ("0.40")."""
    try:
        number = float(value or "")
    except ValueError:
        return None
    return f"{number:g}" if number > 0 else None


def model_key(model: str | None) -> str | None:
    """A printer model compared across Bambu Studio's and Bambuddy's names: "Bambu Lab
    X1 Carbon" and "X1C" are one key, "Bambu Lab H2C" and "H2C" another."""
    text = (model or "").upper().strip()
    if text.startswith(_VENDOR):
        text = text[len(_VENDOR) :]
    key = "".join(ch for ch in text if ch.isalnum())
    return _MODEL_ALIASES.get(key, key) or None


def same_family(a: str | None, b: str | None) -> bool:
    """Whether two :func:`model_key` keys are different models whose G-code Bambuddy
    still dispatches across (#2956)."""
    return a != b and a in _X1_FAMILY and b in _X1_FAMILY


class LocalFilaments(BaseModel):
    """What :func:`read_filaments` read: enough to answer ``filament-requirements`` for
    any plate, cached as one file per library file."""

    version: int = FILAMENTS_VERSION
    #: Every project filament, in slot order.
    project: list[FilamentRequirement] = Field(default_factory=list)
    #: ``slice_info.config``'s used filaments, by plate index.
    sliced: dict[int, list[FilamentRequirement]] = Field(default_factory=dict)
    #: The slots each plate's parts name, by plate index; ``None`` when that cannot be
    #: known, and every slot then counts as used.
    plate_slots: dict[int, list[int]] | None = None
    #: What a sliced file was sliced for (#2671); ``None`` for a file that is not.
    profile: SlicedProfile | None = None

    def for_plate(self, plate_id: int | None) -> FilamentRequirements:
        """Bambuddy's answer for ``plate_id`` (``None``: the whole file)."""
        if self.sliced:
            if plate_id is None:
                best: dict[int, FilamentRequirement] = {}
                for filaments in self.sliced.values():
                    for filament in filaments:
                        known = best.get(filament.slot_id)
                        if known is None or filament.used_grams > known.used_grams:
                            best[filament.slot_id] = filament
                return _answer(plate_id, sorted(best.values(), key=lambda f: f.slot_id))
            if plate_id in self.sliced:
                return _answer(plate_id, self.sliced[plate_id])
        used = (
            None
            if plate_id is None or self.plate_slots is None or plate_id not in self.plate_slots
            else set(self.plate_slots[plate_id])
        )
        return _answer(
            plate_id,
            [
                filament.model_copy(
                    update={"used_in_plate": used is None or filament.slot_id in used}
                )
                for filament in self.project
            ],
        )


def _answer(plate_id: int | None, filaments: list[FilamentRequirement]) -> FilamentRequirements:
    return FilamentRequirements(plate_id=plate_id, filaments=filaments)


def _metadata(node: ET.Element) -> dict[str, str]:
    return {m.get("key", ""): m.get("value", "") for m in node.findall("metadata")}


def _number(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element | None:
    if name not in archive.namelist():
        return None
    try:
        return ET.fromstring(_read_capped(archive, name, MAX_SETTINGS_BYTES * 16))
    except ET.ParseError:
        raise UnreadableFilamentsError(f"{name} is not readable XML") from None


def _project(archive: zipfile.ZipFile) -> tuple[list[FilamentRequirement], dict[str, Any]]:
    if PROJECT_SETTINGS_NAME not in archive.namelist():
        return [], {}
    try:
        settings = json.loads(_read_capped(archive, PROJECT_SETTINGS_NAME, MAX_SETTINGS_BYTES))
    except ValueError:
        raise UnreadableFilamentsError(f"{PROJECT_SETTINGS_NAME} is not readable JSON") from None
    if not isinstance(settings, dict):
        return [], {}
    types = settings.get("filament_type")
    colours = settings.get("filament_colour")
    types = types if isinstance(types, list) else []
    colours = colours if isinstance(colours, list) else []

    def text(values: list[Any], index: int) -> str:
        value = values[index] if index < len(values) else ""
        return value if isinstance(value, str) else ""

    return [
        FilamentRequirement(slot_id=index + 1, type=text(types, index), color=text(colours, index))
        for index in range(max(len(types), len(colours)))
    ], settings


def _sliced(root: ET.Element | None) -> dict[int, list[FilamentRequirement]]:
    plates: dict[int, list[FilamentRequirement]] = {}
    for plate in root.findall("plate") if root is not None else []:
        index = _number(_metadata(plate).get("index"))
        if index is None:
            continue
        for node in plate.findall("filament"):
            slot = _number(node.get("id"))
            try:
                grams = float(node.get("used_g", "0"))
            except ValueError:
                continue
            if slot is None or grams <= 0:
                continue
            plates.setdefault(index, []).append(
                FilamentRequirement(
                    slot_id=slot,
                    type=node.get("type", ""),
                    color=node.get("color", ""),
                    used_grams=round(grams, 1),
                )
            )
    return {index: sorted(found, key=lambda f: f.slot_id) for index, found in plates.items()}


def _diameters(value: str | None) -> list[str]:
    return [part for part in (value or "").replace(",", " ").split() if part]


def _first(value: Any) -> str | None:
    """A project setting that is a string, or a list of them, as one string."""
    if isinstance(value, list):
        value = value[0] if value else None
    return value if isinstance(value, str) and value else None


def _profile(
    archive: zipfile.ZipFile, root: ET.Element | None, settings: dict[str, Any]
) -> SlicedProfile:
    with_gcode = {
        index
        for name in archive.namelist()
        if _GCODE.fullmatch(name) and (index := _number(name[_PLATE_PREFIX:_GCODE_SUFFIX]))
    }
    plates: dict[int, SlicedPlate] = {}
    for plate in root.findall("plate") if root is not None else []:
        meta = _metadata(plate)
        index = _number(meta.get("index"))
        if index is None or index not in with_gcode:
            continue
        try:
            weight: float | None = round(float(meta.get("weight", "")), 2)
        except ValueError:
            weight = None
        plates[index] = SlicedPlate(
            index=index,
            print_seconds=_number(meta.get("prediction")),
            weight_g=weight,
            nozzle_diameters=_diameters(meta.get("nozzle_diameters")),
        )
    for index in with_gcode - plates.keys():
        plates[index] = SlicedPlate(index=index)
    nozzles = settings.get("nozzle_diameter")
    project = [value for value in (nozzles if isinstance(nozzles, list) else [nozzles]) if value]
    profile = SlicedProfile(
        printer_model=_first(settings.get("printer_model")),
        printer_preset=_first(settings.get("printer_settings_id")),
        process=_first(settings.get("print_settings_id")),
        bed_type=_first(settings.get("curr_bed_type")),
        nozzle_diameters=[str(value) for value in project if isinstance(value, (str, int, float))],
        plates=[plates[index] for index in sorted(plates)],
    )
    profile.nozzle_diameters = profile.diameters_for([p.index for p in profile.plates]) or [
        size for value in profile.nozzle_diameters if (size := diameter(value)) is not None
    ]
    return profile


def _painted(archive: zipfile.ZipFile, budget: ReadBudget) -> bool | None:
    """Whether any model file paints a triangle; ``None`` when that would inflate more
    than ``budget`` allows the model files."""
    names = [name for name in archive.namelist() if name.lower().endswith(".model")]
    allowed = budget.model_bytes()
    if allowed is not None and sum(archive.getinfo(n).file_size for n in names) > allowed:
        return None
    for name in names:
        tail = b""
        with archive.open(name) as entry:
            while chunk := entry.read(_CHUNK):
                if _PAINT in tail + chunk:
                    return True
                tail = chunk[-len(_PAINT) :]
    return False


def _plate_slots(
    archive: zipfile.ZipFile,
    config: ET.Element | None,
    settings: dict[str, Any],
    budget: ReadBudget,
) -> dict[int, list[int]] | None:
    if config is None or config.find(".//layer_config_ranges") is not None:
        return None
    if _painted(archive, budget) is not False:
        return None
    objects: dict[str, set[int]] = {}
    for obj in config.findall("object"):
        own = _number(_metadata(obj).get("extruder")) or 1
        slots = {own}
        for part in obj.findall("part"):
            if (part.get("subtype") or "normal_part") in _NO_FILAMENT:
                continue
            slots.add(_number(_metadata(part).get("extruder")) or own)
        objects[obj.get("id", "")] = slots
    support: set[int] = set()
    for key in _SUPPORT_FILAMENTS:
        named = settings.get(key)
        for value in named if isinstance(named, list) else [named]:
            if isinstance(value, str) and (number := _number(value)):
                support.add(number)
    plates: dict[int, list[int]] = {}
    for plate in config.findall("plate"):
        index = _number(_metadata(plate).get("plater_id"))
        if index is None:
            continue
        used: set[int] = set()
        for instance in plate.findall("model_instance"):
            found = objects.get(_metadata(instance).get("object_id", ""))
            if found is None:
                return None  # a plate placing an object the file does not describe
            used |= found
        if used:
            used |= support
        plates[index] = sorted(used)
    return plates or None


def read_filaments(
    payload: BinaryIO, budget: ReadBudget | None = None, *, sliced: bool = False
) -> LocalFilaments:
    """``payload``'s filament slots, as :class:`LocalFilaments`. A file that is not a
    3MF, or whose settings cannot be read, is :class:`UnreadableFilamentsError`; so is
    one with G-code in it unless ``sliced`` says it is a sliced file (#2671), which is
    then read with its :class:`SlicedProfile`, and must hold G-code."""
    try:
        with zipfile.ZipFile(payload) as archive:
            has_gcode = any(_GCODE.fullmatch(name) for name in archive.namelist())
            if has_gcode != sliced:
                raise UnreadableFilamentsError(
                    "the file is sliced already" if has_gcode else "the file holds no G-code"
                )
            project, settings = _project(archive)
            info = _xml(archive, SLICE_INFO_NAME)
            used = _sliced(info)
            profile = _profile(archive, info, settings) if sliced else None
            plate_slots = (
                None
                if used or sliced
                else _plate_slots(
                    archive, _xml(archive, MODEL_SETTINGS_NAME), settings, budget or ReadBudget()
                )
            )
    except zipfile.BadZipFile:
        raise UnreadableFilamentsError("the file is not a 3MF archive") from None
    except ArchiveTooLargeError as error:
        raise UnreadableFilamentsError(str(error)) from None
    except (KeyError, OSError, EOFError) as error:
        reason = type(error).__name__
        raise UnreadableFilamentsError(f"the 3MF could not be read ({reason})") from None
    return LocalFilaments(project=project, sliced=used, plate_slots=plate_slots, profile=profile)
