"""Proposed spool types for the spools that need one (#6701, spec §8.1: "else SB proposes a
new type for a person to confirm").

`proposals` groups the open (unarchived) spools with no type by brand, material, subtype,
RGBA, label weight and the ``material_number`` of their newest Bambuddy reading (the
``data.bb`` of their last ``synced_from_bb`` event; none for a spool with no link, and none
for one longer than 128 characters, which a cut would make collide: R-6729-3). A group with
no ``material_number`` folds into the one group of its other key fields that has one, when
exactly one does; else it stays apart (R-6729-1). A spool with no material, RGBA or label
weight is in no group; it is listed with what it lacks.
Each group proposes a code, named as types are named today: the ``material_number`` when
there is one (§8.1: it carries the type code, and the pull matches by it first), else
``BRAND-MATERIAL-SUBTYPE-COLOUR`` in upper case, with the colour name, else the hex (the
price book's "Make a type", `frontend/src/components/filament/makeType.ts` `typeCodeOf`).
A code another type or an earlier group holds takes ``-<label weight>G``, then ``-2``,
``-3`` ... A group whose key a type matches now names that type (`_match`): the type its
``material_number`` names when that type is of the group's key, else the type of its
identity. Groups can share a ``material_number`` and differ in colour or weight; the code
alone never puts one group on another's type (R-6701-8).

`confirm` takes the groups a person picked, each with its code, in one transaction. Per
group it re-checks `_match` and uses a type that matches now; else it makes the type:
the key, the colour name most of its spools have, and each other value every one of them
shares (category, effect, extra colours, diameter, density, temperatures, insert type; no
cost, so no price book row). Then each of the group's spools that is still open, untyped
and of the key gets the type, loses each override equal to the type's value and gets one
``edited`` event (`FilamentSpoolStore.update_on`). A spool with no ``material_number`` is
of a numbered pick whose other key fields it shares only under the fold's rule: the open
untyped spools hold exactly one ``material_number`` for those fields, the pick's
(R-6729-5). Any other spool is skipped and reported. Per group the answer names the type
used, whether the group made it (``made``) and whether this request made it (``new``):
a group that joined a type an earlier group just made has ``new`` and not ``made``, so
its own code was not used (R-6729-1). A code an unarchived type holds (the
``filament_spool_types_code`` index) is `CatalogExistsError`, and nothing is written; any
other unique violation propagates (R-6729-2).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow

from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import CatalogExistsError, CatalogRefError
from scadbuddy.filament.colour import normalise_rgba
from scadbuddy.filament.component import Filament
from scadbuddy.filament.types import OVERRIDABLE

Conn = Connection[DictRow]
Row = Mapping[str, Any]

#: What a group shares, in `TypeProposal`'s terms.
KEY = ("brand_id", "material_id", "subtype_id", "rgba", "label_weight_g", "material_number")
#: The values a new type takes when every spool of its group shares one.
SHARED = (
    "category_id",
    "effect_id",
    "extra_colours",
    "diameter_mm",
    "density_g_cm3",
    "nozzle_temp_min",
    "nozzle_temp_max",
    "bed_temp_min",
    "bed_temp_max",
    "insert_type_id",
)
#: What a spool needs to be grouped, as `UngroupedSpool.missing` names it.
_NEEDED: tuple[tuple[m.ProposalField, str], ...] = (
    ("material", "material_id"),
    ("rgba", "rgba"),
    ("label_weight_g", "label_weight_g"),
)
_SPOOL_ROWS = (
    "SELECT s.id, s.number, s.type_id, s.archived_at, "
    + ", ".join(f"s.{c}" for c in OVERRIDABLE)
    + ", (SELECT e.data -> 'bb' ->> 'material_number' FROM filament_spool_events e"
    " WHERE e.spool_id = s.id AND e.kind = 'synced_from_bb' AND e.data ? 'bb'"
    " ORDER BY e.at DESC, e.seq DESC LIMIT 1) AS material_number FROM filament_spools s"
)
_CODE_MAX = 128
#: The unique index on a type's code: the one violation that is a name clash (R-6729-2).
_CODE_INDEX = "filament_spool_types_code"


def proposals(f: Filament) -> m.TypeProposals:
    """The proposed types, the largest group first, and the spools in no group."""
    groups: dict[tuple[Any, ...], list[Row]] = {}
    ungrouped: list[m.UngroupedSpool] = []
    with f.spools.pool.connection() as conn:
        rows = conn.execute(
            _SPOOL_ROWS + " WHERE s.type_id IS NULL AND s.archived_at IS NULL ORDER BY s.number"
        ).fetchall()
        for row in rows:
            missing = [name for name, column in _NEEDED if row[column] is None]
            if missing:
                ungrouped.append(
                    m.UngroupedSpool(spool_id=row["id"], number=row["number"], missing=missing)
                )
            else:
                groups.setdefault(_key(row), []).append(row)
        _fold(groups)
        taken = {
            r["code"].upper()
            for r in conn.execute(
                "SELECT code FROM filament_spool_types WHERE archived_at IS NULL"
            ).fetchall()
        }
        found: list[m.TypeProposal] = []
        for key, members in sorted(
            groups.items(), key=lambda kv: (-len(kv[1]), kv[1][0]["number"])
        ):
            want = dict(zip(KEY, key, strict=True))
            names = _names(conn, want)
            colour = _colour_name(members)
            kind = _match(conn, f, want, names)
            if kind is not None:
                code = kind.code
            else:
                code = _free(_base_code(want, names, colour), want["label_weight_g"], taken)
                taken.add(code.upper())
            found.append(
                m.TypeProposal(
                    code=code,
                    type_id=None if kind is None else kind.id,
                    **want,
                    **names,
                    colour_name=colour,
                    spool_ids=[r["id"] for r in members],
                    numbers=[r["number"] for r in members],
                    count=len(members),
                )
            )
    return m.TypeProposals(groups=found, ungrouped=ungrouped)


def confirm(f: Filament, body: m.TypeProposalConfirm, *, actor: str) -> m.TypeProposalResult:
    """Type each picked group's spools, as the module says, in one transaction."""
    wanted = sorted({spool_id for pick in body.groups for spool_id in pick.spool_ids})
    done: list[m.ConfirmedProposal] = []
    made: set[UUID] = set()
    try:
        with f.spools.transaction() as conn:
            # Every spool first, in id order, as the other writers of several spools lock.
            rows = {
                row["id"]: dict(row)
                for row in conn.execute(
                    _SPOOL_ROWS + " WHERE s.id = ANY(%s) ORDER BY s.id FOR UPDATE OF s",
                    (wanted,),
                ).fetchall()
            }
            numbered = _numbered(conn) if any(p.material_number for p in body.groups) else {}
            for pick in body.groups:
                done.append(_confirm_one(conn, f, pick, rows, actor, made, numbered))
    except errors.UniqueViolation as exc:
        if exc.diag.constraint_name != _CODE_INDEX:
            raise
        raise CatalogExistsError() from None
    except errors.ForeignKeyViolation:
        raise CatalogRefError() from None
    return m.TypeProposalResult(groups=done)


def _confirm_one(
    conn: Conn,
    f: Filament,
    pick: m.TypeProposalPick,
    rows: dict[UUID, dict[str, Any]],
    actor: str,
    made_here: set[UUID],
    numbered: Mapping[tuple[Any, ...], set[str]],
) -> m.ConfirmedProposal:
    want = {k: getattr(pick, k) for k in KEY}
    eligible: list[dict[str, Any]] = []
    skipped: list[m.SkippedSpool] = []
    for spool_id in dict.fromkeys(pick.spool_ids):
        row = rows.get(spool_id)
        reason: m.SkipReason | None = (
            "not_found"
            if row is None
            else "archived"
            if row["archived_at"] is not None
            else "typed"
            if row["type_id"] is not None
            else "changed"
            if not _of_pick(row, want, numbered)
            else None
        )
        if reason is not None:
            skipped.append(m.SkippedSpool(spool_id=spool_id, reason=reason))
        else:
            assert row is not None
            eligible.append(row)
    if not eligible:
        return m.ConfirmedProposal(
            code=pick.code, type_id=None, made=False, new=False, typed=[], skipped=skipped
        )
    kind = _match(conn, f, want, _names(conn, want))
    made = kind is None
    if kind is None:
        kind = f.types.create_on(conn, _type_body(pick, eligible))
        made_here.add(kind.id)
    for row in eligible:
        cleared = {
            c: None for c in OVERRIDABLE if row[c] is not None and row[c] == getattr(kind, c)
        }
        patch = m.SpoolPatch.model_validate({"type_id": kind.id, **cleared})
        f.spools.update_on(conn, row["id"], patch, actor=actor)
        # A later group naming this spool finds it typed.
        row["type_id"] = kind.id
    return m.ConfirmedProposal(
        code=kind.code,
        type_id=kind.id,
        made=made,
        new=kind.id in made_here,
        typed=[row["id"] for row in eligible],
        skipped=skipped,
    )


def _key(row: Row) -> tuple[Any, ...]:
    number = row["material_number"]
    if number is not None and len(number) > _CODE_MAX:
        number = None
    return (*(row[k] for k in KEY[:-1]), number)


def _fold(groups: dict[tuple[Any, ...], list[Row]]) -> None:
    """Fold each group with no ``material_number`` into the one group of its other key
    fields that has one, when exactly one does (R-6729-1)."""
    numbered: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
    for key in groups:
        if key[-1] is not None:
            numbered.setdefault(key[:-1], []).append(key)
    for key in [k for k in groups if k[-1] is None]:
        into = numbered.get(key[:-1], [])
        if len(into) == 1:
            members = groups[into[0]] + groups.pop(key)
            groups[into[0]] = sorted(members, key=lambda r: r["number"])


def _numbered(conn: Conn) -> dict[tuple[Any, ...], set[str]]:
    """The ``material_number``s the open untyped spools hold, by their other key fields."""
    found: dict[tuple[Any, ...], set[str]] = {}
    for row in conn.execute(
        _SPOOL_ROWS + " WHERE s.type_id IS NULL AND s.archived_at IS NULL"
    ).fetchall():
        key = _key(row)
        if key[-1] is not None:
            found.setdefault(key[:-1], set()).add(key[-1])
    return found


def _of_pick(
    row: Row, want: Mapping[str, Any], numbered: Mapping[tuple[Any, ...], set[str]]
) -> bool:
    """Whether a spool is of a pick's key. One with no ``material_number`` is of a numbered
    pick whose other key fields it shares only under the fold's rule: the pick's number is
    the one the open untyped spools hold for those fields (R-6729-5)."""
    key = _key(row)
    if key[-1] is None and want["material_number"] is not None:
        if numbered.get(key[:-1]) != {want["material_number"]}:
            return False
        key = (*key[:-1], want["material_number"])
    return dict(zip(KEY, key, strict=True)) == want


def _names(conn: Conn, want: Mapping[str, Any]) -> dict[str, Any]:
    names = conn.execute(
        "SELECT (SELECT name FROM filament_brands WHERE id = %s) AS brand,"
        " (SELECT name FROM filament_materials WHERE id = %s) AS material,"
        " (SELECT name FROM filament_subtypes WHERE id = %s) AS subtype",
        (want["brand_id"], want["material_id"], want["subtype_id"]),
    ).fetchone()
    assert names is not None
    return dict(names)


def _match(
    conn: Conn, f: Filament, want: Mapping[str, Any], names: Mapping[str, Any]
) -> m.SpoolType | None:
    """The type the group's key names now. The type its ``material_number`` names counts
    only when that type is of the key (R-6701-8): `types.match` matches by code alone, and
    groups that differ in colour or weight can share a ``material_number``."""
    identity = {"brand": names["brand"], "material": names["material"], "subtype": names["subtype"]}
    if want["material_number"] is not None:
        kind = f.types.match(
            code=want["material_number"], rgba="#000000", label_weight_g=0, conn=conn, **identity
        )
        if kind is not None and _of_key(kind, want):
            return kind
    return f.types.match(
        rgba=want["rgba"], label_weight_g=want["label_weight_g"], conn=conn, **identity
    )


def _of_key(kind: m.SpoolType, want: Mapping[str, Any]) -> bool:
    return (
        kind.brand_id == want["brand_id"]
        and kind.material_id == want["material_id"]
        and kind.subtype_id == want["subtype_id"]
        and normalise_rgba(kind.rgba) == normalise_rgba(want["rgba"])
        and kind.label_weight_g == want["label_weight_g"]
    )


def _colour_name(rows: Iterable[Row]) -> str | None:
    """The colour name most of ``rows`` have; the first one's on a tie."""
    counted = Counter(r["colour_name"] for r in rows if r["colour_name"])
    return counted.most_common(1)[0][0] if counted else None


def _base_code(want: Mapping[str, Any], names: Mapping[str, Any], colour: str | None) -> str:
    if want["material_number"]:
        return str(want["material_number"])[:_CODE_MAX]
    rgba: str = want["rgba"]
    hex_ = rgba[1:7] if rgba.endswith("FF") else rgba[1:]
    parts = [names["brand"], names["material"], names["subtype"], colour or hex_]
    joined = "-".join(p for p in parts if p).upper()
    return re.sub(r"[^A-Z0-9]+", "-", joined).strip("-")[:_CODE_MAX]


def _free(base: str, label_weight_g: int, taken: set[str]) -> str:
    """``base``, else with the label weight, else with a number: one no type holds."""
    if base.upper() not in taken:
        return base
    weighed = f"{base[: _CODE_MAX - 12]}-{label_weight_g}G"
    code, n = weighed, 2
    while code.upper() in taken:
        code, n = f"{weighed}-{n}", n + 1
    return code


def _type_body(pick: m.TypeProposalPick, rows: list[dict[str, Any]]) -> m.SpoolTypeCreate:
    values: dict[str, Any] = {k: getattr(pick, k) for k in KEY if k != "material_number"}
    values |= {"code": pick.code, "colour_name": _colour_name(rows)}
    for column in SHARED:
        first = rows[0][column]
        if first is not None and all(r[column] == first for r in rows):
            values[column] = first
    return m.SpoolTypeCreate.model_validate(values)
