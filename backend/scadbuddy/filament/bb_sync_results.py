"""What Bambuddy answered to the sync's writes (#6265 Task 5, plan
`docs/superpowers/plans/2026-11-18-filament-bb-sync.md`): `apply_results` links what a
create or an adoption made, stores the field hashes and the seen copy of each answer,
books the push's echo event (Decision 9), drops a link Bambuddy says is gone (Decision
13), and keeps one error row per item whose last write failed (Decision 19).

The print worker made the writes and sends them back with their answers. The plan is not
stored, so every write is checked against SB's links before anything is written (F13): a
result is ignored and logged when no write has its key, its item does not exist, a
create's item is linked or has no open intent, an update on an unlinked item has no open
intent or names a Bambuddy row another SB row holds (R-T4-10), another write's ``bb_id``
is not the item's link, or its answer is not the row it names.

The seen copy is merged, not swapped (R-T4-28): a field of the answer goes in only when
the write sent it, or when it is SB's compare value now. Every other field keeps its old
seen value, or stays out, so a value a person changed in Bambuddy is never taken as one
SB saw, and the next plan does not push SB's older value over it. A create's answer is
stored whole: Bambuddy made the row from SB's body a moment before, so a field the create
did not send holds Bambuddy's default, not a person's edit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid4

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from scadbuddy.bambuddy.models import (
    BbLocation,
    BbSupplier,
    CatalogFilament,
    LocalPreset,
    SpoolCatalogEntry,
    as_utc,
)
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.core.cancel import CancelToken, check, watched
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament import bb_sync_passes, ledger
from scadbuddy.filament.bambuddy_pull import REF_KIND, record_instance, snapshot
from scadbuddy.filament.bb_sync_models import (
    BbSnapshot,
    BbWrite,
    BbWriteResult,
    Kind,
    LinksResetCounts,
    LinksResetDone,
    LinksResetPreview,
    ResultsReport,
    SkipReason,
    SyncError,
    SyncPassResult,
    SyncPlan,
    SyncStatus,
    json_form,
    seen_row,
    seen_value,
)
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.filament.bb_sync_plan import (
    ROW_KINDS,
    UNSEEN,
    BbRefs,
    SpoolFacts,
    SyncBusyError,
    bb_form,
    compare_form,
    desired_and_unmapped,
    hold_sync,
    k_form,
    pendings,
    plan,
    row_seen,
)
from scadbuddy.filament.component import Filament
from scadbuddy.filament.rfid_push import enqueue_tag

if TYPE_CHECKING:
    from scadbuddy.library.settings_store import SettingsStore

logger = logging.getLogger(__name__)

Conn = Connection[DictRow]

#: Who the push's ``synced_from_bb`` event names.
ACTOR = "system:bambuddy-sync"

#: The writes whose 404 says Bambuddy's row is gone (Decision 13, R-T4-13). A list
#: ``PUT`` also answers 404 for a supplier Bambuddy does not know (`sync-routes.json`
#: ``write_semantics.replace_all_lists``), so its 404 keeps the link: a spool that is
#: really gone is missing from the next complete listing, and its probe drops it.
_GONE_ON_404 = frozenset({"update", "archive", "restore", "probe", "reset_consumed"})
#: The writes Bambuddy answers with the spool's whole row (``SpoolResponse`` in
#: `sync-routes.json`; a spool's probe is ``GET spool_row``, another row's probe answers
#: that row, #6688). A list ``PUT`` answers its list.
_FULL_ROW = frozenset({"create", "update", "archive", "restore", "probe", "reset_consumed"})
#: The spool writes that change Bambuddy's row, and so book Decision 9's event and move
#: the link's ``bb_updated_at``.
_ECHOED = frozenset({"create", "update", "archive", "restore", "reset_consumed"})
#: The field each list ``PUT`` answers (the preset list: none a spool row carries).
_LIST_FIELDS: dict[str, str | None] = {
    "k_profiles": "k_profiles",
    "suppliers": "suppliers",
    "presets": None,
}
#: Every data type pushing: SB's compare values then cover every field an answer carries.
_EVERY = FilamentSync(
    spools="sb_to_bb",
    weights="sb_to_bb",
    tags="off",
    presets="sb_to_bb",
    suppliers="sb_to_bb",
    locations="sb_to_bb",
)
_ROW_MODELS: dict[str, type[BbSupplier | BbLocation | SpoolCatalogEntry | CatalogFilament]] = {
    "supplier": BbSupplier,
    "location": BbLocation,
    "insert_type": SpoolCatalogEntry,
    "filament": CatalogFilament,
}

Outcome = Literal["linked", "updated", "failed", "unlinked"]

#: The spool references a linked row of each kind gives (`BbRefs`).
_REF_FIELDS = {"supplier": "suppliers", "location": "locations", "insert_type": "insert_types"}


@dataclass
class _Reads:
    """What one `apply_results` call reads of SB once, not once per result (R-T5-11):
    each row kind's desired values, and the spool references, which a link this call
    makes is added to."""

    rows: dict[str, dict[UUID, dict[str, Any]]] = field(default_factory=dict)
    refs: BbRefs | None = None
    #: R-6688-10, R-6688-11: the plan's unconfirmed rows (probed this round, refused in
    #: this pass, or cut by the cap). Bambuddy may have deleted them, so the plan named
    #: none of them to a spool (`BbRefs.without`), and a spool's compare names none.
    unconfirmed: frozenset[tuple[str, UUID]] = frozenset()
    #: The round's spools, whose SB data is read once (#6902), and that data.
    spool_ids: Sequence[UUID] = ()
    facts: SpoolFacts | None = None

    def spool_facts(self, conn: Conn) -> SpoolFacts:
        if self.facts is None:
            self.facts = SpoolFacts.read(conn, self.spool_ids, _EVERY)
        return self.facts

    def echoed(self, conn: Conn, spool_id: UUID) -> None:
        """A spool's echo event was booked, which refreshes its derived columns
        (`ledger.refresh_derived`). Its compare values read them (grams left, last dried),
        so when they moved the spool's data is read again for its next result."""
        if self.facts is None or spool_id not in self.facts.spools:
            return
        row = conn.execute(
            "SELECT grams_left, last_dried_at FROM filament_spools WHERE id = %s", (spool_id,)
        ).fetchone()
        held = self.facts.spools[spool_id]
        if row is None or (row["grams_left"], row["last_dried_at"]) != (
            held.grams_left,
            held.last_dried_at,
        ):
            self.facts = None

    def desired(self, conn: Conn, kind: str, sb_id: UUID) -> dict[str, Any] | None:
        if kind not in self.rows:
            self.rows[kind] = {item.sb_id: item.desired for item in ROW_KINDS[kind].items(conn)}
        return self.rows[kind].get(sb_id)

    def bb_refs(self, conn: Conn) -> BbRefs:
        if self.refs is None:
            self.refs = BbRefs.read(conn).without(self.unconfirmed)
        return self.refs

    def linked(self, kind: str, sb_id: UUID, bb_id: int) -> None:
        if self.refs is not None and kind in _REF_FIELDS:
            ids = cast("dict[UUID, int]", getattr(self.refs, _REF_FIELDS[kind]))
            ids[sb_id] = bb_id


class _IgnoredError(Exception):
    """A result `apply_results` does not trust; its reason is logged."""


def apply_results(
    store: Filament,
    plan_writes: Sequence[BbWrite],
    results: Sequence[BbWriteResult],
    *,
    instance: str,
    now: datetime,
    pass_id: str | None = None,
    unconfirmed: Sequence[tuple[str, UUID]] = (),
    cancel: CancelToken | None = None,
) -> ResultsReport:
    """Record what Bambuddy answered to ``plan_writes``, as the module says, all of them
    in one transaction, each result in a savepoint of its own, so an ignored result rolls
    back its own writes only (#6902: a transaction per result was 200 commits, and each
    took 1 to 2 s on prod). The transaction takes the state row's lock once, and the
    instance's sync lock (`hold_sync`), so a second call for the instance while this one
    runs is `SyncBusyError` and writes nothing. It also locks the round's spools up front,
    in id order, so a writer that locks several spools in the same order waits for the
    round rather than deadlocking with it. ``cancel`` stops the round between results (and
    cancels the running statement): the whole round rolls back, unless it had already
    passed its last check, just before the commit. Either way the results sent again later
    are safe: they find their items linked or recorded, and the plan's ``pass_id`` adds no
    attempt. An error that is no ignored result rolls the round back too. ``now`` is the
    stored seen copies' ``bb_seen_at``.

    An ``ok`` create or adoption links the item, closes its intent, sets its hashes and
    seen copy (a spool's K-profiles too, R-T4-11), books a spool's event and queues the
    spool's open tags. An ``ok`` update, archive, restore or list ``PUT`` merges its
    hashes (``||``, so a row's ``bb_pushed`` keeps its ``unseen`` marks, R-T4-27) and its
    seen copy; an ``ok`` probe the seen copy only. A list ``PUT``'s ``answer`` is
    ``{field: rows}`` (``k_profiles`` or ``suppliers``) or the spool's whole row (R-T3-6).
    A failed write's 404 on a linked item drops the link and its error row, except a
    list ``PUT``'s; any other failure upserts the item's error row and keeps its hashes and
    intent (R-T4-18). The upsert adds an attempt and sets ``at = now()``, unless the row's
    ``last_pass_id`` is ``pass_id`` (the plan round these results answer): results sent
    again after a lost answer count once (R-T6-5). A ``None`` ``pass_id`` is a round of
    its own, so it always adds one. A spool's answer is compared with its desired
    values less the rows in ``unconfirmed`` (the plan's, R-6688-11) and the rows the
    round probes, as the plan made them. The state row's instance
    is set by the first result that links anything (Decision 12; the pull sets it too,
    `bambuddy_pull.record_instance`), and results for another instance are all ignored.
    A result whose link another SB row holds by now is ignored (review I1)."""
    report = ResultsReport()
    # The round's own probes too: a worker from before R-6688-11 sends no `unconfirmed`.
    probed = {(w.kind, w.sb_id) for w in plan_writes if w.op == "probe" and w.kind != "spool"}
    reads = _Reads(
        unconfirmed=frozenset(probed | set(unconfirmed)),
        spool_ids=[w.sb_id for w in plan_writes if w.kind == "spool"],
    )
    writes = {write.key: write for write in plan_writes}
    outcomes: list[Outcome] = []
    with store.spools.transaction() as conn, watched(cancel, conn):
        hold_sync(conn, instance)
        state = conn.execute("SELECT instance FROM filament_bb_sync_state FOR UPDATE").fetchone()
        foreign = state is not None and state["instance"] not in (None, instance)
        if reads.spool_ids:
            conn.execute(
                "SELECT 1 FROM filament_spools WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                (list(reads.spool_ids),),
            )
        for result in results:
            check(cancel)
            write = writes.get(result.key)
            try:
                if write is None:
                    raise _IgnoredError("no write has this key")
                if foreign:
                    raise _IgnoredError("the links belong to another Bambuddy")
                with conn.transaction():
                    outcome = _apply(conn, write, result, instance, now, reads, pass_id)
            except _IgnoredError as why:
                logger.warning(
                    "a Bambuddy sync result is ignored",
                    extra={"key": result.key, "reason": str(why)},
                )
                continue
            if outcome is not None:
                outcomes.append(outcome)
        check(cancel)
    for outcome in outcomes:
        setattr(report, outcome, getattr(report, outcome) + 1)
    return report


def _apply(
    conn: Conn,
    write: BbWrite,
    result: BbWriteResult,
    instance: str,
    now: datetime,
    reads: _Reads,
    pass_id: str | None,
) -> Outcome | None:
    item = _item(conn, write)
    linked: int | None = item["bb_id"]
    intent = conn.execute(
        "SELECT 1 FROM filament_bb_creates WHERE kind = %s AND sb_id = %s",
        (write.kind, write.sb_id),
    ).fetchone()
    if write.op == "create":
        if linked is not None or intent is None:
            raise _IgnoredError("a create's item must be unlinked, with an open intent")
    elif linked is None:
        if write.op != "update" or intent is None or write.bb_id is None:
            raise _IgnoredError("an unlinked item takes only an adoption, with an open intent")
        if _held(conn, write, write.bb_id):
            raise _IgnoredError("another SB row holds that Bambuddy row")
    elif write.bb_id != linked:
        raise _IgnoredError("the write's Bambuddy id is not the item's link")
    if not result.ok:
        if result.status == 404 and linked is not None and write.op in _GONE_ON_404:
            _unlink(conn, write)
            return "unlinked"
        # The same plan round's results again (the CRUD client sends a POST again after a
        # lost answer) add no attempt and keep ``at`` (R-T6-5).
        conn.execute(
            "INSERT INTO filament_bb_push_errors (kind, sb_id, error, last_pass_id)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (kind, sb_id) DO UPDATE SET error = EXCLUDED.error,"
            " attempts = filament_bb_push_errors.attempts + CASE WHEN"
            " filament_bb_push_errors.last_pass_id = EXCLUDED.last_pass_id THEN 0 ELSE 1 END,"
            " at = CASE WHEN filament_bb_push_errors.last_pass_id = EXCLUDED.last_pass_id"
            " THEN filament_bb_push_errors.at ELSE now() END,"
            " last_pass_id = EXCLUDED.last_pass_id",
            (write.kind, write.sb_id, result.error or "internal", pass_id),
        )
        return "failed"
    bb_id = _answered_id(write, result)
    try:
        with conn.transaction():
            if write.kind == "spool":
                _spool(conn, write, result, item, bb_id, now, reads)
            else:
                _row(conn, write, result, item, bb_id, now, reads)
    except errors.UniqueViolation:
        # Review I1: another SB row took this Bambuddy row since the write was planned
        # (a pull, say). One result never fails the round's others.
        raise _IgnoredError("another SB row holds that Bambuddy row") from None
    if write.op == "probe":
        # A probe writes nothing to Bambuddy: it counts nothing, and the item's last
        # write keeps its error row.
        return None
    conn.execute(
        "DELETE FROM filament_bb_push_errors WHERE kind = %s AND sb_id = %s",
        (write.kind, write.sb_id),
    )
    if linked is not None:
        return "updated"
    reads.linked(write.kind, write.sb_id, bb_id)
    conn.execute(
        "DELETE FROM filament_bb_creates WHERE kind = %s AND sb_id = %s",
        (write.kind, write.sb_id),
    )
    conn.execute(
        "UPDATE filament_bb_sync_state SET instance = %s WHERE instance IS NULL", (instance,)
    )
    return "linked"


def _item(conn: Conn, write: BbWrite) -> dict[str, Any]:
    """The write's SB item, locked: its Bambuddy id, its seen copy and (a spool's) its
    K-profiles as last seen."""
    if write.kind == "spool":
        row = conn.execute(
            "SELECT s.id, l.bb_spool_id AS bb_id, l.bb_seen, l.bb_k_profiles"
            " FROM filament_spools s LEFT JOIN filament_bb_links l ON l.spool_id = s.id"
            " WHERE s.id = %s FOR UPDATE OF s",
            (write.sb_id,),
        ).fetchone()
    else:
        kind = ROW_KINDS[write.kind]
        row = conn.execute(
            f"SELECT id, {kind.bb_column} AS bb_id, bb_seen FROM {kind.table}"
            " WHERE id = %s FOR UPDATE",
            (write.sb_id,),
        ).fetchone()
    if row is None:
        raise _IgnoredError("no such item")
    return dict(row)


def _held(conn: Conn, write: BbWrite, bb_id: int) -> bool:
    """Whether another SB row of the write's kind links ``bb_id``."""
    if write.kind == "spool":
        sql = "SELECT 1 FROM filament_bb_links WHERE bb_spool_id = %s AND spool_id <> %s"
    else:
        kind = ROW_KINDS[write.kind]
        sql = f"SELECT 1 FROM {kind.table} WHERE {kind.bb_column} = %s AND id <> %s"
    return conn.execute(sql, (bb_id, write.sb_id)).fetchone() is not None


def _answered_id(write: BbWrite, result: BbWriteResult) -> int:
    """The Bambuddy id the answer is for: the write's, or the row a create made."""
    if write.op != "create":
        assert write.bb_id is not None  # `_apply` ignores a non-create without one
        if result.bb_id not in (None, write.bb_id):
            raise _IgnoredError("the result names another Bambuddy row")
        return write.bb_id
    made = (result.answer or {}).get("id")
    if not isinstance(made, int) or result.bb_id not in (None, made):
        raise _IgnoredError("a create's answer names no Bambuddy row")
    return made


def _unlink(conn: Conn, write: BbWrite) -> None:
    """Decision 13: Bambuddy's row is gone. The link goes, with what it pushed and saw,
    and so does the item's error row. The plan then makes the row again (a spool only
    while it is unarchived and pushed)."""
    if write.kind == "spool":
        conn.execute("DELETE FROM filament_bb_links WHERE spool_id = %s", (write.sb_id,))
    else:
        kind = ROW_KINDS[write.kind]
        conn.execute(
            f"UPDATE {kind.table} SET {kind.bb_column} = NULL, bb_pushed = '{{}}',"
            " bb_seen = NULL, bb_seen_at = NULL WHERE id = %s",
            (write.sb_id,),
        )
    conn.execute(
        "DELETE FROM filament_bb_push_errors WHERE kind = %s AND sb_id = %s",
        (write.kind, write.sb_id),
    )


def _merge(
    old: Mapping[str, Any] | None,
    answer: Mapping[str, Any],
    write: BbWrite,
    is_sb: Callable[[str, Any], bool],
) -> dict[str, Any]:
    """R-T4-28: ``old`` with each field of ``answer`` that the write sent, or whose value
    is SB's (``is_sb``); a create's ``answer`` whole."""
    if write.op == "create":
        return dict(answer)
    taken = {n: v for n, v in answer.items() if n in write.hashes or is_sb(n, v)}
    return dict(old or {}) | taken


def _spool(
    conn: Conn,
    write: BbWrite,
    result: BbWriteResult,
    item: Mapping[str, Any],
    bb_id: int,
    now: datetime,
    reads: _Reads,
) -> None:
    answer = result.answer or {}
    bb: BbSpool | None = None
    seen: dict[str, Any] = {}
    k_rows: Any = None
    if write.op in _FULL_ROW:
        try:
            bb = BbSpool.model_validate(answer)
        except ValidationError:
            raise _IgnoredError("the answer is not a Bambuddy spool") from None
        if bb.id != bb_id:
            raise _IgnoredError("the answer is another Bambuddy spool")
        seen = seen_row(bb)
        k_rows = json_form([k.model_dump() for k in bb.k_profiles])
    else:
        field = _LIST_FIELDS[write.op]
        rows = None if field is None else answer.get(field)
        if field == "suppliers" and isinstance(rows, list):
            seen = {"suppliers": seen_value("suppliers", rows)}
        elif field == "k_profiles" and isinstance(rows, list):
            k_rows = json_form(rows)
    sb = _spool_compare(conn, write.sb_id, reads.bb_refs(conn), reads.spool_facts(conn))
    # R-7149-8: the baseline in a reset's answer is SB's own doing; every other
    # pull-only field keeps what the pull last saw, so an echo hides no Bambuddy change.
    merged = _merge(
        item["bb_seen"],
        seen,
        write,
        lambda n, _v: (
            (write.op == "reset_consumed" and n == "weight_used_baseline")
            or (n in sb and bb_form(n, seen, k_rows) == sb[n])
        ),
    )
    k = item["bb_k_profiles"]
    if k_rows is not None and (
        write.op == "create"
        or "k_profiles" in write.hashes
        or k_form(k_rows) == sb.get("k_profiles", UNSEEN)
    ):
        k = k_rows
    moved = None if bb is None or write.op not in _ECHOED else (bb.updated_at or now)
    k_json = None if k is None else Jsonb(k)
    if item["bb_id"] is None:
        conn.execute(
            "INSERT INTO filament_bb_links (spool_id, bb_spool_id, bb_updated_at, linked_at,"
            " pushed_fields, bb_seen, bb_seen_at, bb_k_profiles, bb_seeded)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true)",
            (
                write.sb_id,
                bb_id,
                moved or now,
                now,
                Jsonb(write.hashes),
                Jsonb(merged),
                now,
                k_json,
            ),
        )
        tags = conn.execute(
            "SELECT tag_id FROM filament_tag_bindings WHERE spool_id = %s AND unbound_at IS NULL",
            (write.sb_id,),
        ).fetchall()
        for tag in tags:
            enqueue_tag(conn, tag["tag_id"])
    else:
        conn.execute(
            "UPDATE filament_bb_links SET pushed_fields = pushed_fields || %s, bb_seen = %s,"
            " bb_seen_at = %s, bb_k_profiles = %s, bb_updated_at = coalesce(%s, bb_updated_at),"
            " bb_seeded = bb_seeded OR %s WHERE spool_id = %s",
            # A whole row's answer merged in is a full copy; a list's one field is not
            # (R-T5-10), so a full pass still seeds such a link.
            (Jsonb(write.hashes), Jsonb(merged), now, k_json, moved, bb is not None, write.sb_id),
        )
    if bb is not None and write.op in _ECHOED:
        # Decision 9: the pull compares with the newest event's ``bb``, so the row SB's
        # own write made never comes back to SB as a change.
        stamp = None if bb.updated_at is None else bb.updated_at.isoformat()
        ledger.record(
            conn,
            write.sb_id,
            "synced_from_bb",
            ref_kind=REF_KIND if stamp else None,
            ref_id=f"{bb.id}@{stamp}" if stamp else None,
            actor=ACTOR,
            at=bb.updated_at,
            data={"bb_spool_id": bb.id, "bb": snapshot(bb), "pushed": True, "changes": {}},
        )
        reads.echoed(conn, write.sb_id)


def _spool_compare(conn: Conn, spool_id: UUID, refs: BbRefs, facts: SpoolFacts) -> dict[str, Any]:
    """SB's values of the spool in the compare form, for every data type; a field that
    names a row Bambuddy has no id for is left out."""
    desired, _ = desired_and_unmapped(conn, spool_id, refs, _EVERY, facts)
    return {n: compare_form(n, v) for n, v in desired.items() if next(pendings(v), None) is None}


def _row(
    conn: Conn,
    write: BbWrite,
    result: BbWriteResult,
    item: Mapping[str, Any],
    bb_id: int,
    now: datetime,
    reads: _Reads,
) -> None:
    kind = ROW_KINDS[write.kind]
    try:
        answer = _ROW_MODELS[write.kind].model_validate(result.answer)
    except ValidationError:
        raise _IgnoredError("the answer is not a Bambuddy row of the write's kind") from None
    if answer.id != bb_id:
        raise _IgnoredError("the answer is another Bambuddy row")
    seen = row_seen(answer, kind.readable)
    sb = reads.desired(conn, write.kind, write.sb_id) or {}
    merged = _merge(item["bb_seen"], seen, write, lambda n, v: n in sb and sb[n] == v)
    conn.execute(
        f"UPDATE {kind.table} SET {kind.bb_column} = %s, bb_pushed = bb_pushed || %s,"
        " bb_seen = %s, bb_seen_at = %s WHERE id = %s",
        (bb_id, Jsonb(write.hashes), Jsonb(merged), now, write.sb_id),
    )


def record_pass(store: Filament, result: SyncPassResult) -> None:
    """Write the state row for one pass (Decision 19): ``last_pass_at``,
    ``last_pass_started_at`` (the pass's own start, #6836) and the counts always,
    ``last_error`` from ``result.error``, and ``last_ok_at`` only when it is ``None``, so a
    failed pass keeps the last clean one's time. #6905: and the pass's history row
    (`bb_sync_passes.insert_on`) in the same transaction; a pass that found the links or
    its instance another Bambuddy's (``instance_changed``) gets the history row only, so
    the state stays the configured instance's."""
    counts = {
        "written": result.written,
        "failed": result.failed,
        "deferred": result.deferred,
        "conflicts": result.conflicts,
        "unmapped_k": result.unmapped_k,
    }
    with store.spools.transaction() as conn:
        bb_sync_passes.insert_on(conn, result)
        if result.instance_changed:
            return
        conn.execute(
            "UPDATE filament_bb_sync_state SET last_pass_at = now(),"
            " last_pass_started_at = COALESCE(%s, now()),"
            " last_ok_at = CASE WHEN %s THEN now() ELSE last_ok_at END,"
            " last_error = %s, counts = %s",
            (result.started_at, result.error is None, result.error, Jsonb(counts)),
        )


#: R-T11-1: each error kind's table and the label it shows: a spool's `#N` (the spool
#: list's name for it), a spool type's code (the Bambuddy filament's name, Decision 14),
#: any other row's name.
_LABELS: dict[Kind, tuple[str, str]] = {
    "spool": ("filament_spools", "'#' || number"),
    "supplier": ("filament_suppliers", "name"),
    "location": ("filament_locations", "name"),
    "insert_type": ("filament_insert_types", "name"),
    "filament": ("filament_spool_types", "code"),
}


def _labels(conn: Conn, rows: list[Any]) -> dict[tuple[str, UUID], str]:
    """The label of each error row's item, one query per kind present."""
    ids: dict[Kind, list[UUID]] = {}
    for row in rows:
        ids.setdefault(row["kind"], []).append(row["sb_id"])
    found: dict[tuple[str, UUID], str] = {}
    for kind, sb_ids in ids.items():
        table, label = _LABELS[kind]
        for hit in conn.execute(
            f"SELECT id, {label} AS label FROM {table} WHERE id = ANY(%s)",
            (sb_ids,),
        ).fetchall():
            found[(kind, hit["id"])] = hit["label"]
    return found


def sync_status(
    store: Filament,
    directions: FilamentSync,
    skipped: SkipReason | None,
    configured: str | None = None,
) -> SyncStatus:
    """The state row and the error rows (Decision 19), newest error first, with the
    directions and the skip reason the caller read from the settings. ``configured`` is
    the configured Bambuddy's instance (`instance_for`), ``None`` with none:
    ``instance_changed`` is whether the links belong to another one, as the plan judges
    it (Decision 12, #6773)."""
    with store.spools.transaction() as conn:
        state = conn.execute(
            "SELECT instance, last_pass_at, last_pass_started_at, last_ok_at, last_error, counts"
            " FROM filament_bb_sync_state"
        ).fetchone()
        errors = conn.execute(
            "SELECT kind, sb_id, error, attempts, at FROM filament_bb_push_errors"
            " ORDER BY at DESC, kind, sb_id"
        ).fetchall()
        labels = _labels(conn, errors)
    if state is None:
        raise RuntimeError("filament_bb_sync_state has no row; its migration seeds one")
    return SyncStatus(
        instance=state["instance"],
        last_pass_at=state["last_pass_at"],
        last_pass_started_at=state["last_pass_started_at"],
        last_ok_at=state["last_ok_at"],
        last_error=state["last_error"],
        counts=state["counts"],
        errors=[
            SyncError.model_validate(dict(row) | {"label": labels.get((row["kind"], row["sb_id"]))})
            for row in errors
        ],
        directions=directions,
        skipped=skipped,
        instance_changed=configured is not None
        and state["instance"] is not None
        and state["instance"] != configured,
    )


def adopt_instance(store: Filament, instance: str) -> None:
    """Record ``instance`` as the links' Bambuddy when none is recorded and links exist:
    #6261's pull made them before the state row did (Decision 12). A link of a supplier,
    location, insert type, filament, colour or preset counts too
    (`bambuddy_pull.ANY_LINK`). The API calls it at boot with the configured Bambuddy's,
    before its first nudge."""
    with store.spools.transaction() as conn:
        record_instance(conn, instance)


class ResetRefusedError(Exception):
    """A reset of the Bambuddy links refused (#6773); ``code`` is the 409's code:
    ``nothing_to_reset`` (no instance is recorded, so no link belongs to one),
    ``same_instance`` (the links belong to the configured Bambuddy),
    ``confirm_mismatch`` (``confirm_instance`` is not the links' instance) or
    ``proof_changed`` (the caller expected the links kept, but the configured Bambuddy
    could not be read or is no longer proven the links' own, #6845 R-6845-6)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


#: The rows a link of each kind is kept on, and its Bambuddy id column (#6773).
_ROW_LINKS: tuple[tuple[str, str, str], ...] = (
    ("suppliers", "filament_suppliers", "bb_supplier_id"),
    ("locations", "filament_locations", "bb_location_id"),
    ("insert_types", "filament_insert_types", "bb_catalog_id"),
    ("filaments", "filament_spool_types", "bb_filament_id"),
)


def _reset_counts(conn: Conn, instance: str | None) -> LinksResetCounts:
    """What a reset deletes now: one count per link kind, the intents, the error rows
    and the ignored Bambuddy spools. ``presets``: the ScadBuddy presets whose Bambuddy id
    a delete of ``instance``'s links drops (`reset_links`' own condition)."""
    counts: dict[str, int] = {}
    for name, table in (
        ("spools", "filament_bb_links"),
        ("intents", "filament_bb_creates"),
        ("errors", "filament_bb_push_errors"),
        ("ignored", "filament_bb_ignored"),
    ):
        row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
        counts[name] = row["n"] if row is not None else 0
    for name, table, column in _ROW_LINKS:
        row = conn.execute(
            f"SELECT count(*) AS n FROM {table} WHERE {column} IS NOT NULL"
        ).fetchone()
        counts[name] = row["n"] if row is not None else 0
    row = conn.execute(
        "SELECT count(*) AS n FROM filament_colours WHERE bb_colour_id IS NOT NULL"
    ).fetchone()
    counts["colours"] = row["n"] if row is not None else 0
    row = conn.execute(
        "SELECT count(*) AS n FROM filament_presets WHERE bb_preset_id IS NOT NULL"
        " AND (bb_instance IS NULL OR bb_instance = %s)",
        (instance,),
    ).fetchone()
    counts["presets"] = row["n"] if row is not None else 0
    return LinksResetCounts(**counts)


def _proof_by(
    links: Mapping[int, datetime | None], held: Mapping[int, datetime | None] | None
) -> tuple[bool, bool]:
    """``(same, changed)``: the rule of `same_bambuddy` and `changed_since_pull` over the
    links (a Bambuddy id to ScadBuddy's time of it) and what Bambuddy holds (an id to its
    time; ``None``: the listing proves nothing)."""
    if not links or held is None:
        return False, False
    exact = False
    timed = 0
    for bb_id, seen in links.items():
        if bb_id not in held:
            return False, False
        now = held[bb_id]
        if seen is None or now is None:
            continue
        if now < seen:
            return False, False
        timed += 1
        exact = exact or now == seen
    enough = timed * 2 >= len(links)
    return exact and enough, enough and not exact


def _proof(
    links: Mapping[int, datetime | None], rows: Sequence[BbSpool], bad: Sequence[int | None]
) -> tuple[bool, bool]:
    """``(same, changed)`` for `same_bambuddy` and `changed_since_pull`."""
    return _proof_by(links, None if bad else {row.id: row.updated_at for row in rows})


def _stamp(value: str | None) -> datetime | None:
    """A Bambuddy local preset's ``updated_at`` as text (naive UTC, as Bambuddy sends
    it), as an aware time; ``None`` when absent or not a time."""
    if value is None:
        return None
    try:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


#: The configured Bambuddy's filament local presets, read for the proof from the presets
#: when no spool is linked (#7343); ``None``: not read, or the read failed.
PresetListing = Sequence[LocalPreset] | None


def _preset_links(conn: Conn, instance: str | None) -> dict[int, datetime | None]:
    """The presets' links a delete of ``instance``'s links drops (`_reset_counts`), each
    Bambuddy id with Bambuddy's ``updated_at`` as ScadBuddy last took it (``bb_updated_at``):
    the mirror's last write, or for a preset imported from Bambuddy, its last import."""
    return {
        row["bb_preset_id"]: _stamp(row["bb_updated_at"])
        for row in conn.execute(
            "SELECT bb_preset_id, bb_updated_at FROM filament_presets"
            " WHERE bb_preset_id IS NOT NULL AND (bb_instance IS NULL OR bb_instance = %s)",
            (instance,),
        )
    }


def wants_preset_listing(store: Filament) -> bool:
    """Whether the proof of the same Bambuddy stands on the presets (#7343): no spool is
    linked, and a preset holds a Bambuddy id of the links' Bambuddy (`_preset_links`).
    Then the reset reads Bambuddy's presets."""
    with store.spools.transaction() as conn:
        row = conn.execute(
            "SELECT NOT EXISTS (SELECT 1 FROM filament_bb_links)"
            " AND EXISTS (SELECT 1 FROM filament_presets p WHERE p.bb_preset_id IS NOT NULL"
            " AND (p.bb_instance IS NULL OR p.bb_instance ="
            " (SELECT instance FROM filament_bb_sync_state LIMIT 1))) AS w"
        ).fetchone()
    return bool(row is not None and row["w"])


def same_bambuddy(
    links: Mapping[int, datetime | None], rows: Sequence[BbSpool], bad: Sequence[int | None]
) -> bool:
    """Whether a Bambuddy whose spools are ``rows`` (``bad``: the rows that did not parse,
    `bambuddy_pull.parse_rows`) is the one the spool ``links`` were made against (#6845
    R-6845-2). Bambuddy has no instance id of its own, so it is proven by its spools: it
    holds every linked BB spool id (a link maps to its ``bb_updated_at``), none older
    than ScadBuddy last saw it, and at least one exactly as then. A link with no time on
    either side (a pre-#6265 link, or a row Bambuddy sends with no ``updated_at``) is
    only checked to be there, so at least half the links must have a time on both sides
    (Decision 199): a few exact matches among many unchecked links prove too little. A
    listing with a row that did not parse, or no spool link at all, proves nothing."""
    return _proof(links, rows, bad)[0]


def changed_since_pull(
    links: Mapping[int, datetime | None], rows: Sequence[BbSpool], bad: Sequence[int | None]
) -> bool:
    """Why `same_bambuddy` is false although nothing contradicts it: Bambuddy holds every
    linked spool, none older than its link, at least half the links are timed on both
    sides, but not one is exactly as ScadBuddy last saw it, so every timed linked spool
    was edited in Bambuddy since the last pull and nothing ties this Bambuddy to the
    links. The reset offers only the delete then (#6845 review)."""
    return _proof(links, rows, bad)[1]


def _proof_of(
    conn: Conn,
    listing: tuple[Sequence[BbSpool], Sequence[int | None]] | None,
    presets: PresetListing,
    instance: str | None,
) -> tuple[bool, bool]:
    """`same_bambuddy` and `changed_since_pull` for the links as they stand: by the spool
    links when there are any (``listing``, the configured Bambuddy's spools), else by the
    presets' links of ``instance`` (``presets``, its filament local presets, #7343) under
    the same rule. A preset's time is Bambuddy's ``updated_at`` as ScadBuddy last took it: the
    mirror's last write (`preset_mirror._mirrored`), or for a preset imported from Bambuddy,
    its last import (`sb_presets.import_presets`, `refresh_bambuddy`). Either is Bambuddy's
    ``onupdate=func.now()``, so another Bambuddy would need a preset under the same id
    written at the same recorded time. A preset with
    no time (sent before the mirror kept one, and not written by the mirror since) is only
    checked to be there, and counts against the rule that at least half the links are timed
    on both sides."""
    links = {
        row["bb_spool_id"]: row["bb_updated_at"]
        for row in conn.execute("SELECT bb_spool_id, bb_updated_at FROM filament_bb_links")
    }
    if links:
        return (False, False) if listing is None else _proof(links, *listing)
    held = None if presets is None else {p.id: _stamp(p.updated_at) for p in presets}
    return _proof_by(_preset_links(conn, instance), held)


#: What `parse_rows` makes of the configured Bambuddy's spools: the rows, and the ids of
#: those that did not parse. ``None``: no Bambuddy is configured, so nothing was read.
Listing = tuple[Sequence[BbSpool], Sequence[int | None]] | None


def reset_preview(
    store: Filament,
    configured: str | None,
    listing: Listing,
    *,
    presets: PresetListing = None,
    unreadable: bool = False,
) -> LinksResetPreview:
    """The dry run of `reset_links`: the links' instance, the configured one, what a
    reset would delete, and whether the configured Bambuddy, whose spools are
    ``listing`` and whose filament local presets are ``presets`` (read only when no spool
    is linked, `wants_preset_listing`), is the links' own (#6845, #7343: `_proof_of`), so
    a reset with ``expect_kept`` keeps the links. ``unreadable``: the configured Bambuddy
    could not be read (``listing`` is then ``None``), which the answer says (Decision
    198). Reads only."""
    with store.spools.transaction() as conn:
        state = conn.execute("SELECT instance FROM filament_bb_sync_state").fetchone()
        instance = state["instance"] if state is not None else None
        counts = _reset_counts(conn, instance)
        same, changed = (
            (False, False) if listing is None else _proof_of(conn, listing, presets, instance)
        )
    return LinksResetPreview(
        instance=state["instance"] if state is not None else None,
        configured_instance=configured,
        counts=counts,
        same_bambuddy=same,
        changed_since_pull=changed,
        bambuddy_unreadable=unreadable,
    )


def reset_links(
    store: Filament,
    confirm_instance: str,
    configured: str | None,
    listing: Listing,
    *,
    presets: PresetListing = None,
    expect_kept: bool,
) -> LinksResetDone:
    """Reset the Bambuddy links after the Bambuddy URL changed, in one transaction under
    the state row's lock, which `apply_results` takes too. Refused (`ResetRefusedError`)
    when no instance is recorded, when it is ``configured`` or when it is not
    ``confirm_instance``.

    With ``expect_kept`` (the preview proved the same Bambuddy, #6845 R-6845-6), the
    configured Bambuddy, whose spools are ``listing`` (``None``: it could not be read)
    and, when no spool is linked, whose filament local presets are ``presets`` (#7343),
    must still be the links' own (`_proof_of`); then nothing is deleted: the state
    row records ``configured`` as the links' instance, so every link and intent stays
    (R-6845-3), and so does the usage mark, under ``configured`` (R-7002-6). When it is
    not, the reset is refused ``proof_changed`` and writes nothing, so a Bambuddy that
    changed between the preview and the reset never turns "Keep links" into a delete.
    Without ``expect_kept`` (the person confirmed the delete, Decision 198; ``listing`` is
    not read) every Bambuddy link and what the sync keeps of the old Bambuddy is deleted
    (#6773): the
    spool links, each supplier's, location's, insert type's and spool type's Bambuddy id
    with its hashes and seen copy, the open create intents, the error rows, the ignored
    Bambuddy spools, the state row's ``instance`` and its usage mark (R-7002-6), and the
    old Bambuddy's preset ids in profile fetch runs' ``files`` (#7223). No
    ScadBuddy spool, type, supplier or location is deleted, and none of their own fields
    changes."""
    with store.spools.transaction() as conn:
        state = conn.execute("SELECT instance FROM filament_bb_sync_state FOR UPDATE").fetchone()
        instance = state["instance"] if state is not None else None
        if instance is None:
            raise ResetRefusedError("nothing_to_reset")
        if instance == configured:
            raise ResetRefusedError("same_instance")
        if confirm_instance != instance:
            raise ResetRefusedError("confirm_mismatch")
        if expect_kept:
            if (
                configured is None
                or listing is None
                or not _proof_of(conn, listing, presets, instance)[0]
            ):
                raise ResetRefusedError("proof_changed")
            # The same Bambuddy: its usage mark stays and moves to the new name (R-7002-6).
            conn.execute(
                "UPDATE filament_bb_sync_state SET instance = %s, usage_read_instance ="
                " CASE WHEN usage_read_instance = %s THEN %s ELSE usage_read_instance END",
                (configured, instance, configured),
            )
            # ScadBuddy's presets' Bambuddy ids are the same Bambuddy's too (#7176).
            conn.execute(
                "UPDATE filament_presets SET bb_instance = %s WHERE bb_instance = %s",
                (configured, instance),
            )
            logger.info(
                "the Bambuddy links were kept: the new URL reaches the same Bambuddy",
                extra={"instance": instance, "configured": configured},
            )
            return LinksResetDone(
                instance=instance,
                kept=True,
                configured_instance=configured,
                deleted=LinksResetCounts(),
            )
        counts = _reset_counts(conn, instance)
        for table in (
            "filament_bb_links",
            "filament_bb_creates",
            "filament_bb_push_errors",
            "filament_bb_ignored",
        ):
            conn.execute(f"DELETE FROM {table}")
        for _, table, column in _ROW_LINKS:
            conn.execute(
                f"UPDATE {table} SET {column} = NULL, bb_pushed = '{{}}', bb_seen = NULL,"
                f" bb_seen_at = NULL WHERE {column} IS NOT NULL OR bb_seen IS NOT NULL"
                " OR bb_pushed <> '{}'"
            )
        # A colour's link only: the pull never pushes a colour, so it has no hashes (#6907).
        conn.execute(
            "UPDATE filament_colours SET bb_colour_id = NULL WHERE bb_colour_id IS NOT NULL"
        )
        # ScadBuddy's presets (#7176): an id of the old Bambuddy names nothing on the new
        # one. Each preset is sent to the new one by the next mirror, a preset made in
        # the old Bambuddy too, which ScadBuddy now holds as its own (`manual`).
        conn.execute(
            "UPDATE filament_presets SET bb_preset_id = NULL, bb_instance = NULL,"
            " bb_updated_at = NULL, mirrored_sha256 = NULL, mirror_create_floor = NULL,"
            " source = CASE WHEN source = 'bambuddy' THEN 'manual' ELSE source END"
            " WHERE bb_preset_id IS NOT NULL AND (bb_instance IS NULL OR bb_instance = %s)",
            (instance,),
        )
        # A profile fetch run's ``preset_id``s name presets of the old Bambuddy too; the new
        # one's ids would match them and make an adopted preset ``3dfp`` of the old run's
        # key (`sb_presets._fetched_key`, #7223). Runs since #7201 write none.
        conn.execute(
            "UPDATE filament_profile_fetches SET files = ("
            " SELECT coalesce(jsonb_object_agg(m.key, CASE WHEN jsonb_typeof(m.value) = 'object'"
            " THEN (SELECT coalesce(jsonb_object_agg(n.key, CASE WHEN jsonb_typeof(n.value)"
            " = 'object' THEN n.value - 'preset_id' ELSE n.value END), '{}'::jsonb)"
            " FROM jsonb_each(m.value) AS n) ELSE m.value END), '{}'::jsonb)"
            " FROM jsonb_each(files) AS m)"
            " WHERE jsonb_typeof(files) = 'object' AND jsonb_path_exists(files, '$.*.*.preset_id')"
        )
        conn.execute(
            "UPDATE filament_bb_sync_state SET instance = NULL, usage_read_instance = NULL,"
            " usage_read_at = NULL, usage_read_id = NULL"
        )
    logger.info("the Bambuddy links were reset", extra={"instance": instance})
    return LinksResetDone(instance=instance, kept=False, deleted=counts)


class LocalFilamentSync:
    """`FilamentSyncApi` over SB's own stores, which the CRUD routes call; the print
    worker's `RemoteFilamentSync` gives its answers through them, and the API's in-process
    print queue runs `InProcessFilamentSync`, this class waiting out a busy sync. It reads
    ``filament_sync`` and the timer's interval (``spool_overlay_refresh_seconds``) on each
    plan, so a change applies to the next.
    A plan or results call for another instance than the configured Bambuddy's plans
    nothing (``instance_changed``) and records nothing: after a reset of the links
    (#6773) the state row names no instance, so a pass of the old Bambuddy's workflow
    still running must not create rows there or link them again."""

    def __init__(self, filament: Filament, settings_store: SettingsStore) -> None:
        self._filament = filament
        self._settings = settings_store

    async def directions(self) -> FilamentSync:
        read = await asyncio.to_thread(self._settings.read, {"filament_sync"})
        sync: FilamentSync = read["filament_sync"]
        return sync

    async def plan(
        self, snap: BbSnapshot, limit: int, cancel: CancelToken | None = None
    ) -> SyncPlan:
        """``cancel``: the CRUD route's (`api/disconnect.py`), cancelled when its caller
        goes away; the plan then writes nothing unless it had already passed its last
        check (`plan`). Another plan or results call of the
        instance still running is `SyncBusyError`."""

        def run() -> SyncPlan:
            read = self._settings.read(
                {"filament_sync", "spool_overlay_refresh_seconds", "bambuddy_url"}
            )
            interval_s = float(read["spool_overlay_refresh_seconds"])
            if _foreign(read["bambuddy_url"], snap.instance):
                return SyncPlan(
                    writes=[],
                    deferred=0,
                    directions=read["filament_sync"],
                    interval_s=interval_s,
                    instance_changed=True,
                )
            return plan(
                self._filament,
                snap,
                read["filament_sync"],
                interval_s=interval_s,
                limit=limit,
                cancel=cancel,
            )

        planned = await asyncio.to_thread(run)
        # One id per plan round (R-T6-5): the worker sends it back with the round's results.
        return planned.model_copy(update={"pass_id": uuid4().hex})

    async def results(
        self,
        instance: str,
        writes: Sequence[BbWrite],
        results: Sequence[BbWriteResult],
        pass_id: str | None,
        unconfirmed: Sequence[tuple[Kind, UUID]] = (),
        cancel: CancelToken | None = None,
    ) -> ResultsReport:
        """``cancel`` as `plan`'s: the round then writes nothing unless it had already
        passed its last check (`apply_results`)."""
        read = await asyncio.to_thread(self._settings.read, {"bambuddy_url"})
        # This reads bambuddy_url before apply_results takes the state row's lock. After a
        # reset the row's instance is NULL, so this check is what stops an old pass's
        # results. It holds because a reset is refused until the URL has changed
        # (Decisions 165, 168).
        if _foreign(read["bambuddy_url"], instance):
            logger.warning(
                "Bambuddy sync results for another instance are ignored",
                extra={"instance": instance},
            )
            return ResultsReport()
        return await asyncio.to_thread(
            apply_results,
            self._filament,
            writes,
            results,
            instance=instance,
            now=datetime.now(UTC),
            pass_id=pass_id,
            unconfirmed=unconfirmed,
            cancel=cancel,
        )

    async def record_pass(self, result: SyncPassResult) -> None:
        await asyncio.to_thread(record_pass, self._filament, result)


class InProcessFilamentSync(LocalFilamentSync):
    """`LocalFilamentSync` for the pass that the API's in-process print queue runs. Its
    plan and results calls wait out another call's `SyncBusyError` (#6910 review): the
    print worker's `RemoteFilamentSync` waits out the route's 503 ``sync_busy`` within
    its ``BUSY_BUDGET``, and this waits within the same budget, trying again every
    ``BUSY_BACKOFF_S``, so a second caller does not fail the pass at once. ``clock`` and
    ``sleep`` are for tests."""

    BUSY_BUDGET = 60.0
    BUSY_BACKOFF_S = 1.0

    def __init__(
        self,
        filament: Filament,
        settings_store: SettingsStore,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(filament, settings_store)
        self._clock = clock
        self._sleep = sleep

    async def _until_free[T](self, call: Callable[[], Awaitable[T]]) -> T:
        deadline = self._clock() + self.BUSY_BUDGET
        while True:
            try:
                return await call()
            except SyncBusyError:
                if self._clock() + self.BUSY_BACKOFF_S > deadline:
                    raise
            await self._sleep(self.BUSY_BACKOFF_S)

    async def plan(
        self, snap: BbSnapshot, limit: int, cancel: CancelToken | None = None
    ) -> SyncPlan:
        return await self._until_free(lambda: LocalFilamentSync.plan(self, snap, limit, cancel))

    async def results(
        self,
        instance: str,
        writes: Sequence[BbWrite],
        results: Sequence[BbWriteResult],
        pass_id: str | None,
        unconfirmed: Sequence[tuple[Kind, UUID]] = (),
        cancel: CancelToken | None = None,
    ) -> ResultsReport:
        return await self._until_free(
            lambda: LocalFilamentSync.results(
                self, instance, writes, results, pass_id, unconfirmed, cancel
            )
        )


def _foreign(bambuddy_url: str | None, instance: str) -> bool:
    """Whether ``instance`` is not the configured Bambuddy's. With none configured no
    pass starts (`skip_reason`), so nothing is foreign."""
    return bool(bambuddy_url) and instance_for(cast("str", bambuddy_url)) != instance
