"""ScadBuddy's own spool inventory (#4643, tier B of #3818): the records the native spool
provider (`spools/spool_providers.py`) offers the overlay.

Since #6261 (Task 8) they live in the filament tables (`filament/spools.py`): a native
spool is a ``filament_spools`` row, read with its effective values, and its weight is its
ledger's grams left (`filament/ledger.py`). A person keeps them
(``/api/v1/native-spools``, `api/native_spools.py`), and a native print's uses are ledger
events: ``print_started`` as the printer takes the plate (its estimate comes off at
once), ``print_ended`` when the run settles (:meth:`NativeSpoolStore.settle_uses`). A
record's RFID keys are cleaned as the overlay cleans a tray's
(`spools.models.clean_tray_uuid`, `clean_tag_uid`), so a record a person types from a
tray's report finds that tray's spool; one unarchived spool holds a tag (an open
binding).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from scadbuddy.filament import ledger
from scadbuddy.filament.colour import normalise_rgba
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.spools.models import SpoolSnapshot, clean_tag_uid, clean_tray_uuid

if TYPE_CHECKING:
    from scadbuddy.bambuddy.filaments import SpoolOption


Conn = Connection[DictRow]

#: What a tag may be typed as: hex digits, with the separators a reader prints.
_TAG_TEXT = re.compile(r"^[0-9A-Fa-f:\- ]*$")


def _tag(clean: Callable[[str | None], str | None]) -> Callable[[str | None], str | None]:
    def check(value: str | None) -> str | None:
        if value is None:
            return None
        if not _TAG_TEXT.fullmatch(value):
            raise ValueError("a tag is hex digits, as the printer reports it")
        return clean(value)

    return check


def _colour(value: str | None) -> str | None:
    return None if value is None else value.upper()


TrayUuid = Annotated[str | None, Field(max_length=64), AfterValidator(_tag(clean_tray_uuid))]
TagUid = Annotated[str | None, Field(max_length=64), AfterValidator(_tag(clean_tag_uid))]
Colour = Annotated[
    str | None,
    Field(pattern=r"^#[0-9A-Fa-f]{6}$", description="#RRGGBB"),
    AfterValidator(_colour),
]
Text = Annotated[str | None, Field(max_length=128)]
Grams = Annotated[float | None, Field(ge=0, le=100_000)]
LabelWeight = Annotated[int | None, Field(gt=0, le=100_000)]
Note = Annotated[str | None, Field(max_length=2000)]


class NativeSpoolCreate(BaseModel):
    """A spool on ScadBuddy's own shelf. Every field may be left out; a tag of all zeros
    (an untagged spool's report) is stored as none."""

    model_config = ConfigDict(extra="forbid")

    material: Text = None
    colour: Colour = None
    brand: Text = None
    #: What a person calls it: the colour's name, "Galaxy Black".
    name: Text = None
    #: The filament's weight when full, as the label says.
    label_weight_g: LabelWeight = None
    remaining_g: Grams = None
    tray_uuid: TrayUuid = None
    tag_uid: TagUid = None
    #: The filament preset id it prints with (``GFA00``), as a tray's ``tray_info_idx``.
    preset: Text = None
    note: Note = None


class NativeSpoolPatch(NativeSpoolCreate):
    """Only the fields sent change (``model_fields_set``); ``null`` clears one.
    ``archived`` takes the spool off the shelf (``true``) or puts it back."""

    archived: bool | None = None


class NativeSpool(BaseModel):
    id: UUID
    material: str | None
    colour: str | None
    brand: str | None
    name: str | None
    label_weight_g: int | None
    remaining_g: float | None
    tray_uuid: str | None
    tag_uid: str | None
    preset: str | None
    note: str | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def snapshot(self) -> SpoolSnapshot:
        """The record as the overlay reads every provider's (`SpoolSnapshot`)."""
        return SpoolSnapshot(
            record_id=str(self.id),
            material=self.material,
            colour=self.colour,
            brand=self.brand,
            name=self.name,
            remaining_g=self.remaining_g,
            label_weight_g=self.label_weight_g,
            tray_uuid=self.tray_uuid,
            tag_uid=self.tag_uid,
            preset=self.preset,
        )


class SpoolUse(BaseModel):
    """What one slot of a LAN start uses of a native spool: its ``print_started``
    estimate in ``filament_spool_events`` (`NativeSpoolStore.record_uses`)."""

    slot_id: int
    spool_id: UUID
    grams: float


def spool_uses(spools: Mapping[int, SpoolOption], grams: Mapping[int, float]) -> list[SpoolUse]:
    """Each slot's use of the native spool in its tray, by slot: the overlay's native
    record of the slot's chosen tray (`SpoolOption.records`, found there by RFID or a
    person's tray link), and the grams the sliced plate gives the slot
    (`slicing.slice_info.plate_grams`). A slot with no native record, or that prints
    nothing, uses none."""
    uses: list[SpoolUse] = []
    for slot_id in sorted(spools):
        record = spools[slot_id].records.get("native")
        used = grams.get(slot_id, 0.0)
        if record is None or used <= 0:
            continue
        try:
            spool_id = UUID(record.record_id)
        except ValueError:
            continue
        uses.append(SpoolUse(slot_id=slot_id, spool_id=spool_id, grams=used))
    return uses


class NativeSpoolExistsError(ValueError):
    """An unarchived spool already carries the tag."""

    def __init__(self) -> None:
        super().__init__("another spool on the shelf carries that tag")


#: Who the ledger events of the native API name: it has no caller.
ACTOR = "native"
#: The material of a spool that names none (the 1302Z migration's).
UNKNOWN = "Unknown"
#: The preset a native spool keeps: it names no printer model or nozzle (#6262 adds them).
_ANY = "*"
#: A native spool read off the filament tables: its effective brand, material, colour,
#: name and label weight (the spool's own, else its type's; `filament/types.py`
#: `effective`), its ledger's grams left, its ``*`` preset and the tag it shows (the open
#: binding's, or, archived, the one its archive closed). ``Unknown``, the material a spool
#: that names none is given, reads as none, as the native record had it.
_READ = """
SELECT s.id, CASE WHEN upper(mat.name) <> upper(%(unknown)s) THEN mat.name END AS material,
       left(coalesce(s.rgba, t.rgba), 7) AS colour,
       br.name AS brand, coalesce(s.colour_name, t.colour_name) AS name,
       coalesce(s.label_weight_g, t.label_weight_g) AS label_weight_g,
       s.grams_left AS remaining_g,
       CASE WHEN tag.shown THEN tag.tray_uuid END AS tray_uuid,
       CASE WHEN tag.shown THEN tag.tag_uid END AS tag_uid,
       (SELECT p.slicer_filament FROM filament_spool_presets p WHERE p.spool_id = s.id
        AND p.printer_model = '*' AND p.nozzle_diameter = '*') AS preset,
       s.note, s.archived_at, s.created_at, s.updated_at
FROM filament_spools s
LEFT JOIN filament_spool_types t ON t.id = s.type_id
LEFT JOIN filament_materials mat ON mat.id = coalesce(s.material_id, t.material_id)
LEFT JOIN filament_brands br ON br.id = coalesce(s.brand_id, t.brand_id)
LEFT JOIN LATERAL (
    SELECT g.tray_uuid, g.tag_uid, (b.unbound_at IS NULL
        OR (s.archived_at IS NOT NULL AND b.reason = 'archived')) AS shown
    FROM filament_tag_bindings b JOIN filament_tags g ON g.id = b.tag_id
    WHERE b.spool_id = s.id
    ORDER BY b.unbound_at IS NULL DESC, b.unbound_at DESC, b.bound_at DESC
    LIMIT 1
) tag ON true
"""
#: The binding a spool's tag reads from (as `_READ` picks it), with its tag's keys.
_SHOWN = (
    "SELECT b.id, b.tag_id, b.unbound_at, b.reason, g.tag_uid, g.tray_uuid"
    " FROM filament_tag_bindings b JOIN filament_tags g ON g.id = b.tag_id"
    " WHERE b.spool_id = %s"
    " ORDER BY b.unbound_at IS NULL DESC, b.unbound_at DESC, b.bound_at DESC LIMIT 1"
)


def _shown(conn: Conn, spool_id: UUID) -> DictRow | None:
    """The binding the spool's tag reads from: its open one, else the one its archive
    closed."""
    row = conn.execute(_SHOWN, (spool_id,)).fetchone()
    if row is not None and row["unbound_at"] is not None and row["reason"] != "archived":
        return None
    return row


#: A native field and the `filament_spools` column it is kept in.
_COLUMN_OF = {"name": "colour_name", "label_weight_g": "label_weight_g", "note": "note"}


def _like_prefix(text: str) -> str:
    """A ``LIKE`` pattern for the strings that start with ``text``."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def _ref(run_id: str, subtask_name: str, slot_id: int) -> str:
    """A use's ``ref_id``, as the 1302Z migration writes it."""
    return f"{run_id}/{subtask_name}/{slot_id}"


def _catalog_id(conn: Conn, table: str, name: str) -> UUID:
    """The unarchived row of ``table`` named ``name`` (case-blind), made when missing."""
    while True:
        found = conn.execute(
            f"SELECT id FROM {table} WHERE archived_at IS NULL AND upper(name) = upper(%s)",
            (name,),
        ).fetchone()
        if found is not None:
            got: UUID = found["id"]
            return got
        made = conn.execute(
            f"INSERT INTO {table} (name, sort_order) VALUES (%s, 1000)"
            " ON CONFLICT DO NOTHING RETURNING id",
            (name,),
        ).fetchone()
        if made is not None:
            new: UUID = made["id"]
            return new


def _material_id(conn: Conn, name: str | None) -> UUID:
    """A native spool's material; one with none is ``Unknown``, as the migration has it."""
    return _catalog_id(conn, "filament_materials", (name or "").strip() or UNKNOWN)


def _brand_id(conn: Conn, name: str | None) -> UUID | None:
    text = (name or "").strip()
    return _catalog_id(conn, "filament_brands", text) if text else None


def _rgba(colour: str | None) -> str | None:
    return None if colour is None else normalise_rgba(colour)


def _set_preset(conn: Conn, spool_id: UUID, preset: str | None) -> None:
    if preset is None:
        conn.execute(
            "DELETE FROM filament_spool_presets WHERE spool_id = %s"
            " AND printer_model = %s AND nozzle_diameter = %s",
            (spool_id, _ANY, _ANY),
        )
        return
    conn.execute(
        "INSERT INTO filament_spool_presets (spool_id, printer_model, nozzle_diameter,"
        " slicer_filament) VALUES (%s, %s, %s, %s) ON CONFLICT (spool_id, printer_model,"
        " nozzle_diameter) DO UPDATE SET slicer_filament = excluded.slicer_filament",
        (spool_id, _ANY, _ANY, preset),
    )


def _close_open(conn: Conn, spool_id: UUID, reason: str, actor: str) -> None:
    """Close the spool's open binding, if it has one, with a ``tag_unbound`` event."""
    closed = conn.execute(
        "UPDATE filament_tag_bindings SET unbound_at = now(), reason = %s"
        " WHERE spool_id = %s AND unbound_at IS NULL RETURNING tag_id",
        (reason, spool_id),
    ).fetchone()
    if closed is not None:
        ledger.record(
            conn,
            spool_id,
            "tag_unbound",
            actor=actor,
            data={"tag_id": closed["tag_id"], "reason": reason},
        )


def _lock_tags(conn: Conn, ids: Iterable[UUID | None], uid: str | None, tray: str | None) -> None:
    """Lock the tags a write may touch, by id, before the spool: every writer of bindings
    takes the tag first (`FilamentSpoolStore.bind_tag`)."""
    conn.execute(
        "SELECT id FROM filament_tags WHERE id = ANY(%s) OR tag_uid = %s OR tray_uuid = %s"
        " ORDER BY id FOR UPDATE",
        ([i for i in ids if i is not None], uid, tray),
    )


def _set_tag(
    conn: Conn, spool_id: UUID, uid: str | None, tray: str | None, *, open_: bool, actor: str
) -> None:
    """Give the spool the tag ``uid``/``tray`` names, as the native record's two keys did.
    With ``open_`` the binding is open (`NativeSpoolExistsError` when another spool holds
    the tag); an archived spool gets a closed one (reason ``archived``), which it shows.
    A tag only this spool's own matches has its keys set in place (a typo fixed); keys no
    tag has make a new tag; no keys take the tag off."""
    shown = _shown(conn, spool_id)
    own: UUID | None = None if shown is None else shown["tag_id"]
    if uid is None and tray is None:
        if shown is None:
            return
        if shown["unbound_at"] is None:
            _close_open(conn, spool_id, "unbound", actor)
        else:
            conn.execute(
                "UPDATE filament_tag_bindings SET reason = 'unbound' WHERE id = %s", (shown["id"],)
            )
        return
    is_open = shown is not None and shown["unbound_at"] is None
    if (
        shown is not None
        and (shown["tag_uid"], shown["tray_uuid"]) == (uid, tray)
        and (is_open == open_)
    ):
        return
    found = conn.execute(
        "SELECT id, tag_uid, tray_uuid FROM filament_tags WHERE tag_uid = %s OR tray_uuid = %s"
        " ORDER BY id FOR UPDATE",
        (uid, tray),
    ).fetchall()
    if len(found) > 1:
        raise NativeSpoolExistsError()
    if found:
        tag = found[0]
        tag_id: UUID = tag["id"]
        if open_:
            holder = conn.execute(
                "SELECT spool_id FROM filament_tag_bindings"
                " WHERE tag_id = %s AND unbound_at IS NULL",
                (tag_id,),
            ).fetchone()
            if holder is not None and holder["spool_id"] != spool_id:
                raise NativeSpoolExistsError()
        if tag_id == own:
            conn.execute(
                "UPDATE filament_tags SET tag_uid = %s, tray_uuid = %s WHERE id = %s",
                (uid, tray, tag_id),
            )
            if is_open == open_:
                return
        else:
            for key, value in (("tag_uid", uid), ("tray_uuid", tray)):
                if value is not None and tag[key] is not None and tag[key] != value:
                    raise NativeSpoolExistsError()
            conn.execute(
                "UPDATE filament_tags SET tag_uid = coalesce(tag_uid, %s),"
                " tray_uuid = coalesce(tray_uuid, %s) WHERE id = %s",
                (uid, tray, tag_id),
            )
    else:
        made = conn.execute(
            "INSERT INTO filament_tags (kind, tag_uid, tray_uuid) VALUES (%s, %s, %s) RETURNING id",
            ("bambu_mifare" if tray is not None else "other", uid, tray),
        ).fetchone()
        assert made is not None
        tag_id = made["id"]
    _close_open(conn, spool_id, "rebound", actor)
    conn.execute(
        "INSERT INTO filament_tag_bindings (tag_id, spool_id, unbound_at, reason)"
        " VALUES (%s, %s, CASE WHEN %s THEN NULL ELSE now() END,"
        " CASE WHEN %s THEN NULL ELSE 'archived' END)",
        (tag_id, spool_id, open_, open_),
    )
    ledger.record(conn, spool_id, "tag_bound", actor=actor, data={"tag_id": tag_id})
    if not open_:
        ledger.record(
            conn,
            spool_id,
            "tag_unbound",
            actor=actor,
            data={"tag_id": tag_id, "reason": "archived"},
        )


class NativeSpoolStore:
    """The native spool API over the filament tables (#6261, Task 8): a native spool is a
    `filament_spools` row (`filament/spools.py`), its weight is its ledger's grams left
    (`filament/ledger.py`), and a native print's uses are its ``print_started`` and
    ``print_ended`` events. Over a pool whose connections autocommit and return dict rows
    (the settings store's, `SettingsStore.pool`, which every process that builds the
    providers has). Synchronous: callers run it in a thread."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    @property
    def pool(self) -> ConnectionPool[Conn]:
        """The pool a CRUD route runs a settle and its ledger row on (`settle_uses_in`)."""
        return self._pool

    def list(self, *, include_archived: bool = False) -> list[NativeSpool]:
        where = "" if include_archived else " WHERE s.archived_at IS NULL"
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"{_READ}{where} ORDER BY s.created_at, s.id", {"unknown": UNKNOWN}
            ).fetchall()
        return [NativeSpool.model_validate(row) for row in rows]

    def any(self) -> bool:
        """Whether any spool is on the shelf, unarchived."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM filament_spools WHERE archived_at IS NULL) AS any"
            ).fetchone()
        return bool(row and row["any"])

    def get(self, spool_id: UUID) -> NativeSpool | None:
        with self._pool.connection() as conn:
            return self._read(conn, spool_id)

    def create(self, spool: NativeSpoolCreate, *, actor: str = ACTOR) -> NativeSpool:
        """A new spool with its ``created`` event (``remaining_g``, when sent, is its
        reading); an unknown material or brand joins its catalog. ``actor`` is who the
        events name (the route's caller)."""
        try:
            with self._pool.connection() as conn, conn.transaction():
                row = conn.execute(
                    "INSERT INTO filament_spools (material_id, brand_id, colour_name, rgba,"
                    " label_weight_g, note) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (
                        _material_id(conn, spool.material),
                        _brand_id(conn, spool.brand),
                        spool.name,
                        _rgba(spool.colour),
                        spool.label_weight_g,
                        spool.note,
                    ),
                ).fetchone()
                assert row is not None
                spool_id: UUID = row["id"]
                ledger.record(
                    conn,
                    spool_id,
                    "created",
                    source="person" if spool.remaining_g is not None else None,
                    absolute_g=spool.remaining_g,
                    actor=actor,
                )
                if spool.preset is not None:
                    _set_preset(conn, spool_id, spool.preset)
                _set_tag(conn, spool_id, spool.tag_uid, spool.tray_uuid, open_=True, actor=actor)
                made = self._read(conn, spool_id)
        except errors.UniqueViolation:
            raise NativeSpoolExistsError() from None
        assert made is not None
        return made

    def update(
        self, spool_id: UUID, patch: NativeSpoolPatch, *, actor: str = ACTOR
    ) -> NativeSpool | None:
        """The fields ``patch`` sends, its events naming ``actor``; ``None`` for no such
        spool. The fields kept on the
        spool's row write one ``edited`` event; ``remaining_g`` is a person's reading (a
        ``weighed`` event; ``null`` leaves the ledger's value); ``archived`` writes
        ``archived`` (the tag goes off, as the migration leaves an archived spool) or
        ``restored`` (it takes its tag back, `NativeSpoolExistsError` when another spool
        holds it)."""
        sent = patch.model_fields_set
        tags_sent = bool({"tray_uuid", "tag_uid"} & sent)
        archiving = "archived" in sent and patch.archived is not None
        try:
            with self._pool.connection() as conn, conn.transaction():
                # Tags before the spool, always: lock the tags the shown binding and the
                # sent keys name, then look again, until what is locked is what is shown.
                locked: set[UUID] | None = None
                while True:
                    shown = _shown(conn, spool_id)
                    keys = (None, None) if shown is None else (shown["tag_uid"], shown["tray_uuid"])
                    uid = patch.tag_uid if "tag_uid" in sent else keys[0]
                    tray = patch.tray_uuid if "tray_uuid" in sent else keys[1]
                    wanted = set() if shown is None else {shown["tag_id"]}
                    if not (tags_sent or archiving) or (locked is not None and wanted <= locked):
                        break
                    _lock_tags(conn, wanted, uid, tray)
                    locked = (locked or set()) | wanted
                current = conn.execute(
                    "SELECT * FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,)
                ).fetchone()
                if current is None:
                    return None
                self._edit(conn, current, patch, actor)
                was_archived = current["archived_at"] is not None
                archived = patch.archived if archiving else was_archived
                if archived != was_archived:
                    if archived:
                        _close_open(conn, spool_id, "archived", actor)
                    conn.execute(
                        "UPDATE filament_spools SET archived_at = CASE WHEN %s THEN now() END"
                        " WHERE id = %s",
                        (archived, spool_id),
                    )
                    ledger.record(
                        conn, spool_id, "archived" if archived else "restored", actor=actor
                    )
                if tags_sent or (was_archived and not archived):
                    _set_tag(conn, spool_id, uid, tray, open_=not archived, actor=actor)
                conn.execute(
                    "UPDATE filament_spools SET updated_at = now() WHERE id = %s", (spool_id,)
                )
                return self._read(conn, spool_id)
        except errors.UniqueViolation:
            raise NativeSpoolExistsError() from None

    def delete(self, spool_id: UUID) -> bool:
        """Forget a spool no print has ended on, with its open estimates, events, tag
        bindings and presets; ``False`` when there was none. A spool with
        a ``print_ended`` event is `SpoolInUseError`: it stays, archived, since its ledger
        keeps a finished print. A Bambuddy-linked one is `SpoolBambuddyLinkedError`: the
        next pull would make it again. Its cached copy
        goes at the provider's next refresh, as an archived one's does."""
        return FilamentSpoolStore(self._pool).delete(spool_id, refusing=("print_ended",))

    # -- what native prints use ------------------------------------------------------

    def record_uses(self, run_id: str, subtask_name: str, uses: Sequence[SpoolUse]) -> None:
        """What a LAN start uses, as the printer takes it: a ``print_started`` event with
        its ``print_estimate`` per slot (``ref_id`` ``run/subtask/slot``), which the ledger
        takes off at once. A slot recorded already (an attempt that starts again) keeps
        its event; a spool deleted since is left out."""
        if not uses:
            return
        with self._pool.connection() as conn, conn.transaction():
            for use in sorted(uses, key=lambda u: (u.spool_id, u.slot_id)):
                ref = _ref(run_id, subtask_name, use.slot_id)
                # One event per slot, whichever spool: what the old table's key held.
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (ref,))
                taken = conn.execute(
                    "SELECT 1 FROM filament_spool_events WHERE kind = 'print_started'"
                    " AND ref_kind = 'print_run' AND ref_id = %s",
                    (ref,),
                ).fetchone()
                spool = conn.execute(
                    "SELECT 1 FROM filament_spools WHERE id = %s", (use.spool_id,)
                ).fetchone()
                if taken is not None or spool is None:
                    continue
                ledger.record(
                    conn,
                    use.spool_id,
                    "print_started",
                    source="print_estimate",
                    delta_g=-use.grams,
                    ref_kind="print_run",
                    ref_id=ref,
                    actor=ACTOR,
                )

    def settle_uses(self, run_id: str, stages: Sequence[str]) -> set[UUID]:
        """Settle ``run_id``'s uses once its print has settled: ``stages`` is how each of
        its LAN starts ended, in the order its result lists them
        (``print_runs.result.lan``, as the follow's progress reads them). A start that
        ended ``done`` books its sliced grams as the actual (``print_ended``,
        ``print_actual``, ``data.basis`` ``sliced``); every other start books 0 (``basis``
        ``not_applied``), which drops its estimate. A ``print_ended`` is booked at its
        ``print_started``'s time, so a reading after the plate started (a BB pull between
        two plates, or before the settle) is not taken off again. Each use is settled
        once: the ledger keeps one ``print_ended`` per ``ref_id``. The spools whose weight
        changed, or that a done start took grams off (a spool whose weight is not kept
        takes none)."""
        with self._pool.connection() as conn, conn.transaction():
            return self.settle_uses_in(conn, run_id, stages)

    @staticmethod
    def settle_uses_in(conn: Conn, run_id: str, stages: Sequence[str]) -> set[UUID]:
        """`settle_uses` on ``conn``, in its caller's transaction: the CRUD route runs it
        there whole, row locks included, with its ledger row (#5298, plans D5, D6)."""
        prefix = f"{run_id}/"
        # By `filament_spool_events_print_ref`: a prefix match, the run id escaped.
        pending = conn.execute(
            "SELECT e.spool_id, e.ref_id, e.delta_g, e.at FROM filament_spool_events e"
            " WHERE e.kind = 'print_started' AND e.ref_kind = 'print_run'"
            " AND e.ref_id LIKE %s AND NOT EXISTS (SELECT 1 FROM filament_spool_events d"
            " WHERE d.spool_id = e.spool_id AND d.kind = 'print_ended'"
            " AND d.ref_kind = 'print_run' AND d.ref_id = e.ref_id)",
            (_like_prefix(prefix),),
        ).fetchall()
        if not pending:
            return set()
        spools = sorted({use["spool_id"] for use in pending})
        before = {
            row["id"]: row["grams_left"]
            for row in conn.execute(
                "SELECT id, grams_left FROM filament_spools WHERE id = ANY(%s)"
                " ORDER BY id FOR UPDATE",
                (spools,),
            ).fetchall()
        }
        row = conn.execute(
            "SELECT result->'lan' AS lan FROM print_runs WHERE id = %s", (run_id,)
        ).fetchone()
        starts = row["lan"] if row is not None and isinstance(row["lan"], list) else []
        done = {
            start.get("subtask_name")
            for start, stage in zip(starts, stages, strict=False)
            if isinstance(start, dict) and stage == "done"
        }
        changed: set[UUID] = set()
        for use in pending:
            subtask_name = use["ref_id"][len(prefix) :].rsplit("/", 1)[0]
            applied = subtask_name in done
            grams = -(use["delta_g"] or 0.0)
            ended = ledger.record(
                conn,
                use["spool_id"],
                "print_ended",
                source="print_actual",
                delta_g=-grams if applied else 0.0,
                ref_kind="print_run",
                ref_id=use["ref_id"],
                actor=ACTOR,
                # At the plate's start: no end time is kept per plate (`LanStarted` has
                # none), and a reading taken after the start is read as holding the
                # plate, as BB's is once BB deducts it (`ledger.py`).
                at=use["at"],
                data={"basis": "sliced" if applied else "not_applied"},
            )
            if ended is not None and applied and before.get(use["spool_id"]) is not None:
                changed.add(use["spool_id"])
        after = conn.execute(
            "SELECT id, grams_left FROM filament_spools WHERE id = ANY(%s)", (spools,)
        ).fetchall()
        changed |= {r["id"] for r in after if r["grams_left"] != before.get(r["id"])}
        return changed

    # -- internals --------------------------------------------------------------------

    @staticmethod
    def _read(conn: Conn, spool_id: UUID) -> NativeSpool | None:
        row = conn.execute(
            f"{_READ} WHERE s.id = %(id)s", {"unknown": UNKNOWN, "id": spool_id}
        ).fetchone()
        return None if row is None else NativeSpool.model_validate(row)

    @staticmethod
    def _edit(conn: Conn, current: Mapping[str, Any], patch: NativeSpoolPatch, actor: str) -> None:
        """The sent fields kept on the spool's row and its ``*`` preset, as one ``edited``
        event of ``[before, after]`` per change; then ``remaining_g`` as a reading."""
        sent = patch.model_fields_set
        spool_id: UUID = current["id"]
        after: dict[str, Any] = {
            column: getattr(patch, field) for field, column in _COLUMN_OF.items() if field in sent
        }
        if "colour" in sent:
            after["rgba"] = _rgba(patch.colour)
        if "brand" in sent:
            after["brand_id"] = _brand_id(conn, patch.brand)
        if "material" in sent:
            # A spool with a type takes the type's material when its own is cleared.
            typed = current["type_id"] is not None and patch.material is None
            after["material_id"] = None if typed else _material_id(conn, patch.material)
        changes: dict[str, list[Any]] = {
            column: [current[column], value]
            for column, value in after.items()
            if current[column] != value
        }
        if changes:
            columns = list(changes)
            conn.execute(
                f"UPDATE filament_spools SET {', '.join(f'{c} = %s' for c in columns)}"
                " WHERE id = %s",
                [*(after[c] for c in columns), spool_id],
            )
        if "preset" in sent:
            was = conn.execute(
                "SELECT slicer_filament FROM filament_spool_presets WHERE spool_id = %s"
                " AND printer_model = %s AND nozzle_diameter = %s",
                (spool_id, _ANY, _ANY),
            ).fetchone()
            old = None if was is None else was["slicer_filament"]
            if old != patch.preset:
                _set_preset(conn, spool_id, patch.preset)
                changes["preset"] = [old, patch.preset]
        if changes:
            ledger.record(conn, spool_id, "edited", actor=actor, data={"changes": changes})
        if "remaining_g" in sent and patch.remaining_g is not None:
            ledger.record(
                conn,
                spool_id,
                "weighed",
                source="person",
                absolute_g=patch.remaining_g,
                actor=actor,
            )
