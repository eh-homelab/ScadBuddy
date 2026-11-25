"""An insert's empty weight from weighing new, unused spools (#7280, plan
`docs/superpowers/plans/2026-11-23-insert-empty-weight-from-new-spool.md`).

A new spool holds its label weight of filament (R-7280-1), so a reading of it gives its
empty spool and insert: ``reading - label weight``. Each accepted reading is one row of
`filament_insert_weight_readings`, at most one per spool (R-7280-2), and the insert's
``empty_weight_g`` is the mean of its rows, rounded half up (D4), with basis ``derived``.

The check only reads. Nothing changes the insert until a person (or the agent for one)
accepts (R-7280-4). Accepting books no ledger event: the weighing itself stays
``POST /filament/spools/{id}/events`` (R-7280-7), sent after the accept (D3).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.filament import ledger
from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import read_on

if TYPE_CHECKING:
    from scadbuddy.filament.spools import FilamentSpoolStore

Conn = Connection[DictRow]

#: How far grams may be from the label weight and still be "full" (D2).
FULL_TOLERANCE_G = 0.5
#: How a placeholder's note begins, in any case, after leading space (R-7280-3).
PLACEHOLDER_NOTE = "placeholder weight"

_USED_KINDS = frozenset({ledger.PRINT_STARTED, ledger.PRINT_ENDED})
_USED_SOURCES = frozenset({ledger.PRINT_ESTIMATE, ledger.PRINT_ACTUAL})


class NoInsertWeightOfferError(Exception):
    """An accept when the check has no offer now; ``reason`` says why."""

    def __init__(self, reason: m.NoOfferReason) -> None:
        super().__init__(f"no insert weight offer: {reason}")
        self.reason: m.NoOfferReason = reason


def is_placeholder(basis: str, note: str | None) -> bool:
    """Whether an insert's weight is a placeholder: its basis, or its note (R-7280-3).
    The catalog's ``placeholder`` column is the same rule in SQL."""
    return basis == "placeholder" or (note or "").lstrip().lower().startswith(PLACEHOLDER_NOTE)


# `check_on` uses `consumed` and `full` separately so it can name `used` before `not_full`.
def unused(events: Sequence[m.SpoolEvent], *, grams_left: float | None, label: int) -> bool:
    """Whether a spool is new by D2: no event uses filament and its grams left still
    equal the label weight."""
    return full(grams_left, label) and not consumed(events, label)


def full(grams_left: float | None, label: int) -> bool:
    """Whether the grams left equal the label weight (D2)."""
    return grams_left is not None and abs(grams_left - label) < FULL_TOLERANCE_G


def consumed(events: Sequence[m.SpoolEvent], label: int) -> bool:
    """Whether an event shows use (D2): a print, a print's grams, a delta down, or a
    reading below the label weight."""
    for event in events:
        if event.kind in _USED_KINDS or event.source in _USED_SOURCES:
            return True
        if event.delta_g is not None and event.delta_g < 0:
            return True
        reading = ledger.reading(event, label)
        if reading is not None and reading < label - FULL_TOLERANCE_G:
            return True
    return False


def offer_reason(reading: float, label: int) -> m.NoOfferReason | None:
    """Why ``reading`` of a new spool gives no insert weight, else ``None``: the derived
    weight must be above 0 and at most the label weight (R-7280-1)."""
    derived = reading - label
    if derived <= 0:
        return "not_above_label"
    if derived > label:
        return "derived_too_heavy"
    return None


#: A shortfall within this share of the label weight reads as a tared scale by default.
TARED_SHARE = 0.10


def below_label(
    reading_g: float, label: int, empty_g: int
) -> tuple[float, list[m.BelowLabelResolution], m.BelowLabelResolutionKind]:
    """A new spool's reading under its label weight (#7683, spec §5.3): the shortfall,
    the resolutions in order (``tared`` always; ``short`` and ``label_wrong`` only when the
    reading is at least the empty weight) and the default (``tared`` for a shortfall within
    `TARED_SHARE` of the label, else ``short``, else ``tared``)."""
    shortfall = label - reading_g
    found = [m.BelowLabelResolution(kind="tared", filament_g=reading_g)]
    filament = reading_g - empty_g
    if filament >= 0:
        found.append(m.BelowLabelResolution(kind="short", filament_g=filament))
        found.append(
            m.BelowLabelResolution(
                kind="label_wrong",
                filament_g=filament,
                new_label_weight_g=max(1, math.floor(filament + 0.5)),
            )
        )
    default: m.BelowLabelResolutionKind = "tared"
    if shortfall > TARED_SHARE * label and len(found) > 1:
        default = "short"
    return shortfall, found, default


def mean_weight(derived: Sequence[float]) -> int:
    """The mean of the readings, rounded half up (D4)."""
    return math.floor(sum(derived) / len(derived) + 0.5)


def checked_by_default(basis: str, placeholder: bool) -> bool:
    """An offer is checked only over a placeholder or an earlier derived weight (R-7280-4)."""
    return placeholder or basis == "derived"


def check_on(
    conn: Conn, spool_id: UUID, reading_g: float, *, lock: bool = False
) -> m.InsertWeightCheck | None:
    """Check ``reading_g`` of a spool against its insert; ``None`` for no such spool.
    With ``lock``, the spool's row and then its insert's are locked (`accept_on`)."""
    spool = conn.execute(
        "SELECT s.archived_at IS NOT NULL AS archived, s.grams_left,"
        " coalesce(s.label_weight_g, t.label_weight_g) AS label_weight_g,"
        " coalesce(s.insert_type_id, t.insert_type_id) AS insert_type_id"
        " FROM filament_spools s LEFT JOIN filament_spool_types t ON t.id = s.type_id"
        f" WHERE s.id = %s{' FOR UPDATE OF s' if lock else ''}",
        (spool_id,),
    ).fetchone()
    if spool is None:
        return None
    insert: dict[str, Any] | None = None
    if spool["insert_type_id"] is not None:
        insert = conn.execute(
            "SELECT id, name, empty_weight_g, empty_weight_basis, note"
            f" FROM filament_insert_types WHERE id = %s{' FOR UPDATE' if lock else ''}",
            (spool["insert_type_id"],),
        ).fetchone()

    def answer(
        reason: m.NoOfferReason | None, offer: m.InsertWeightOffer | None = None
    ) -> m.InsertWeightCheck:
        weight = None if insert is None else insert["empty_weight_g"]
        check = m.InsertWeightCheck(
            offer=offer,
            reason=reason,
            insert_type_id=None if insert is None else insert["id"],
            empty_weight_g=weight,
            filament_g=None if weight is None else reading_g - weight,
        )
        if reason == "below_label":
            assert label is not None and weight is not None
            check.shortfall_g, check.resolutions, check.default = below_label(
                reading_g, label, weight
            )
        return check

    label: int | None = spool["label_weight_g"]
    if spool["archived"]:
        return answer("archived")
    if insert is None:
        return answer("no_insert")
    if label is None:
        return answer("no_label_weight")
    if consumed(ledger.events(conn, spool_id), label):
        return answer("used")
    if not full(spool["grams_left"], label):
        return answer("not_full")
    if reading_g < label - FULL_TOLERANCE_G:
        return answer("below_label")
    reason = offer_reason(reading_g, label)
    if reason is not None:
        return answer(reason)
    others = [
        row["derived_g"]
        for row in conn.execute(
            "SELECT derived_g FROM filament_insert_weight_readings"
            " WHERE insert_type_id = %s AND spool_id <> %s",
            (insert["id"], spool_id),
        ).fetchall()
    ]
    derived = reading_g - label
    new_weight = mean_weight([*others, derived])
    if new_weight < 1:  # the table's CHECK (empty_weight_g > 0)
        return answer("not_above_label")
    placeholder = is_placeholder(insert["empty_weight_basis"], insert["note"])
    offer = m.InsertWeightOffer(
        insert_type_id=insert["id"],
        insert_name=insert["name"],
        spool_id=spool_id,
        reading_g=reading_g,
        label_weight_g=label,
        derived_g=derived,
        current_empty_weight_g=insert["empty_weight_g"],
        current_basis=insert["empty_weight_basis"],
        placeholder=placeholder,
        new_empty_weight_g=new_weight,
        sample_count=len(others) + 1,
        checked_by_default=checked_by_default(insert["empty_weight_basis"], placeholder),
    )
    return answer(None, offer)


def accept_on(conn: Conn, spool_id: UUID, reading_g: float) -> m.InsertWeightAccepted | None:
    """Keep ``reading_g`` as the spool's row and set its insert's weight to the mean of
    the insert's rows, basis ``derived``; ``None`` for no such spool. Every condition is
    checked again under the row locks (D3); `NoInsertWeightOfferError` when none is met
    now. A spool's second accept replaces its row, and a row of the spool's old insert
    moves to its new one; the old insert keeps its weight (D5). Run it in a transaction."""
    check = check_on(conn, spool_id, reading_g, lock=True)
    if check is None:
        return None
    offer = check.offer
    if offer is None:
        assert check.reason is not None
        raise NoInsertWeightOfferError(check.reason)
    conn.execute(
        "INSERT INTO filament_insert_weight_readings"
        " (insert_type_id, spool_id, reading_g, label_weight_g, derived_g)"
        " VALUES (%s, %s, %s, %s, %s)"
        " ON CONFLICT (spool_id) DO UPDATE SET insert_type_id = EXCLUDED.insert_type_id,"
        " reading_g = EXCLUDED.reading_g, label_weight_g = EXCLUDED.label_weight_g,"
        " derived_g = EXCLUDED.derived_g, created_at = now()",
        (offer.insert_type_id, spool_id, reading_g, offer.label_weight_g, offer.derived_g),
    )
    conn.execute(
        "UPDATE filament_insert_types SET empty_weight_g = %s,"
        " empty_weight_basis = 'derived', updated_at = now() WHERE id = %s",
        (offer.new_empty_weight_g, offer.insert_type_id),
    )
    insert = read_on(conn, "insert-types", offer.insert_type_id)
    assert isinstance(insert, m.InsertType)
    return m.InsertWeightAccepted(
        insert_type=insert,
        empty_weight_g=insert.empty_weight_g,
        sample_count=insert.derived_count,
        filament_g=reading_g - insert.empty_weight_g,
    )


class InsertWeightStore:
    """`check_on` and `accept_on`, each in a transaction of the spools store's pool.
    Synchronous: callers run it in a thread."""

    def __init__(self, spools: FilamentSpoolStore) -> None:
        self._spools = spools

    def check(self, spool_id: UUID, reading_g: float) -> m.InsertWeightCheck | None:
        with self._spools.transaction() as conn:
            return check_on(conn, spool_id, reading_g)

    def accept(self, spool_id: UUID, reading_g: float) -> m.InsertWeightAccepted | None:
        with self._spools.transaction() as conn:
            return accept_on(conn, spool_id, reading_g)
