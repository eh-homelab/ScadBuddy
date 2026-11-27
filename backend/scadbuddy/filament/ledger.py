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
``print_ended`` at the report that shows the plate's timed end (`filament/prints.py`). An event
with no ``at`` is booked now: the clock's time, but never earlier than a microsecond
after the spool's newest event, so a clock that steps back (WSL's does) cannot put an
event before the one it follows. That guard only moves an event booked now, never a
named time, so it never pushes a physical time past a newer anchor. Three rules (#6263, spec §5.5):

1. At one instant every delta comes before every reading, and events of one kind keep
   the order they were booked in (``seq``; a pull's ``created`` and ``synced_from_bb`` at
   one BB time stay in order). A reading
   taken as a plate ends holds that plate. BB writes a usage row and its spool's
   ``updated_at`` at one instant, so a BB reading holds its own usage row.
2. An ``ams_remain`` reading with ``data.printing`` true is recorded but is never an
   anchor: the AMS remain % moves in steps during a plate.
3. One plate is counted once. SB's events (``ref_kind`` `PRINT_RUN`) and BB's usage row
   (`BB_USAGE`) of one job (`job_of`) on one spool are a pair (`pairs`). For a pair, SB's events
   are skipped and BB's ``delta_g`` counts, because BB keeps a failed plate's partial
   grams and SB books 0 g. The pair counts when it lies after the anchor, and each
   anchor is compared in its own clock: an anchor from BB (`BB_REFS`) with BB's usage
   time, any other anchor with SB's time of the plate (its newest end, else its start).
   So the skew between BB's clock and SB's cannot count a plate twice or drop it. A pair
   is per spool: when BB booked the plate on another spool than SB's estimate, the pull
   books a 0 g ``print_ended`` on the estimate's spool with ``data.covered_by`` the BB
   event (`bambuddy_usage._cover`, #6598). The estimate then drops as any estimate does
   whose ref has a ``print_ended``: `basis` skips it.

A ``weighed`` event sent with its scale reading keeps the breakdown in its ``data``
(#7682): ``reading_g``, ``empty_g``, ``empty_source``, ``empty_basis``, ``empty_name``,
``expected_g`` (the grams left just before it, `expected_before`) and ``drift_g``
(`breakdown`); the newest one's drift is the spool's ``last_drift_g``.

`refresh_derived` reads a spool's whole history, except for an event booked on top of
it (`record` passes the event; #6263's AMS readings come one per tick). When no other
event of the spool is later than it, it is not a delta (``source`` ``print_actual`` or
``print_estimate``), not a ``print_started`` or ``print_ended``, and changes nothing
(no ``data.changes``: an edit can lock the weight or move the label weight), the event
sorts last and joins no pair, so the walk's answer is known without the walk. A reading
that qualifies as an anchor (an ``ams_remain`` one when the weight is not locked and it
is not ``printing``) gives ``grams_left``; any other event leaves it. A ``weighed`` or
``scale`` event with ``grams_after`` sets ``last_weighed_*``, a ``dried`` one
``last_dried_*``. Any other event takes the full walk.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, NamedTuple
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic_core import to_jsonable_python

from scadbuddy.filament import models as m
from scadbuddy.filament.provenance import current_by
from scadbuddy.filament.shopping import add_for_empty_on

if TYPE_CHECKING:
    from scadbuddy.filament.empty_weight import EmptyWeight

Conn = Connection[DictRow]

#: A print run's events (`filament/print_uses.py`): ``ref_kind`` `PRINT_RUN`, ``ref_id``
#: ``run/subtask/slot``. A start books a `PRINT_STARTED` event of source
#: `PRINT_ESTIMATE`; its settle a `PRINT_ENDED` one of source `PRINT_ACTUAL`.
PRINT_RUN: Final = "print_run"
PRINT_STARTED: Final = "print_started"
PRINT_ENDED: Final = "print_ended"
PRINT_ESTIMATE: Final = "print_estimate"
PRINT_ACTUAL: Final = "print_actual"
#: The ``ref_kind`` of a BB usage row booked on the spool.
BB_USAGE: Final = "bb_usage"
#: The ``ref_kind`` of a BB pull's ``created`` and ``synced_from_bb`` events.
BB_SPOOL: Final = "bb_spool"
#: The event that starts a spool's consumed counter again (#7149 R-7149-4): no grams. Its
#: source is ``bb`` when the pull booked Bambuddy's reset, else ``person``.
CONSUMED_RESET: Final = "consumed_reset"
#: The ``ref_kind``s of the events that carry BB's clock.
BB_REFS: Final = frozenset({BB_SPOOL, "bb_scale", "bb_dried", BB_USAGE})

_COLUMNS = ", ".join(m.SpoolEvent.model_fields)
_ABSOLUTE = frozenset({"scale", "person", "bb", "ams_remain"})
_MANUAL = frozenset({"scale", "person"})
_DELTA = frozenset({PRINT_ACTUAL, PRINT_ESTIMATE})
_USED = frozenset({PRINT_STARTED, PRINT_ENDED})


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
    now, by the module's rule. An agent's or a flow's call adds ``data.author``
    (`provenance.current_by`), unless ``data`` names one; never ``data.by``, which
    `capture.py` writes as a string."""
    author = current_by()
    if author is not None and "author" not in (data or {}):
        data = (data or {}) | {"author": author}
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
    event = m.SpoolEvent.model_validate(row)
    derived = refresh_derived(conn, spool_id, event)
    if derived is not None:
        _check_empty(conn, spool_id, event, derived)
    return event


class Derived(NamedTuple):
    """What `refresh_derived` left on the spool's row that `_check_empty` needs, so a
    booking reads the row once."""

    grams_left: float | None
    archived: bool


def _check_empty(conn: Conn, spool_id: UUID, event: m.SpoolEvent, now: Derived) -> None:
    """#6868: the move into empty adds the spool's type to the shopping list, in this
    write's transaction (`shopping.add_for_empty_on`). Every spool event is booked here, so
    every path that empties a spool is checked: a print's grams, a weighing, an AMS
    reading, a person's edit, the Bambuddy pull. The move is an unarchived spool whose
    ``grams_left`` goes from above 0 (``grams_before``) to 0, or an ``archived`` event on a
    spool not already empty. Unknown grams that become 0 are no move (an import or the
    first reading of an old reel), nor is the pull's archived import of a spool Bambuddy
    had archived (``data.bb_archived_at``). Later writes on an empty spool add nothing."""
    if event.kind == "non_print_waste":
        # #7167: the archive's own zero reading is no run-out; the `archived` event decides.
        return
    before = event.grams_before
    was_empty = before is not None and before <= 0
    if event.kind == "archived":
        # The waste event before it took the spool to 0 g; what it wrote off was there.
        if (event.data.get("non_print_waste_g") or 0) > 0:
            was_empty = False
        if was_empty or "bb_archived_at" in event.data:
            return
        add_for_empty_on(conn, spool_id, cause="archived", at=event.at)
        return
    if before is None or was_empty or now.archived:
        return
    if now.grams_left is not None and now.grams_left <= 0:
        add_for_empty_on(conn, spool_id, cause="ran_out", at=event.at)


def reading(event: m.SpoolEvent, label_weight_g: int | None) -> float | None:
    """The grams an absolute reading gives, by the rule above; ``None`` for an event
    that is not one."""
    if event.source not in _ABSOLUTE:
        return None
    if event.grams_after is not None:
        return event.grams_after
    pct = event.data.get("remain_pct") if event.source == "ams_remain" else None
    if isinstance(pct, int | float) and label_weight_g is not None:
        return pct * label_weight_g / 100
    return None


def pairs(events: Sequence[m.SpoolEvent]) -> set[str]:
    """Rule 3's pairs: the jobs with a BB usage `PRINT_ENDED` event and an SB
    (`PRINT_RUN`) plate event on this spool."""
    used = {job_of(e) for e in events if e.ref_kind == BB_USAGE and e.kind == PRINT_ENDED}
    return {
        job
        for e in events
        if e.ref_kind == PRINT_RUN and e.kind in _USED and (job := job_of(e)) in used
        if job is not None
    }


def is_anchor(event: m.SpoolEvent, *, weight_locked: bool, label_weight_g: int | None) -> bool:
    """Whether ``event`` may anchor the grams left: it is a reading, and not an
    ``ams_remain`` one while the weight is locked or the AMS is printing (rule 2).
    `basis` also skips an ``ams_remain`` reading older than the newest manual one."""
    if reading(event, label_weight_g) is None:
        return False
    return not (
        event.source == "ams_remain" and (weight_locked or event.data.get("printing") is True)
    )


def basis(
    events: Sequence[m.SpoolEvent], *, weight_locked: bool, label_weight_g: int | None
) -> m.GramsBasis:
    """The reading the grams left anchor on, and the deltas taken off it, by the rule
    above; no anchor when there is no reading. At one time, deltas come first and events
    of one kind keep the order they were booked in (rule 1)."""
    ordered = sorted(events, key=lambda e: (e.at, 0 if e.source in _DELTA else 1, e.seq))
    readings = [reading(e, label_weight_g) for e in ordered]
    manual = max(
        (i for i, e in enumerate(ordered) if e.source in _MANUAL and readings[i] is not None),
        default=-1,
    )
    anchor = -1
    for i, event in enumerate(ordered):
        if not is_anchor(event, weight_locked=weight_locked, label_weight_g=label_weight_g):
            continue
        if event.source == "ams_remain" and i < manual:
            continue
        anchor = i
    if anchor < 0:
        return m.GramsBasis(anchor=None, deltas=[])
    # Rule 3: a job with a BB usage row and an SB plate event on this spool is a pair.
    # `plate` is SB's position of the plate per job: its newest end, else its newest start.
    paired = pairs(ordered)
    ends: dict[str, int] = {}
    starts: dict[str, int] = {}
    for i, event in enumerate(ordered):
        job = job_of(event)
        if event.ref_kind == PRINT_RUN and job in paired and job is not None:
            if event.kind == PRINT_ENDED:
                ends[job] = i
            elif event.kind == PRINT_STARTED:
                starts[job] = i
    plate = starts | ends
    bb_anchor = ordered[anchor].ref_kind in BB_REFS
    ended = {(e.ref_kind, e.ref_id) for e in ordered if e.kind == "print_ended" and e.ref_id}
    deltas: list[m.SpoolEvent] = []
    for i, event in enumerate(ordered):
        job = job_of(event)
        if job in plate and event.ref_kind == PRINT_RUN:
            continue
        if job in plate and event.ref_kind == BB_USAGE:
            after = i > anchor if bb_anchor else plate[job] > anchor
            if after and event.kind == PRINT_ENDED and event.delta_g is not None:
                deltas.append(event)
            continue
        if i <= anchor or event.source not in _DELTA or event.delta_g is None:
            continue
        if event.source == PRINT_ESTIMATE and (event.ref_kind, event.ref_id) in ended:
            continue
        deltas.append(event)
    return m.GramsBasis(anchor=ordered[anchor], deltas=deltas)


def grams_left(
    events: Sequence[m.SpoolEvent], *, weight_locked: bool, label_weight_g: int | None
) -> float | None:
    """The grams left on a spool by the rule above; ``None`` with no reading to anchor
    on. Never below zero. It is `basis`'s anchor less its deltas, so the two agree."""
    found = basis(events, weight_locked=weight_locked, label_weight_g=label_weight_g)
    if found.anchor is None:
        return None
    value = reading(found.anchor, label_weight_g)
    assert value is not None
    for event in found.deltas:
        assert event.delta_g is not None
        value += event.delta_g
    return max(value, 0.0)


def expected_before(
    events: Sequence[m.SpoolEvent],
    at: datetime | None,
    *,
    weight_locked: bool,
    label_weight_g: int | None,
) -> float | None:
    """The grams left the ledger gave just before a weighing at ``at`` (#7682): `grams_left`
    over the events before it and, by rule 1, the deltas at that very instant; every event
    when ``at`` is None (the weighing is booked now). ``None`` with no anchor by then."""
    if at is not None:
        events = [e for e in events if e.at < at or (e.at == at and e.source in _DELTA)]
    return grams_left(events, weight_locked=weight_locked, label_weight_g=label_weight_g)


def breakdown(
    *,
    reading_g: float,
    grams: float,
    empty: EmptyWeight | None,
    expected: float | None,
    tared: bool = False,
    resolved: bool = False,
) -> dict[str, Any]:
    """A weighing's ``data`` keys (#7682): the reading, what was taken off it for the empty
    spool and where that came from (``insert`` when it is the effective empty weight,
    ``tared`` when the scale was tared on the empty spool so nothing was taken off (#7683),
    else ``person``: the client took something else off, or nothing was known), the grams
    the ledger expected and the drift, ``grams - expected``, to 0.1 g; both null with no
    ``expected``. A ``resolved`` weighing (#7683: the person explained the shortfall)
    keeps ``expected_g`` and records no drift."""
    empty_g = reading_g - grams
    source: str
    basis: str | None
    name: str | None
    if tared:
        source, basis, name = "tared", None, None
    elif empty is not None and abs(empty_g - empty.grams) < 0.05:
        source, basis, name = empty.source, empty.basis, empty.name
    else:
        source, basis, name = "person", None, None
    return {
        "reading_g": reading_g,
        "empty_g": empty_g,
        "empty_source": source,
        "empty_basis": basis,
        "empty_name": name,
        "expected_g": expected,
        "drift_g": None if expected is None or resolved else round(grams - expected, 1),
    }


def closing(
    events: Sequence[m.SpoolEvent],
    *,
    label_weight_g: int | None,
    added_full: bool | None,
    final_filament_g: float | None,
    reason: m.ArchiveReason | None,
    grams_left: float | None = None,
    waste: bool = False,
) -> dict[str, Any]:
    """An ``archived`` event's ``data`` (#7684, spec §5.4): the reason and the lifetime
    reconciliation. ``started_g`` is the first ``scale`` or ``person`` reading
    (``started_from`` ``reading``; a ``created`` event's counts), else the label weight of a
    spool added full (``label``), else null. ``ledger_consumed_g`` is the grams of the
    plates (`metrics.plates`) at or after that reading, every plate for the label, never
    affected by a ``consumed_reset``; null without a start. ``final_filament_g`` is the
    final weighing's filament, else ``grams_left`` (the spool's at the archive).
    ``scale_consumed_g`` is ``started_g`` less the final weighing's filament and ``drift_g``
    is ``ledger_consumed_g - scale_consumed_g`` (negative: the scale found less filament
    than the ledger expected), both null without a weighing or a start. Grams to 0.1 g.
    With ``waste`` (an archive that books it, #7167) it also holds ``non_print_waste_g``:
    the final filament when above 0 and the reason is not ``respooled`` (the filament
    moved to another reel), else null."""
    from scadbuddy.filament.metrics import plates  # metrics imports this module

    ordered = sorted(events, key=lambda e: (e.at, e.seq))
    first = next(
        (
            e
            for e in ordered
            if e.source in _MANUAL and e.grams_after is not None and e.kind != "non_print_waste"
        ),
        None,
    )
    started: float | None
    started_from: str | None
    since: datetime | None
    if first is not None:
        started, started_from, since = first.grams_after, "reading", first.at
    elif added_full and label_weight_g is not None:
        started, started_from, since = float(label_weight_g), "label", None
    else:
        started, started_from, since = None, None, None
    consumed: float | None = None
    if started is not None:
        consumed = sum(p.grams for p in plates(ordered) if since is None or p.at >= since)
    final = grams_left if final_filament_g is None else final_filament_g
    scale: float | None = None
    drift: float | None = None
    if started is not None and final_filament_g is not None:
        scale = started - final_filament_g
        drift = None if consumed is None else consumed - scale
    data: dict[str, Any] = {
        "reason": reason,
        "started_g": _tenth(started),
        "started_from": started_from,
        "ledger_consumed_g": _tenth(consumed),
        "final_filament_g": _tenth(final),
        "scale_consumed_g": _tenth(scale),
        "drift_g": _tenth(drift),
    }
    if waste:
        left = _tenth(final)
        booked = left is not None and left > 0 and reason != "respooled"
        data["non_print_waste_g"] = left if booked else None
    return data


def _tenth(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def events(conn: Conn, spool_id: UUID) -> list[m.SpoolEvent]:
    """A spool's events, oldest first; two of one time in the order they were booked."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM filament_spool_events WHERE spool_id = %s ORDER BY at, seq",
        (spool_id,),
    ).fetchall()
    return [m.SpoolEvent.model_validate(row) for row in rows]


def plate_events(
    conn: Conn, spool_ids: Sequence[UUID] | None = None
) -> dict[UUID, list[m.SpoolEvent]]:
    """The `PRINT_STARTED` and `PRINT_ENDED` events of ``spool_ids`` (every spool with
    none), per spool, oldest first: what `metrics.plates` reads. The partial index
    ``filament_spool_events_plates`` serves it, so the AMS readings are never read."""
    where = "kind IN ('print_started', 'print_ended')"
    args: list[Any] = []
    if spool_ids is not None:
        where += " AND spool_id = ANY(%s)"
        args.append(list(spool_ids))
    found: dict[UUID, list[m.SpoolEvent]] = {}
    for row in conn.execute(
        f"SELECT {_COLUMNS} FROM filament_spool_events WHERE {where} ORDER BY spool_id, at, seq",
        args,
    ).fetchall():
        found.setdefault(row["spool_id"], []).append(m.SpoolEvent.model_validate(row))
    return found


class BadCursorError(ValueError):
    """A ``cursor`` that no page of this ledger gave."""

    def __init__(self) -> None:
        super().__init__("the cursor is not one this ledger gave")


def _encode(at: datetime, seq: int) -> str:
    return f"{at.isoformat()}|{seq}".encode().hex()


def _decode(cursor: str) -> tuple[datetime, int]:
    try:
        text = bytes.fromhex(cursor).decode()
        at, seq = text.rsplit("|", 1)
        return datetime.fromisoformat(at), int(seq)
    except ValueError:
        raise BadCursorError() from None


def page(
    conn: Conn,
    *,
    spool_id: UUID | None = None,
    kind: m.EventKind | None = None,
    source: m.EventSource | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> m.EventPage:
    """One page of every spool's events, newest first by ``(at, seq)``. The cursor names
    the last row of the page before, as that pair, so two events of one time never lose
    or repeat a row at the page's edge (F4). ``since`` is included and ``until`` excluded,
    both on the event's ``at``."""
    where: list[str] = []
    args: list[Any] = []
    for column, value in (("e.spool_id", spool_id), ("e.kind", kind), ("e.source", source)):
        if value is not None:
            where.append(f"{column} = %s")
            args.append(value)
    if since is not None:
        where.append("e.at >= %s")
        args.append(since)
    if until is not None:
        where.append("e.at < %s")
        args.append(until)
    if cursor is not None:
        at, seq = _decode(cursor)
        where.append("(e.at, e.seq) < (%s, %s)")
        args.extend((at, seq))
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    columns = ", ".join(f"e.{c}" for c in m.SpoolEvent.model_fields)
    rows = conn.execute(
        f"SELECT {columns}, e.seq, s.number AS spool_number FROM filament_spool_events e"
        f" JOIN filament_spools s ON s.id = e.spool_id{clause}"
        " ORDER BY e.at DESC, e.seq DESC LIMIT %s",
        [*args, limit + 1],
    ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return m.EventPage(
        events=[m.LedgerRow.model_validate(row) for row in rows],
        next_cursor=_encode(rows[-1]["at"], rows[-1]["seq"]) if more else None,
    )


def refresh_derived(
    conn: Conn, spool_id: UUID, event: m.SpoolEvent | None = None
) -> Derived | None:
    """Recompute the spool's ``grams_left`` and ``last_*`` columns from its events. The
    label weight is the spool's own, else its type's. The spool's row is locked (`record`
    has locked it already), so two transactions that each record an event read each
    other's in turn. With ``event`` (the one just booked), an event on top of the history
    updates the columns without reading it, by the module's rule. It answers the new
    ``grams_left`` and whether the spool is archived; ``None`` for no such spool."""
    spool = conn.execute(
        "SELECT s.weight_locked, coalesce(s.label_weight_g, t.label_weight_g) AS label_weight_g,"
        " s.grams_left, s.archived_at IS NOT NULL AS archived"
        " FROM filament_spools s LEFT JOIN filament_spool_types t ON t.id = s.type_id"
        " WHERE s.id = %s FOR UPDATE OF s",
        (spool_id,),
    ).fetchone()
    if spool is None:
        return None
    archived: bool = spool["archived"]
    if event is not None and _on_top(conn, spool_id, event):
        left = _book_on_top(conn, spool_id, event, spool["weight_locked"], spool["label_weight_g"])
        return Derived(spool["grams_left"] if left is None else left, archived)
    history = events(conn, spool_id)
    left = grams_left(
        history, weight_locked=spool["weight_locked"], label_weight_g=spool["label_weight_g"]
    )
    weighed = [e for e in history if _weighs(e)]
    dried = [e for e in history if e.kind == "dried"]
    used = [e for e in history if e.kind in _USED]
    last_weighed = weighed[-1] if weighed else None
    last_dried = dried[-1] if dried else None
    conn.execute(
        "UPDATE filament_spools SET grams_left = %s, last_weighed_g = %s, last_weighed_at = %s,"
        " last_drift_g = %s, last_dried_at = %s, last_dried_temp_c = %s, last_dried_hours = %s,"
        " last_used_at = %s WHERE id = %s",
        (
            left,
            last_weighed.grams_after if last_weighed else None,
            last_weighed.at if last_weighed else None,
            last_weighed.data.get("drift_g") if last_weighed else None,
            last_dried.at if last_dried else None,
            last_dried.data.get("temp_c") if last_dried else None,
            last_dried.data.get("hours") if last_dried else None,
            used[-1].at if used else None,
            spool_id,
        ),
    )
    return Derived(left, archived)


def _weighs(event: m.SpoolEvent) -> bool:
    return (event.kind == "weighed" or event.source == "scale") and event.grams_after is not None


def _on_top(conn: Conn, spool_id: UUID, event: m.SpoolEvent) -> bool:
    """True when ``event`` sorts last in the walk and can join no pair or change the
    rule's inputs: the module's fast-path rule."""
    if event.source in _DELTA or event.kind in _USED or event.data.get("changes"):
        return False
    row = conn.execute(
        "SELECT max(at) AS at FROM filament_spool_events WHERE spool_id = %s AND id <> %s",
        (spool_id, event.id),
    ).fetchone()
    newest = row["at"] if row is not None else None
    return newest is None or newest <= event.at


def _book_on_top(
    conn: Conn,
    spool_id: UUID,
    event: m.SpoolEvent,
    weight_locked: bool,
    label_weight_g: int | None,
) -> float | None:
    """The columns ``event`` changes when it is on top. A last reading is the walk's
    anchor whenever it qualifies (no manual reading is later), and no delta follows it.
    The new ``grams_left`` when it sets one, else ``None`` (the column is unchanged)."""
    sets: dict[str, Any] = {}
    value = reading(event, label_weight_g)
    if value is not None and is_anchor(
        event, weight_locked=weight_locked, label_weight_g=label_weight_g
    ):
        sets["grams_left"] = max(value, 0.0)
    if _weighs(event):
        sets |= {
            "last_weighed_g": event.grams_after,
            "last_weighed_at": event.at,
            "last_drift_g": event.data.get("drift_g"),
        }
    if event.kind == "dried":
        sets |= {
            "last_dried_at": event.at,
            "last_dried_temp_c": event.data.get("temp_c"),
            "last_dried_hours": event.data.get("hours"),
        }
    if sets:
        columns = ", ".join(f"{c} = %s" for c in sets)
        conn.execute(
            f"UPDATE filament_spools SET {columns} WHERE id = %s", [*sets.values(), spool_id]
        )
    left: float | None = sets.get("grams_left")
    return left
