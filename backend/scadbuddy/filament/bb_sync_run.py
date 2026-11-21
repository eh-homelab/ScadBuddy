"""One ScadBuddy-to-Bambuddy sync pass, on the print worker (#6265 Task 7, plan
`docs/superpowers/plans/2026-11-18-filament-bb-sync.md` Decision 11).

The pass reads Bambuddy once into a `BbSnapshot`. Then it runs plan rounds: it asks SB
(`FilamentSyncApi`) for the next writes, makes them in order through `BambuddyClient`, and
sends what Bambuddy answered back to SB with the round's ``pass_id``. SB first, Bambuddy
second: only `FilamentSyncApi.results` writes SB, and only for the writes Bambuddy
answered. A write that fails becomes a not-ok result and the others go on. The pass ends
when a plan has no writes, after ``max_rounds`` rounds, or after `WRITE_CAP` calls.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel, ValidationError
from temporalio import activity

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.models import SpoolCatalogEntry
from scadbuddy.core.problems import ApiError
from scadbuddy.filament.bambuddy_pull import parse_rows
from scadbuddy.filament.bb_sync_models import (
    DEFAULT_INTERVAL_S,
    SYNC_PASS_ACTIVITY,
    BbSnapshot,
    BbWrite,
    BbWriteResult,
    FilamentSyncApi,
    SyncPassInput,
    SyncPassResult,
    pushes_anything,
)
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.filament.rfid_push import error_label
from scadbuddy.library.settings_store import StoredSettings

logger = logging.getLogger(__name__)

#: At most this many Bambuddy calls (writes and probes) in one pass (Decision 11).
WRITE_CAP = 200


async def run_pass(
    api: FilamentSyncApi,
    load: Callable[[], Awaitable[StoredSettings]],
    *,
    instance: str,
    full: bool,
    heartbeat: Callable[[], None],
    max_rounds: int = 4,
    interval_s: float = DEFAULT_INTERVAL_S,
) -> SyncPassResult:
    """One pass, as the module says. With no Bambuddy URL it is ``skipped``, and so is a
    pass with no data type that pushes (``nothing_pushes``, R-T8-2: no Bambuddy read). When the URL
    is another instance's, it calls nothing, records nothing and answers
    ``instance_changed``: the workflow of the configured instance records its own passes.
    So does a pass whose plan answers ``instance_changed`` (SB's links belong to another
    Bambuddy, R-T7-7). Else it records the pass (`FilamentSyncApi.record_pass`) whether
    it worked or not; a spool listing it could not read, or that is not JSON, is a failed
    pass (R-T7-8).

    ``written`` counts the writes Bambuddy answered ok (a probe writes nothing),
    ``failed`` the writes it refused (a probe's 404 is an answer: the row is gone). The
    other counts come from the plans: ``deferred`` and ``more`` from the last round's,
    ``conflicts`` and ``unmapped_k`` the largest of the rounds', since every round plans
    from the one snapshot (R-T4-33). ``interval_s`` is the plans' (the stored setting),
    else the given one."""
    started_at = datetime.now(UTC)
    settings = await load()
    empty = SyncPassResult(
        written=0,
        failed=0,
        deferred=0,
        more=False,
        instance_changed=False,
        interval_s=interval_s,
        started_at=started_at,
    )
    if not settings.bambuddy_url:
        result = empty.model_copy(update={"skipped": "no_bambuddy"})
        await api.record_pass(result)
        return result
    if instance_for(settings.bambuddy_url) != instance:
        return empty.model_copy(update={"instance_changed": True})
    if not pushes_anything(await api.directions()):
        # R-T8-2: no Bambuddy read when nothing pushes; the workflow ends on it.
        result = empty.model_copy(update={"skipped": "nothing_pushes"})
        await api.record_pass(result)
        return result
    async with client_for(settings) as client:
        try:
            snap = await _read(client, instance, full, heartbeat)
        except ApiError as exc:
            result = empty.model_copy(update={"error": error_label(exc)})
            await api.record_pass(result)
            return result
        result = await _rounds(api, client, snap, heartbeat, max_rounds, empty)
    if not result.instance_changed:
        await api.record_pass(result)
    return result


async def _read(
    client: BambuddyClient, instance: str, full: bool, heartbeat: Callable[[], None]
) -> BbSnapshot:
    """Bambuddy read once. The spool listing must be read (its `ApiError` ends the pass);
    any other listing that fails is ``None``, never empty. Each listing heartbeats (review
    M4): a slow Bambuddy must not time the pass out before its first write."""
    read_at = datetime.now(UTC)
    rows = await client.spool_rows(include_archived=True)
    heartbeat()
    _, unparsed = parse_rows(rows)
    return BbSnapshot(
        instance=instance,
        full=full,
        spools=[cast("dict[str, Any]", row) for row in rows if isinstance(row, dict)],
        spools_complete=not unparsed,
        suppliers=await _maybe(client.suppliers(), "suppliers", heartbeat),
        locations=await _maybe(client.locations(), "locations", heartbeat),
        catalog=await _maybe(client.spool_catalog(), "spool catalog", heartbeat),
        filaments=await _maybe(client.filament_catalog(), "filament catalog", heartbeat),
        read_at=read_at,
    )


async def _maybe[T](
    read: Awaitable[list[T]], name: str, heartbeat: Callable[[], None]
) -> list[T] | None:
    try:
        return await read
    except (ApiError, ValidationError) as exc:
        label = error_label(exc) if isinstance(exc, ApiError) else "invalid"
        logger.warning(
            "the sync could not read a Bambuddy listing; it is left unread",
            extra={"listing": name, "error": label},
        )
        return None
    finally:
        heartbeat()


async def _rounds(
    api: FilamentSyncApi,
    client: BambuddyClient,
    snap: BbSnapshot,
    heartbeat: Callable[[], None],
    max_rounds: int,
    empty: SyncPassResult,
) -> SyncPassResult:
    calls = written = failed = conflicts = unmapped_k = deferred = 0
    interval_s = empty.interval_s
    instance_changed = False
    for _ in range(max_rounds):
        if calls >= WRITE_CAP:
            break
        plan = await api.plan(snap, WRITE_CAP - calls)
        heartbeat()
        interval_s = plan.interval_s
        deferred = plan.deferred
        conflicts = max(conflicts, plan.conflicts)
        unmapped_k = max(unmapped_k, plan.unmapped_k)
        if plan.instance_changed:
            instance_changed = True
            break
        if not plan.writes:
            break
        results: list[BbWriteResult] = []
        for write in plan.writes:
            result = await _call(client, write, snap.catalog)
            results.append(result)
            if result.ok:
                if write.op != "probe":
                    written += 1
            elif not (write.op == "probe" and result.status == 404):
                failed += 1
            heartbeat()
        calls += len(plan.writes)
        await api.results(snap.instance, plan.writes, results, plan.pass_id, plan.unconfirmed)
        heartbeat()
    return SyncPassResult(
        written=written,
        failed=failed,
        deferred=deferred,
        more=deferred > 0,
        conflicts=conflicts,
        unmapped_k=unmapped_k,
        instance_changed=instance_changed,
        interval_s=interval_s,
        started_at=empty.started_at,
    )


async def _call(
    client: BambuddyClient, write: BbWrite, catalog: list[SpoolCatalogEntry] | None
) -> BbWriteResult:
    """Make one write; Bambuddy's answer, or why it failed (`error_label`, never the
    message)."""
    try:
        answer = await _send(client, write, catalog)
    except _NotListedError:
        return BbWriteResult(key=write.key, ok=False, error="not_found", status=404)
    except _ListingFailedError as exc:
        # No status: the listing's own answer is not the row's, so even its 404 keeps
        # the link (R-6688-3).
        return BbWriteResult(key=write.key, ok=False, error=exc.label)
    except ApiError as exc:
        code = exc.extensions.get("bambuddy_status")
        return BbWriteResult(
            key=write.key,
            ok=False,
            error=error_label(exc),
            status=code if isinstance(code, int) else None,
        )
    except Exception:
        # The write may have reached Bambuddy (an answer that is not its row, say): it
        # is recorded as refused, and the create intent finds a lost row again.
        logger.warning("a sync write to Bambuddy failed", extra={"key": write.key}, exc_info=True)
        return BbWriteResult(key=write.key, ok=False, error="internal")
    bb_id = answer.get("id") if isinstance(answer, dict) else None
    return BbWriteResult(
        key=write.key,
        ok=True,
        bb_id=bb_id if isinstance(bb_id, int) else write.bb_id,
        answer=answer,
    )


def _row(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


async def _send(
    client: BambuddyClient, write: BbWrite, catalog: list[SpoolCatalogEntry] | None
) -> dict[str, Any] | None:
    """The client call for ``write``, and what Bambuddy answered as `BbWriteResult.answer`:
    a row, a list ``PUT``'s list under its field name (R-T5-2), or ``None`` for the
    preset list, which no spool field holds."""
    op, bb_id = write.op, write.bb_id
    body = write.body
    if op == "create":
        return _row(await _create(client, write))
    if bb_id is None:
        raise ValueError(f"{write.key} names no Bambuddy row")
    if write.kind == "spool":
        match op:
            case "update":
                return _row(await client.update_spool(bb_id, cast("dict[str, Any]", body)))
            case "archive":
                return _row(await client.archive_spool(bb_id))
            case "restore":
                return _row(await client.restore_spool(bb_id))
            case "probe":
                return _row(await client.spool_row(bb_id))
            case "k_profiles":
                rows = cast("list[dict[str, Any]]", body)
                return {"k_profiles": await client.put_spool_k_profiles(bb_id, rows)}
            case "suppliers":
                rows = cast("list[dict[str, Any]]", body)
                return {"suppliers": await client.put_spool_suppliers(bb_id, rows)}
            case "presets":
                await client.put_spool_presets(bb_id, cast("list[dict[str, Any]]", body))
                return None
    if op == "probe":
        return await _probe(client, write.kind, bb_id)
    if op != "update":
        raise ValueError(f"{write.key}: no such write")
    fields = cast("dict[str, Any]", body)
    match write.kind:
        case "supplier":
            return _row(await client.update_supplier(bb_id, fields))
        case "location":
            return _row(await client.update_location(bb_id, fields))
        case "filament":
            return _row(await client.update_filament(bb_id, fields))
        case _:
            # ``PUT …/catalog/{id}`` takes the name and the weight both: a field the plan
            # left out keeps Bambuddy's value as this pass read it.
            now = next((e for e in catalog or [] if e.id == bb_id), None)
            name = fields.get("name", now.name if now else None)
            weight = fields.get("weight", now.weight if now else None)
            if name is None or weight is None:
                raise ValueError(f"{write.key}: catalog entry {bb_id} was not read")
            return _row(await client.update_catalog_entry(bb_id, name, float(weight)))


class _NotListedError(Exception):
    """A probe's listing, read whole, does not hold the row: Bambuddy deleted it."""


class _ListingFailedError(Exception):
    """A probe could not read its listing whole; ``label`` is what the error row says."""

    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.label = label


#: The listing a probe reads again: for each kind Bambuddy has no ``GET`` of one row for
#: (Bambuddy v1.3.1: ``/inventory/suppliers/{id}`` and ``/inventory/locations/{id}`` take
#: ``PATCH`` and ``DELETE`` only, ``/inventory/catalog/{id}`` ``PUT`` and ``DELETE``), and
#: for a filament entry whose ``GET`` by id answered 404 (R-6688-8).
_LISTINGS: dict[str, Callable[[BambuddyClient], Awaitable[Sequence[BaseModel]]]] = {
    "supplier": lambda client: client.suppliers(),
    "location": lambda client: client.locations(),
    "insert_type": lambda client: client.spool_catalog(),
    "filament": lambda client: client.filament_catalog(),
}


async def _probe(client: BambuddyClient, kind: str, bb_id: int) -> dict[str, Any]:
    """#6688 (R-6688-2, R-6688-3): whether Bambuddy still holds a linked row, by ``GET``
    only. A filament entry is read by its id first. A 404 there may be a Bambuddy build
    without the route, which says nothing of the entry, so it is confirmed by the listing
    (R-6688-8), as every other kind's probe is: the row there is the answer, and a listing
    read whole without it is `_NotListedError`; a listing that fails or does not parse is
    `_ListingFailedError`, which is never a 404."""
    if kind == "filament":
        try:
            return _row(await client.filament(bb_id))
        except ApiError as exc:
            if exc.extensions.get("bambuddy_status") != 404:
                raise
    try:
        rows = await _LISTINGS[kind](client)
    except ApiError as exc:
        raise _ListingFailedError(error_label(exc)) from None
    except ValidationError:
        raise _ListingFailedError("invalid") from None
    for row in rows:
        if getattr(row, "id", None) == bb_id:
            return _row(row)
    raise _NotListedError


async def _create(client: BambuddyClient, write: BbWrite) -> BaseModel:
    body = cast("dict[str, Any]", write.body)
    match write.kind:
        case "spool":
            return await client.create_spool(body)
        case "supplier":
            return await client.create_supplier(body)
        case "location":
            return await client.create_location(body)
        case "insert_type":
            return await client.create_catalog_entry(body["name"], float(body["weight"]))
        case "filament":
            return await client.create_filament(body)
    raise ValueError(f"{write.key}: no such create")


class FilamentSyncActivities:
    """The ``filament_bb_sync_pass`` activity. ``api`` is `LocalFilamentSync` in the API's
    process and `RemoteFilamentSync` on the print worker; ``load`` reads the stored
    settings (the Bambuddy URL and key)."""

    def __init__(self, api: FilamentSyncApi, load: Callable[[], Awaitable[StoredSettings]]) -> None:
        self.api = api
        self.load = load

    @activity.defn(name=SYNC_PASS_ACTIVITY)
    async def sync_pass(self, start: SyncPassInput) -> SyncPassResult:
        """One pass (`run_pass`), its ``more`` as the pass left it: the workflow runs the
        next pass at once, and bounds how many it runs back to back (R-T4-21, R-T7-1)."""
        return await self.one_pass(start, activity.heartbeat)

    async def one_pass(self, start: SyncPassInput, heartbeat: Callable[[], None]) -> SyncPassResult:
        return await run_pass(
            self.api,
            self.load,
            instance=start.instance,
            full=start.full,
            heartbeat=heartbeat,
            interval_s=start.interval_s,
        )


def settings_loader(load: Callable[[], StoredSettings]) -> Callable[[], Awaitable[StoredSettings]]:
    """``load`` (blocking: a database read or an HTTP call) off the event loop."""

    async def read() -> StoredSettings:
        return await asyncio.to_thread(load)

    return read
