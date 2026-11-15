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

The order is when things physically happened (§8.2, "Consumption is counted once"). An
event that names its time (``at``) keeps it: a BB weighing or drying at BB's time, BB's
state (``synced_from_bb``) at the BB row's ``updated_at``, a BB usage row at its
``created_at``, a print's ``print_started`` when the printer takes the plate and its
``print_ended`` at the report that shows the plate's timed end. An event with no ``at``
is booked now: the clock's time, but never earlier than a microsecond after the spool's
newest event, so a clock that steps back (WSL's does) cannot put an event before the one
it follows. That guard only moves an event booked now, never a named time, so it never
pushes a physical time past a newer anchor. Three rules (#6263, spec §5.5):

1. At one instant every delta comes before every reading, and events of one kind keep
   the order they are given in (`events` gives them in insert order, ``seq``). A reading
   taken as a plate ends holds that plate. BB writes a usage row and its spool's
   ``updated_at`` at one instant, so a BB reading holds its own usage row.
2. An ``ams_remain`` reading with ``data.printing`` true is recorded but is never an
   anchor: the AMS remain % moves in steps during a plate.
3. One plate is counted once. SB's events (``ref_kind`` `PRINT_RUN`) and BB's usage row
   (`BB_USAGE`) of one job (`job_of`) on one spool are a pair. For a pair, SB's events
   are skipped and BB's ``delta_g`` counts, because BB keeps a failed plate's partial
   grams and SB books 0 g. The pair counts when it lies after the anchor, and each
   anchor is compared in its own clock: an anchor from BB (`BB_REFS`) with BB's usage
   time, any other anchor with SB's time of the plate (its newest end, else its start).
   So the skew between BB's clock and SB's cannot count a plate twice or drop it.

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

#: The ``ref_kind`` of SB's plate events (``ref_id`` ``run/subtask/slot``).
PRINT_RUN = "print_run"
#: The ``ref_kind`` of a BB usage row booked on the spool.
BB_USAGE = "bb_usage"
#: The ``ref_kind``s of the events that carry BB's clock.
BB_REFS = frozenset({"bb_spool", "bb_scale", "bb_dried", BB_USAGE})


def job_of(event: m.SpoolEvent) -> str | None:
    """The job an event belongs to: ``data.job``, else for a `PRINT_RUN` event from
    before #6263 the subtask in its ``ref_id`` (``run/subtask/slot``)."""
    job = event.data.get("job")
    if isinstance(job, str):
        return job
    if event.ref_kind == PRINT_RUN and event.ref_id:
        parts = event.ref_id.split("/")
        if len(parts) >= 3:
            return "/".join(parts[1:-1])
    return None


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
    on. Never below zero. At one time, deltas come first and events of one kind keep the
    order they are given in (rule 1)."""
    # sorted() is stable, so the given order breaks the remaining ties.
    ordered = sorted(events, key=lambda e: (e.at, 0 if e.source in _DELTA else 1))
    readings = [_reading(e, label_weight_g) for e in ordered]
    manual = max(
        (i for i, e in enumerate(ordered) if e.source in _MANUAL and readings[i] is not None),
        default=-1,
    )
    anchor = -1
    for i, event in enumerate(ordered):
        if readings[i] is None:
            continue
        if event.source == "ams_remain" and (
            weight_locked or i < manual or event.data.get("printing") is True
        ):
            continue
        anchor = i
    if anchor < 0:
        return None
    # Rule 3: a job with a BB usage row and an SB plate event on this spool is a pair.
    # `plate` is SB's position of the plate per job: its newest end, else its newest start.
    used = {job_of(e) for e in ordered if e.ref_kind == BB_USAGE and e.kind == "print_ended"}
    ends: dict[str, int] = {}
    starts: dict[str, int] = {}
    for i, event in enumerate(ordered):
        job = job_of(event)
        if event.ref_kind == PRINT_RUN and job in used and job is not None:
            if event.kind == "print_ended":
                ends[job] = i
            elif event.kind == "print_started":
                starts[job] = i
    plate = starts | ends
    bb_anchor = ordered[anchor].ref_kind in BB_REFS
    ended = {(e.ref_kind, e.ref_id) for e in ordered if e.kind == "print_ended" and e.ref_id}
    value = readings[anchor]
    assert value is not None
    for i, event in enumerate(ordered):
        job = job_of(event)
        if job in plate and event.ref_kind == PRINT_RUN:
            continue
        if job in plate and event.ref_kind == BB_USAGE:
            after = i > anchor if bb_anchor else plate[job] > anchor
            if after and event.kind == "print_ended" and event.delta_g is not None:
                value += event.delta_g
            continue
        if i <= anchor or event.source not in _DELTA or event.delta_g is None:
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
