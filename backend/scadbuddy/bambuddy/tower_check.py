"""The prime tower judged before a slice (#6758, #3770 P3).

The send moves an author's tower that stands on its objects, or that an extruder cannot
reach (:func:`~scadbuddy.bambuddy.send.towers_within_reach`, #6691). It judges the tower
by the file's own settings. The print dialog can choose other settings: "No sparse
layers" on, a rib wall off, another width. Then the slicer can still refuse the plate
with "Object conflicts were detected" (#6734). This module judges the tower by the
settings the print chose, on the file as the send lays it out, with the same parts the
send uses (:func:`~scadbuddy.render.plate.tower_footprint`,
:func:`~scadbuddy.render.plate.tower_beside`, ``send._tower_on``). The dialog's check
and the agent's ``print_output`` read the answer, and the fix is a generic process
override, ``wipe_tower_x``/``wipe_tower_y`` (#2998), one value per plate as Bambu Studio
stores them. The slicer's CLI writes each key of the process preset over the file's
(``update_full_config`` in ``src/BambuStudio.cpp`` at v02.08.02.61,
https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/BambuStudio.cpp#L3132-L3194),
so those two keys replace the file's tower places.
"""

from __future__ import annotations

import dataclasses
import io
import json
import math
import zipfile
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.prime_tower import PrimeTowerChoice, tower_asked
from scadbuddy.bambuddy.send import (
    Target,
    _file_tower,
    _laid_out_for,
    _source_bed,
    _tower_on,
    _tower_value,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.render.bambu3mf import (
    PROJECT_SETTINGS_NAME,
    layout_of,
    plate_origin,
    stated_tower_off,
)
from scadbuddy.render.objects3mf import UnreadableObjectsError, plate_boxes
from scadbuddy.render.plate import (
    PlateGeometry,
    Rect,
    TowerSettings,
    tower_beside,
    tower_footprint,
    tower_reaches,
)

#: Why a tower conflicts: it overlaps the objects (with its brim), it stands inside the
#: "No sparse layers" keep-out of them, or not every extruder reaches it.
ConflictReason = Literal["on-objects", "keep-out", "unreachable"]

#: The process keys the fix writes. Bambu Studio's per-plate ``coFloats``.
TOWER_PLACE_KEYS: tuple[str, str] = ("wipe_tower_x", "wipe_tower_y")


class TowerConflict(BaseModel):
    """One plate whose prime tower the slicer would refuse."""

    plate_id: int
    reason: ConflictReason
    #: The tower's ``wipe_tower_x``/``_y`` now.
    x: float
    y: float
    #: A free spot for it (:func:`~scadbuddy.render.plate.tower_beside`), or ``None``
    #: when no edge of the plate has room.
    move_x: float | None = None
    move_y: float | None = None
    message: str


class TowerCheck(BaseModel):
    """The prime tower on each plate the print slices, judged before the slice."""

    conflicts: list[TowerConflict] = Field(default_factory=list)
    #: The process overrides that move each conflicting tower to its free spot and keep
    #: the other plates' towers where they are: ``wipe_tower_x`` and ``wipe_tower_y``, one
    #: value per plate. ``None`` when no tower conflicts or none can move.
    fix: dict[str, list[str]] | None = None


def _first(value: object) -> object:
    """A setting's value, a list read by its first value."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _flag(value: object) -> bool | None:
    value = _first(value)
    if value is None:
        return None
    return str(value) not in ("0", "false", "False")


def _width(value: object) -> float | None:
    try:
        width = float(str(_first(value)))
    except ValueError:
        return None
    return width if math.isfinite(width) and width > 0 else None


def chosen_tower(
    stated: TowerSettings,
    choice: PrimeTowerChoice | None,
    overrides: Mapping[str, object],
    template: Mapping[str, object],
) -> TowerSettings:
    """The tower the slice builds: ``stated`` (the file's, ``send._file_tower``) with the
    template's ``print_settings`` over it, then the print's ``choice``, then its process
    overrides, as the slice lays them over each other (#2998: an explicit override wins)."""
    no_sparse = stated.no_sparse_layers
    rib_wall = stated.rib_wall
    width = stated.width
    for layer in (template, overrides):
        if layer is overrides and choice is not None and choice.no_sparse_layers is not None:
            no_sparse = choice.no_sparse_layers
        stated_no_sparse = _flag(layer.get("wipe_tower_no_sparse_layers"))
        no_sparse = stated_no_sparse if stated_no_sparse is not None else no_sparse
        rib = _flag(layer.get("prime_tower_rib_wall"))
        rib_wall = rib if rib is not None else rib_wall
        width = _width(layer.get("prime_tower_width")) or width
    return dataclasses.replace(stated, no_sparse_layers=no_sparse, rib_wall=rib_wall, width=width)


def _places(
    settings: Mapping[str, object], overrides: Mapping[str, object]
) -> list[tuple[float, float]] | None:
    """Each plate's ``wipe_tower_x``/``_y``: the file's, with the overrides' over them. An
    override shorter than the file reads past its end as its first value, as Bambu
    Studio's ``get_at`` does. ``None`` when the file states no tower place."""
    xs, ys = settings.get("wipe_tower_x"), settings.get("wipe_tower_y")
    if not isinstance(xs, list) or not isinstance(ys, list) or len(xs) != len(ys) or not xs:
        return None
    try:
        places = [(float(x), float(y)) for x, y in zip(xs, ys, strict=True)]
        for axis, key in enumerate(TOWER_PLACE_KEYS):
            given = overrides.get(key)
            if given is None:
                continue
            values = [float(str(v)) for v in (given if isinstance(given, list) else [given])]
            if not values:
                continue
            for index, place in enumerate(places):
                value = values[index] if index < len(values) else values[0]
                places[index] = (value, place[1]) if axis == 0 else (place[0], value)
    except (TypeError, ValueError):
        return None
    return places


def _message(plate_id: int, reason: ConflictReason, keep_out: float, move: object) -> str:
    what = {
        "on-objects": "the prime tower stands on the objects",
        "keep-out": (
            f'the prime tower is closer to the objects than the {keep_out:.0f} mm "No sparse'
            ' layers" needs'
        ),
        "unreachable": "the prime tower is where not every extruder reaches",
    }[reason]
    fix = (
        "Move the prime tower to a free spot."
        if move is not None
        else 'No free spot on this plate fits the tower. Turn off "No sparse layers", or'
        " move the objects."
    )
    return f"Plate {plate_id}: {what}. {fix}"


def judge_towers(
    laid_out: bytes,
    plate: PlateGeometry,
    asked: TowerSettings | None,
    overrides: Mapping[str, object],
    plates: Sequence[int] | None,
) -> TowerCheck:
    """Each tower of ``laid_out`` (a file as the send lays it out for ``plate``), judged
    as :func:`~scadbuddy.bambuddy.send.towers_within_reach` judges one, but at the size
    and keep-out ``asked`` gives (``None``: the file's own, ``send._file_tower``) and at
    the place ``overrides`` give. Only ``plates`` (``None``: all) are judged. A file whose
    objects or tower places cannot be read has no conflicts: the slicer then says why."""
    with zipfile.ZipFile(io.BytesIO(laid_out)) as archive:
        settings = json.loads(archive.read(PROJECT_SETTINGS_NAME))
    places = _places(settings, overrides)
    bed = _source_bed(settings)
    if places is None or bed is None:
        return TowerCheck()
    tower = asked or _file_tower(settings)
    try:
        read = plate_boxes(laid_out)
    except UnreadableObjectsError:
        return TowerCheck()
    source = PlateGeometry(
        model=None, size=bed, usable=Rect(0, 0, *bed), exclusions=(), extruders=1, height=0
    )
    moved = list(places)
    conflicts: list[TowerConflict] = []
    for index, place in enumerate(places, start=1):
        if index > read.count or index not in read.boxes:
            continue
        if plates is not None and index not in plates:
            continue
        low, high = read.boxes[index]
        height = float(high[2] - low[2])
        ox, oy = plate_origin(index, read.count, source)
        objects = Rect(
            float(low[0] - ox), float(low[1] - oy), float(high[0] - ox), float(high[1] - oy)
        )
        footprint = tower_footprint(plate, height, tower)
        reason: ConflictReason
        if not tower_reaches(place, plate, height, tower):
            reason = "unreachable"
        elif _tower_on(place, dataclasses.replace(footprint, keep_out=0.0), objects):
            reason = "on-objects"
        elif _tower_on(place, footprint, objects):
            reason = "keep-out"
        else:
            continue
        spot = tower_beside(objects, plate, height, tower)
        if spot is not None:
            moved[index - 1] = spot
        conflicts.append(
            TowerConflict(
                plate_id=index,
                reason=reason,
                x=place[0],
                y=place[1],
                move_x=spot[0] if spot is not None else None,
                move_y=spot[1] if spot is not None else None,
                message=_message(index, reason, footprint.keep_out, spot),
            )
        )
    fix = (
        {
            "wipe_tower_x": [_tower_value(x) for x, _ in moved],
            "wipe_tower_y": [_tower_value(y) for _, y in moved],
        }
        if moved != places
        else None
    )
    return TowerCheck(conflicts=conflicts, fix=fix)


def tower_check(
    payload: bytes,
    plate: PlateGeometry,
    *,
    choice: PrimeTowerChoice | None,
    overrides: Mapping[str, object],
    template: Mapping[str, object],
    plates: Sequence[int] | None,
) -> TowerCheck | None:
    """The prime tower of the file ``payload`` for a print on ``plate`` with the chosen
    tower (``choice``, the request's process ``overrides`` and the template's
    ``print_settings``), judged before the slice. The file is laid out as the send lays
    it out (``send._laid_out_for``), so a tower the send moves is judged where it goes.

    ``None`` when there is no tower to judge: no printer chosen, the print turns the
    tower off (or leaves off a file that states it off), or the file is printed as it is
    or does not fit the plate (the plate-fit check says that)."""
    if plate.model is None or layout_of(payload) == "as_is":
        return None
    asked = tower_asked(choice, overrides)
    if asked is None and _flag(template.get("enable_prime_tower")) is False:
        asked = False
    stated_off = stated_tower_off(payload)
    if asked is False or (asked is None and stated_off):
        return None
    try:
        laid = _laid_out_for(payload, Target(plate, tower_on=stated_off and asked is True))
    except ApiError:
        return None  # it does not fit: the plate-fit check and the run say why
    with zipfile.ZipFile(io.BytesIO(laid)) as archive:
        stated = _file_tower(json.loads(archive.read(PROJECT_SETTINGS_NAME)))
    return judge_towers(
        laid, plate, chosen_tower(stated, choice, overrides, template), overrides, plates
    )
