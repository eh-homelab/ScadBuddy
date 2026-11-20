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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from scadbuddy.bambuddy.models import BbLocation, BbSupplier, CatalogFilament, SpoolCatalogEntry
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament import ledger
from scadbuddy.filament.bambuddy_pull import REF_KIND, snapshot
from scadbuddy.filament.bb_sync_models import (
    BbSnapshot,
    BbWrite,
    BbWriteResult,
    Kind,
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
from scadbuddy.filament.bb_sync_plan import (
    ROW_KINDS,
    UNSEEN,
    BbRefs,
    bb_form,
    compare_form,
    desired_and_unmapped,
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
_GONE_ON_404 = frozenset({"update", "archive", "restore", "probe"})
#: The writes Bambuddy answers with the spool's whole row (``SpoolResponse`` in
#: `sync-routes.json`; a probe is ``GET spool_row``). A list ``PUT`` answers its list.
_FULL_ROW = frozenset({"create", "update", "archive", "restore", "probe"})
#: The spool writes that change Bambuddy's row, and so book Decision 9's event and move
#: the link's ``bb_updated_at``.
_ECHOED = frozenset({"create", "update", "archive", "restore"})
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

    def desired(self, conn: Conn, kind: str, sb_id: UUID) -> dict[str, Any] | None:
        if kind not in self.rows:
            self.rows[kind] = {item.sb_id: item.desired for item in ROW_KINDS[kind].items(conn)}
        return self.rows[kind].get(sb_id)

    def bb_refs(self, conn: Conn) -> BbRefs:
        if self.refs is None:
            self.refs = BbRefs.read(conn)
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
) -> ResultsReport:
    """Record what Bambuddy answered to ``plan_writes``, as the module says, each result
    in a transaction of its own. ``now`` is the stored seen copies' ``bb_seen_at``.

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
    its own, so it always adds one. The state row's instance
    is set by the first result that links anything (Decision 12), and results for
    another instance are all ignored."""
    report = ResultsReport()
    reads = _Reads()
    writes = {write.key: write for write in plan_writes}
    for result in results:
        write = writes.get(result.key)
        try:
            if write is None:
                raise _IgnoredError("no write has this key")
            with store.spools.transaction() as conn:
                state = conn.execute(
                    "SELECT instance FROM filament_bb_sync_state FOR UPDATE"
                ).fetchone()
                if state is not None and state["instance"] not in (None, instance):
                    raise _IgnoredError("the links belong to another Bambuddy")
                outcome = _apply(conn, write, result, instance, now, reads, pass_id)
        except _IgnoredError as why:
            logger.warning(
                "a Bambuddy sync result is ignored",
                extra={"key": result.key, "reason": str(why)},
            )
            continue
        if outcome is not None:
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
    if write.kind == "spool":
        _spool(conn, write, result, item, bb_id, now, reads)
    else:
        _row(conn, write, result, item, bb_id, now, reads)
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
    sb = _spool_compare(conn, write.sb_id, reads.bb_refs(conn))
    merged = _merge(
        item["bb_seen"], seen, write, lambda n, _v: n in sb and bb_form(n, seen, k_rows) == sb[n]
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


def _spool_compare(conn: Conn, spool_id: UUID, refs: BbRefs) -> dict[str, Any]:
    """SB's values of the spool in the compare form, for every data type; a field that
    names a row Bambuddy has no id for is left out."""
    desired, _ = desired_and_unmapped(conn, spool_id, refs, _EVERY)
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
    """Write the state row for one pass (Decision 19): ``last_pass_at`` and the counts
    always, ``last_error`` from ``result.error``, and ``last_ok_at`` only when it is
    ``None``, so a failed pass keeps the last clean one's time."""
    counts = {
        "written": result.written,
        "failed": result.failed,
        "deferred": result.deferred,
        "conflicts": result.conflicts,
        "unmapped_k": result.unmapped_k,
    }
    with store.spools.transaction() as conn:
        conn.execute(
            "UPDATE filament_bb_sync_state SET last_pass_at = now(),"
            " last_ok_at = CASE WHEN %s THEN now() ELSE last_ok_at END,"
            " last_error = %s, counts = %s",
            (result.error is None, result.error, Jsonb(counts)),
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
    store: Filament, directions: FilamentSync, skipped: SkipReason | None
) -> SyncStatus:
    """The state row and the error rows (Decision 19), newest error first, with the
    directions and the skip reason the caller read from the settings."""
    with store.spools.transaction() as conn:
        state = conn.execute(
            "SELECT instance, last_pass_at, last_ok_at, last_error, counts"
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
        last_ok_at=state["last_ok_at"],
        last_error=state["last_error"],
        counts=state["counts"],
        errors=[
            SyncError.model_validate(dict(row) | {"label": labels.get((row["kind"], row["sb_id"]))})
            for row in errors
        ],
        directions=directions,
        skipped=skipped,
    )


def adopt_instance(store: Filament, instance: str) -> None:
    """Record ``instance`` as the links' Bambuddy when none is recorded and links exist:
    #6261's pull made them before the state row did (Decision 12). The API calls it at
    boot with the configured Bambuddy's, before its first nudge."""
    with store.spools.transaction() as conn:
        conn.execute(
            "UPDATE filament_bb_sync_state SET instance = %s"
            " WHERE instance IS NULL AND EXISTS (SELECT 1 FROM filament_bb_links)",
            (instance,),
        )


class LocalFilamentSync:
    """`FilamentSyncApi` over SB's own stores, for the API's in-process print queue. The
    print worker's `RemoteFilamentSync` gives the same answers through the CRUD routes,
    which call this class. It reads ``filament_sync`` and the timer's interval
    (``spool_overlay_refresh_seconds``) on each plan, so a change applies to the next."""

    def __init__(self, filament: Filament, settings_store: SettingsStore) -> None:
        self._filament = filament
        self._settings = settings_store

    async def directions(self) -> FilamentSync:
        read = await asyncio.to_thread(self._settings.read, {"filament_sync"})
        sync: FilamentSync = read["filament_sync"]
        return sync

    async def plan(self, snap: BbSnapshot, limit: int) -> SyncPlan:
        def run() -> SyncPlan:
            read = self._settings.read({"filament_sync", "spool_overlay_refresh_seconds"})
            return plan(
                self._filament,
                snap,
                read["filament_sync"],
                interval_s=float(read["spool_overlay_refresh_seconds"]),
                limit=limit,
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
    ) -> ResultsReport:
        return await asyncio.to_thread(
            apply_results,
            self._filament,
            writes,
            results,
            instance=instance,
            now=datetime.now(UTC),
            pass_id=pass_id,
        )

    async def record_pass(self, result: SyncPassResult) -> None:
        await asyncio.to_thread(record_pass, self._filament, result)
