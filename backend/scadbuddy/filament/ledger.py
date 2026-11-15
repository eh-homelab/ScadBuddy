"""The spool ledger (#6261, spec §5.5): `filament_spool_events` is the record of what
happened to a spool, and the spool's derived columns (``grams_left``, ``last_*``) are
recomputed from it in the transaction that writes each event (`record`).

The grams-left rule (`grams_left`): the newest absolute reading is the anchor, and the
deltas of every later event are taken off it. An absolute reading is an event with a
``source`` of ``scale``, ``person`` or ``bb`` and its weight in ``grams_after``, or an
``ams_remain`` reading (``grams_after``, else ``data.remain_pct`` of the label weight).
An ``ams_remain`` reading is an anchor only when it is newer than the newest ``scale``
or ``person`` reading and the spool's weight is not locked. A delta is the ``delta_g``
of a ``print_actual`` event, or of a ``print_estimate`` event while no ``print_ended``
event has its ``ref_id``: a print's actual replaces its estimate, and a print that ended
with no actual known (``delta_g`` null) drops its estimate.

The order is when things physically happened, so a print two sources both booked is
counted once (§8.2, "Consumption is counted once"). An event that names its time (``at``)
keeps it: a BB weighing or drying at BB's time, BB's state (``synced_from_bb``) at the BB
row's ``updated_at``, a print's ``print_ended`` at its plate's start (`spools/native.py`
`settle_uses_in`). A reading taken after a plate started is therefore read as holding that
plate, and the plate's actual is not taken off it again. An event with no ``at`` is
booked now: the clock's time, but never earlier than a microsecond after the spool's
newest event, so a clock that steps back (WSL's does) cannot put an event before the one
it follows. That guard only moves an event booked now, never a named time, so it never
pushes a physical time past a newer anchor. Two events of one time keep the order they
were booked in (``seq``), as a pull's ``created`` and ``synced_from_bb`` at one BB time.

`refresh_derived` reads a spool's whole history for each event. That is cheap at a few
events a print; #6263's AMS readings, one per tick, will need an incremental path.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic_core import to_jsonable_python

from scadbuddy.filament import models as m

Conn = Connection[DictRow]

_COLUMNS = ", ".join(m.SpoolEvent.model_fields)
_ABSOLUTE = frozenset({"scale", "person", "bb", "ams_remain"})
_MANUAL = frozenset({"scale", "person"})
_DELTA = frozenset({"print_actual", "print_estimate"})
_USED = frozenset({"print_started", "print_ended"})


def record(
    conn: Conn,
    spool_id: UUID,
    kind: m.EventKind,
    *,
    source: m.EventSource | None = None,
    delta_g: float | None = None,
    absolute_g: float | None = None,
    ref_kind: str | None = None,
    ref_id: str | None = None,
    actor: str | None = None,
    printer: UUID | None = None,
    ams_id: int | None = None,
    tray_id: int | None = None,
    at: datetime | None = None,
    data: dict[str, Any] | None = None,
) -> m.SpoolEvent | None:
    """Write one event and refresh the spool's derived columns, on ``conn``'s
    transaction. The spool's row is locked first, so two writers of one spool take it in
    turn and each reads the other's events. ``absolute_g`` is a reading
    (``grams_after``); ``grams_before`` is the spool's ``grams_left`` when the event is
    booked, also for an event that names an earlier time. ``None`` when an event with
    the spool, ``ref_kind``, ``ref_id`` and ``kind`` is already recorded (a retry books
    nothing). ``at`` is when it physically happened; with none the event is booked
    now, by the module's rule."""
    # Before the insert, whose foreign key would take a KEY SHARE lock that
    # `refresh_derived`'s FOR UPDATE then has to upgrade: two writers deadlock there.
    conn.execute("SELECT 1 FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,))
    row = conn.execute(
        "INSERT INTO filament_spool_events (spool_id, at, kind, source, printer_id, ams_id,"
        " tray_id, grams_before, grams_after, delta_g, ref_kind, ref_id, actor, data)"
        " VALUES (%s, coalesce(%s, greatest(clock_timestamp(), (SELECT max(at)"
        " + interval '1 microsecond' FROM filament_spool_events WHERE spool_id = %s))),"
        " %s, %s, %s, %s, %s,"
        " (SELECT grams_left FROM filament_spools WHERE id = %s), %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (spool_id, ref_kind, ref_id, kind) WHERE ref_id IS NOT NULL DO NOTHING"
        f" RETURNING {_COLUMNS}",
        (
            spool_id,
            at,
            spool_id,
            kind,
            source,
            printer,
            ams_id,
            tray_id,
            spool_id,
            absolute_g,
            delta_g,
            ref_kind,
            ref_id,
            actor,
            Jsonb(to_jsonable_python(data or {})),
        ),
    ).fetchone()
    if row is None:
        return None
    refresh_derived(conn, spool_id)
    return m.SpoolEvent.model_validate(row)


def _reading(event: m.SpoolEvent, label_weight_g: int | None) -> float | None:
    if event.source not in _ABSOLUTE:
        return None
    if event.grams_after is not None:
        return event.grams_after
    pct = event.data.get("remain_pct") if event.source == "ams_remain" else None
    if isinstance(pct, int | float) and label_weight_g is not None:
        return pct * label_weight_g / 100
    return None


def grams_left(
    events: Sequence[m.SpoolEvent], *, weight_locked: bool, label_weight_g: int | None
) -> float | None:
    """The grams left on a spool by the rule above; ``None`` with no reading to anchor
    on. Never below zero. Events of one time keep the order they were booked in (``seq``)."""
    ordered = sorted(events, key=lambda e: (e.at, e.seq))
    readings = [_reading(e, label_weight_g) for e in ordered]
    manual = max(
        (i for i, e in enumerate(ordered) if e.source in _MANUAL and readings[i] is not None),
        default=-1,
    )
    anchor = -1
    for i, event in enumerate(ordered):
        if readings[i] is None:
            continue
        if event.source == "ams_remain" and (weight_locked or i < manual):
            continue
        anchor = i
    if anchor < 0:
        return None
    ended = {(e.ref_kind, e.ref_id) for e in ordered if e.kind == "print_ended" and e.ref_id}
    value = readings[anchor]
    assert value is not None
    for event in ordered[anchor + 1 :]:
        if event.source not in _DELTA or event.delta_g is None:
            continue
        if event.source == "print_estimate" and (event.ref_kind, event.ref_id) in ended:
            continue
        value += event.delta_g
    return max(value, 0.0)


def events(conn: Conn, spool_id: UUID) -> list[m.SpoolEvent]:
    """A spool's events, oldest first; two of one time in the order they were booked."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM filament_spool_events WHERE spool_id = %s ORDER BY at, seq",
        (spool_id,),
    ).fetchall()
    return [m.SpoolEvent.model_validate(row) for row in rows]


def refresh_derived(conn: Conn, spool_id: UUID) -> None:
    """Recompute the spool's ``grams_left`` and ``last_*`` columns from its events. The
    label weight is the spool's own, else its type's. The spool's row is locked (`record`
    has locked it already), so two transactions that each record an event read each
    other's in turn."""
    spool = conn.execute(
        "SELECT s.weight_locked, coalesce(s.label_weight_g, t.label_weight_g) AS label_weight_g"
        " FROM filament_spools s LEFT JOIN filament_spool_types t ON t.id = s.type_id"
        " WHERE s.id = %s FOR UPDATE OF s",
        (spool_id,),
    ).fetchone()
    if spool is None:
        return
    history = events(conn, spool_id)
    weighed = [
        e
        for e in history
        if (e.kind == "weighed" or e.source == "scale") and e.grams_after is not None
    ]
    dried = [e for e in history if e.kind == "dried"]
    used = [e for e in history if e.kind in _USED]
    last_weighed = weighed[-1] if weighed else None
    last_dried = dried[-1] if dried else None
    conn.execute(
        "UPDATE filament_spools SET grams_left = %s, last_weighed_g = %s, last_weighed_at = %s,"
        " last_dried_at = %s, last_dried_temp_c = %s, last_dried_hours = %s, last_used_at = %s"
        " WHERE id = %s",
        (
            grams_left(
                history,
                weight_locked=spool["weight_locked"],
                label_weight_g=spool["label_weight_g"],
            ),
            last_weighed.grams_after if last_weighed else None,
            last_weighed.at if last_weighed else None,
            last_dried.at if last_dried else None,
            last_dried.data.get("temp_c") if last_dried else None,
            last_dried.data.get("hours") if last_dried else None,
            used[-1].at if used else None,
            spool_id,
        ),
    )
