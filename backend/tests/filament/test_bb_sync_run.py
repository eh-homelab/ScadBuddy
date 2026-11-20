"""#6265 Task 7: one sync pass on the print worker. Bambuddy is respx (every sent body is
checked against the recorded schema in ``recordings/openapi/sync-routes.json``), and SB is
a fake `FilamentSyncApi`; no call reaches a live Bambuddy."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx

from scadbuddy.bambuddy.errors import BAD_BODY_PROBLEM
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament.bb_sync_models import (
    BbSnapshot,
    BbWrite,
    BbWriteResult,
    Kind,
    Op,
    ResultsReport,
    SyncPassInput,
    SyncPassResult,
    SyncPlan,
)
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.filament.bb_sync_run import FilamentSyncActivities, run_pass
from scadbuddy.library.settings_store import StoredSettings
from tests.bambuddy.conftest import BASE_URL, assert_recorded_body, recording, shaped

API = f"{BASE_URL}/api/v1"
SYNC = "sync-routes.json"
INSTANCE = instance_for(BASE_URL)
SETTINGS = StoredSettings(bambuddy_url=BASE_URL, bambuddy_api_key="s3cret")

#: A plan round: the plan itself, or a function of the limit and the results sent so far.
Round = SyncPlan | Callable[[int, list[list[BbWriteResult]]], SyncPlan]


class FakeSync:
    """A `FilamentSyncApi` that answers ``rounds`` in turn (then an empty plan) and keeps
    every call."""

    def __init__(self, rounds: Sequence[Round] = ()) -> None:
        self.rounds = list(rounds)
        self.plans: list[tuple[BbSnapshot, int]] = []
        self.planned: list[SyncPlan] = []
        self.sent: list[tuple[str, list[BbWrite], list[BbWriteResult], str | None]] = []
        self.recorded: list[SyncPassResult] = []

    async def plan(self, snap: BbSnapshot, limit: int) -> SyncPlan:
        self.plans.append((snap, limit))
        if self.rounds:
            step = self.rounds.pop(0)
            done = [results for _, _, results, _ in self.sent]
            planned = step if isinstance(step, SyncPlan) else step(limit, done)
        else:
            planned = _plan([])
        planned = planned.model_copy(update={"pass_id": uuid4().hex})
        self.planned.append(planned)
        return planned

    async def results(
        self,
        instance: str,
        writes: Sequence[BbWrite],
        results: Sequence[BbWriteResult],
        pass_id: str | None,
    ) -> ResultsReport:
        self.sent.append((instance, list(writes), list(results), pass_id))
        return ResultsReport()

    async def record_pass(self, result: SyncPassResult) -> None:
        self.recorded.append(result)


def _plan(writes: list[BbWrite], **over: Any) -> SyncPlan:
    return SyncPlan(
        writes=writes, deferred=0, directions=FilamentSync(), interval_s=300.0
    ).model_copy(update=over)


def _write(
    op: Op, bb_id: int | None, body: dict[str, Any] | list[Any], *, kind: Kind = "spool"
) -> BbWrite:
    sb_id = uuid4()
    return BbWrite(
        key=f"{kind}:{sb_id}:{op}", kind=kind, sb_id=sb_id, op=op, bb_id=bb_id, body=body, hashes={}
    )


def _spool_row(bb_id: int, **values: object) -> dict[str, object]:
    return shaped("SpoolResponse", id=bb_id, material="PLA", **values)


def _listings(*, spools: list[Any] | None = None) -> dict[str, respx.Route]:
    """Bambuddy's five listings, as one pass reads them."""
    rows = recording("inventory-spools.json") if spools is None else spools
    return {
        "spools": respx.get(f"{API}/inventory/spools").mock(
            return_value=httpx.Response(200, json=rows)
        ),
        "suppliers": respx.get(f"{API}/inventory/suppliers").mock(
            return_value=httpx.Response(200, json=[])
        ),
        "locations": respx.get(f"{API}/inventory/locations").mock(
            return_value=httpx.Response(200, json=[])
        ),
        "catalog": respx.get(f"{API}/inventory/catalog").mock(
            return_value=httpx.Response(200, json=recording("inventory-catalog.json"))
        ),
        "filaments": respx.get(f"{API}/filament-catalog/").mock(
            return_value=httpx.Response(200, json=[])
        ),
    }


def _spool_updates() -> respx.Route:
    """Every ``PATCH …/inventory/spools/{id}``, each answered with its spool's row."""

    def answer(request: httpx.Request) -> httpx.Response:
        assert_recorded_body(SYNC, "SpoolUpdate", json.loads(request.content))
        bb_id = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=_spool_row(bb_id))

    return respx.patch(url__regex=rf"{re.escape(API)}/inventory/spools/\d+$").mock(
        side_effect=answer
    )


async def _load() -> StoredSettings:
    return SETTINGS


def _beats() -> tuple[list[None], Callable[[], None]]:
    beats: list[None] = []
    return beats, lambda: beats.append(None)


async def _run(sync: FakeSync, **over: Any) -> SyncPassResult:
    args: dict[str, Any] = {"instance": INSTANCE, "full": True, "heartbeat": lambda: None}
    load = over.pop("load", _load)
    return await run_pass(sync, load, **(args | over))


@respx.mock
async def test_no_bambuddy_skips() -> None:
    sync = FakeSync()

    async def bare() -> StoredSettings:
        return StoredSettings()

    result = await _run(sync, load=bare)

    assert result.skipped == "no_bambuddy" and result.error is None
    assert sync.plans == [] and sync.recorded == [result]
    assert not respx.calls


@respx.mock
async def test_another_instance_calls_nothing() -> None:
    sync = FakeSync()
    _listings()

    result = await _run(sync, instance=instance_for("https://another.test"))

    assert result.instance_changed is True
    assert not respx.calls
    assert sync.plans == [] and sync.recorded == []


def test_the_instance_ignores_case_and_a_trailing_slash() -> None:
    assert instance_for("HTTPS://Bambuddy.test/") == INSTANCE
    assert len(INSTANCE) == 12


@respx.mock
async def test_a_failed_write_does_not_stop_the_others() -> None:
    _listings()
    writes = [_write("update", n, {"note": f"spool {n}"}) for n in (1, 2, 3)]
    sync = FakeSync([_plan(writes)])
    routes = [
        respx.patch(f"{API}/inventory/spools/1").mock(
            return_value=httpx.Response(200, json=_spool_row(1, note="spool 1"))
        ),
        respx.patch(f"{API}/inventory/spools/2").mock(return_value=httpx.Response(500)),
        respx.patch(f"{API}/inventory/spools/3").mock(
            return_value=httpx.Response(200, json=_spool_row(3, note="spool 3"))
        ),
    ]

    result = await _run(sync)

    for route in routes:
        assert_recorded_body(SYNC, "SpoolUpdate", json.loads(route.calls.last.request.content))
    [(instance, sent, results, _)] = sync.sent
    assert instance == INSTANCE and sent == writes
    assert [(r.key, r.ok) for r in results] == [(w.key, w.key != writes[1].key) for w in writes]
    assert results[1].error is not None and results[1].error.endswith(":500")
    assert results[1].status == 500 and results[1].answer is None
    assert results[0].answer is not None and results[0].answer["note"] == "spool 1"
    assert results[2].bb_id == 3
    assert (result.written, result.failed, result.error) == (2, 1, None)
    assert sync.recorded == [result]


@respx.mock
async def test_an_answer_that_is_not_a_row_is_an_internal_failure() -> None:
    _listings()
    writes = [_write("update", 1, {"note": "x"}), _write("update", 2, {"note": "y"})]
    respx.patch(f"{API}/inventory/spools/1").mock(return_value=httpx.Response(200, json=[]))
    respx.patch(f"{API}/inventory/spools/2").mock(
        return_value=httpx.Response(200, json=_spool_row(2))
    )
    sync = FakeSync([_plan(writes)])

    result = await _run(sync)

    [(_, _, [odd, fine], _)] = sync.sent
    assert (odd.ok, odd.error, odd.status) == (False, "internal", None)
    assert fine.ok
    assert (result.written, result.failed) == (1, 1)


@respx.mock
async def test_a_second_round_uses_the_new_location_id() -> None:
    _listings()
    location = _write("create", None, {"name": "Shelf A", "identifier": "A1"}, kind="location")
    spool = _write("update", 4, {})

    def second(limit: int, done: list[list[BbWriteResult]]) -> SyncPlan:
        # The plan route reads the link `results` stored for round 1's create.
        [[made]] = done
        assert made.ok and made.answer is not None
        return _plan([spool.model_copy(update={"body": {"location_id": made.answer["id"]}})])

    sync = FakeSync([_plan([location]), second])
    create = respx.post(f"{API}/inventory/locations").mock(
        return_value=httpx.Response(
            200, json=shaped("LocationResponse", id=9, name="Shelf A", identifier="A1")
        )
    )
    update = respx.patch(f"{API}/inventory/spools/4").mock(
        return_value=httpx.Response(200, json=_spool_row(4, location_id=9))
    )

    result = await _run(sync)

    assert_recorded_body(SYNC, "LocationCreate", json.loads(create.calls.last.request.content))
    sent = json.loads(update.calls.last.request.content)
    assert_recorded_body(SYNC, "SpoolUpdate", sent)
    assert sent == {"location_id": 9}
    assert [len(plan.writes) for plan in sync.planned] == [1, 1, 0]
    assert result.written == 2


@respx.mock
async def test_a_failed_supplier_read_is_none_not_empty() -> None:
    routes = _listings()
    routes["suppliers"].mock(return_value=httpx.Response(500))
    sync = FakeSync()

    result = await _run(sync)

    [(snap, limit)] = sync.plans
    assert snap.suppliers is None
    assert snap.locations == [] and snap.filaments == []
    assert snap.catalog is not None and len(snap.catalog) == 3
    assert snap.spools_complete is True and snap.instance == INSTANCE and snap.full is True
    assert limit == 200
    assert result.error is None


@pytest.mark.parametrize("listing", ["locations", "catalog", "filaments"])
@respx.mock
async def test_any_other_failed_read_is_none(listing: str) -> None:
    routes = _listings()
    routes[listing].mock(return_value=httpx.Response(503))
    sync = FakeSync()

    result = await _run(sync)

    [(snap, _)] = sync.plans
    read = {"locations": snap.locations, "catalog": snap.catalog, "filaments": snap.filaments}
    assert read[listing] is None
    assert all(value is not None for name, value in read.items() if name != listing)
    assert snap.suppliers == [] and result.error is None


@respx.mock
async def test_a_listing_of_rows_that_are_not_its_kind_is_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    routes = _listings()
    routes["locations"].mock(return_value=httpx.Response(200, json=[{"id": "nine"}]))
    sync = FakeSync()

    with caplog.at_level(logging.WARNING, logger="scadbuddy.filament.bb_sync_run"):
        await _run(sync)

    [(snap, _)] = sync.plans
    assert snap.locations is None
    [record] = [r for r in caplog.records if r.getMessage().startswith("the sync could not")]
    assert record.__dict__["error"] == "invalid" and record.__dict__["listing"] == "locations"


@respx.mock
async def test_the_write_cap_sets_more() -> None:
    _listings()
    _spool_updates()
    sync = FakeSync([_plan([_write("update", n, {"note": "x"}) for n in range(200)], deferred=5)])

    result = await _run(sync)

    assert len(sync.plans) == 1
    assert result.written == 200 and result.deferred == 5 and result.more is True


@respx.mock
async def test_each_round_asks_for_what_is_left_of_the_cap() -> None:
    _listings()
    _spool_updates()
    # A probe counts toward the cap like a write (R-T7-9).
    respx.get(url__regex=rf"{re.escape(API)}/inventory/spools/\d+$").mock(
        side_effect=lambda request: httpx.Response(
            200, json=_spool_row(int(request.url.path.rsplit("/", 1)[1]))
        )
    )
    first = [_write("update", n, {"note": "x"}) for n in range(140)]
    first += [_write("probe", 1000 + n, {}) for n in range(10)]
    sync = FakeSync([_plan(first), _plan([_write("update", n, {"note": "y"}) for n in range(50)])])

    result = await _run(sync)

    assert [limit for _, limit in sync.plans] == [200, 50]
    assert result.written == 190 and result.more is False


@respx.mock
async def test_a_round_cap_ends_the_pass() -> None:
    _listings()
    _spool_updates()
    sync = FakeSync([_plan([_write("update", n, {"note": "x"})], deferred=1) for n in range(6)])

    result = await _run(sync, max_rounds=4)

    assert len(sync.plans) == 4 and len(sync.sent) == 4
    assert result.written == 4 and result.more is True


@respx.mock
async def test_a_failed_spool_read_records_an_error() -> None:
    routes = _listings()
    routes["spools"].mock(return_value=httpx.Response(500))
    sync = FakeSync()

    result = await _run(sync)

    assert sync.plans == []
    assert result.error is not None and result.error.endswith(":500")
    assert sync.recorded == [result]


@respx.mock
async def test_a_spool_listing_that_is_not_json_fails_the_pass() -> None:
    routes = _listings()
    routes["spools"].mock(return_value=httpx.Response(200, text="<html>proxy</html>"))
    sync = FakeSync()

    result = await _run(sync)

    assert sync.plans == []
    assert result.error == BAD_BODY_PROBLEM
    assert sync.recorded == [result]


@respx.mock
async def test_an_unparsed_row_marks_the_listing_partial() -> None:
    _listings(spools=[_spool_row(1), {"id": 7, "label_weight": "heavy"}])
    sync = FakeSync()

    await _run(sync)

    [(snap, _)] = sync.plans
    assert snap.spools_complete is False
    assert [row["id"] for row in snap.spools] == [1, 7]


@respx.mock
async def test_a_probe_404_comes_back_as_not_found() -> None:
    _listings()
    probe = _write("probe", 5, {})
    found = _write("probe", 6, {})
    sync = FakeSync([_plan([probe, found])])
    respx.get(f"{API}/inventory/spools/5").mock(return_value=httpx.Response(404))
    respx.get(f"{API}/inventory/spools/6").mock(
        return_value=httpx.Response(200, json=_spool_row(6, note="still here"))
    )

    result = await _run(sync)

    [(_, _, [gone, kept], _)] = sync.sent
    assert gone.key == probe.key and gone.ok is False and gone.status == 404
    assert gone.error is not None and gone.error.endswith(":404")
    assert kept.ok is True and kept.answer is not None and kept.answer["note"] == "still here"
    # A probe writes nothing, and its 404 is an answer, not a refusal.
    assert (result.written, result.failed) == (0, 0)


@respx.mock
async def test_every_write_heartbeats() -> None:
    _listings()
    _spool_updates()
    sync = FakeSync([_plan([_write("update", n, {"note": "x"}) for n in range(3)])])
    beats, beat = _beats()

    await _run(sync, heartbeat=beat)

    assert len(beats) == 3


@respx.mock
async def test_conflicts_are_counted_once_per_pass() -> None:
    _listings()
    _spool_updates()
    sync = FakeSync(
        [
            _plan([_write("update", 1, {"note": "x"})], conflicts=1, unmapped_k=2),
            _plan([_write("update", 2, {"note": "y"})], conflicts=1, unmapped_k=2),
        ]
    )

    result = await _run(sync)

    assert result.conflicts == 1 and result.unmapped_k == 2


@respx.mock
async def test_each_round_sends_its_plans_pass_id() -> None:
    _listings()
    _spool_updates()
    sync = FakeSync(
        [_plan([_write("update", 1, {"note": "x"})]), _plan([_write("update", 2, {"note": "y"})])]
    )

    await _run(sync)

    assert [pass_id for *_, pass_id in sync.sent] == [plan.pass_id for plan in sync.planned[:2]]
    assert len({plan.pass_id for plan in sync.planned}) == 3


@respx.mock
async def test_a_list_put_answer_names_its_field() -> None:
    _listings()
    k_rows = [{"printer_id": 1, "extruder": 0, "nozzle_diameter": "0.4", "k_value": 0.02}]
    links = [{"supplier_id": 4, "quoted_price_per_kg": 18.5, "is_purchase_source": True}]
    presets = [{"printer_model": "H2C", "nozzle_diameter": "0.4", "slicer_filament": "GFA00"}]
    writes = [
        _write("k_profiles", 3, k_rows),
        _write("suppliers", 3, links),
        _write("presets", 3, presets),
    ]
    k_answer = [shaped("SpoolKProfileResponse", id=11, spool_id=3, **k_rows[0])]
    link_answer = [shaped("SpoolSupplierResponse", id=1, supplier_name="Bambu", **links[0])]
    put_k = respx.put(f"{API}/inventory/spools/3/k-profiles").mock(
        return_value=httpx.Response(200, json=k_answer)
    )
    put_links = respx.put(f"{API}/inventory/spools/3/suppliers").mock(
        return_value=httpx.Response(200, json=link_answer)
    )
    put_presets = respx.put(f"{API}/inventory/spools/3/filament-presets").mock(
        return_value=httpx.Response(200, json=[])
    )
    sync = FakeSync([_plan(writes)])

    result = await _run(sync)

    assert_recorded_body(
        SYNC, "SpoolKProfileBase", json.loads(put_k.calls.last.request.content), many=True
    )
    assert_recorded_body(
        SYNC, "SpoolSupplierLinkInput", json.loads(put_links.calls.last.request.content), many=True
    )
    assert_recorded_body(
        SYNC,
        "SpoolFilamentPresetBase",
        json.loads(put_presets.calls.last.request.content),
        many=True,
    )
    [(_, _, [k, supplied, preset], _)] = sync.sent
    assert k.ok and k.answer == {"k_profiles": k_answer} and k.bb_id == 3
    assert supplied.ok and supplied.answer == {"suppliers": link_answer}
    assert preset.ok and preset.answer is None
    assert result.written == 3


@respx.mock
async def test_each_row_kind_goes_to_its_route() -> None:
    _listings()
    writes = [
        _write("update", 2, {"website": "https://shop.test"}, kind="supplier"),
        _write("update", 3, {"name": "Shelf B"}, kind="location"),
        # The catalog PUT takes both fields: the weight comes from the listing (id 5: 250 g).
        _write("update", 5, {"name": "Reusable"}, kind="insert_type"),
        _write("create", None, {"name": "Cardboard", "weight": 212.4}, kind="insert_type"),
        _write("update", 8, {"color_hex": "#FF0000"}, kind="filament"),
    ]
    routes = {
        "SupplierUpdate": respx.patch(f"{API}/inventory/suppliers/2").mock(
            return_value=httpx.Response(200, json=shaped("SupplierResponse", id=2, name="Shop"))
        ),
        "LocationUpdate": respx.patch(f"{API}/inventory/locations/3").mock(
            return_value=httpx.Response(200, json=shaped("LocationResponse", id=3, name="Shelf B"))
        ),
        "CatalogEntryUpdate": respx.put(f"{API}/inventory/catalog/5").mock(
            return_value=httpx.Response(
                200, json=shaped("CatalogEntryResponse", id=5, name="Reusable", weight=250)
            )
        ),
        "CatalogEntryCreate": respx.post(f"{API}/inventory/catalog").mock(
            return_value=httpx.Response(
                200, json=shaped("CatalogEntryResponse", id=12, name="Cardboard", weight=212)
            )
        ),
        "FilamentUpdate": respx.patch(f"{API}/filament-catalog/8").mock(
            return_value=httpx.Response(
                200, json=shaped("FilamentResponse", id=8, name="PLA-R", type="PLA")
            )
        ),
    }
    sync = FakeSync([_plan(writes)])

    result = await _run(sync)

    for schema, route in routes.items():
        assert_recorded_body(SYNC, schema, json.loads(route.calls.last.request.content))
    assert json.loads(routes["CatalogEntryUpdate"].calls.last.request.content) == {
        "name": "Reusable",
        "weight": 250,
    }
    [(_, _, results, _)] = sync.sent
    assert all(r.ok for r in results)
    assert results[3].bb_id == 12 and results[3].answer is not None
    assert result.written == 5


@respx.mock
async def test_a_catalog_update_of_the_weight_takes_the_name_read() -> None:
    _listings()
    weight = _write("update", 6, {"weight": 215}, kind="insert_type")
    unread = _write("update", 77, {"name": "Gone"}, kind="insert_type")
    route = respx.put(f"{API}/inventory/catalog/6").mock(
        return_value=httpx.Response(
            200, json=shaped("CatalogEntryResponse", id=6, name="Bambu cardboard", weight=215)
        )
    )
    sync = FakeSync([_plan([weight, unread])])

    result = await _run(sync)

    sent = json.loads(route.calls.last.request.content)
    assert_recorded_body(SYNC, "CatalogEntryUpdate", sent)
    assert sent == {"name": "Bambu cardboard", "weight": 215}
    [(_, _, [ok, missing], _)] = sync.sent
    assert ok.ok and ok.bb_id == 6
    # An entry this pass did not read: no call, an ``internal`` refusal.
    assert (missing.ok, missing.error, missing.status) == (False, "internal", None)
    assert not respx.calls.last.request.url.path.endswith("/77")
    assert (result.written, result.failed) == (1, 1)


@respx.mock
async def test_a_spool_create_archive_and_restore() -> None:
    _listings()
    body = {"material": "PLA", "rgba": "FF0000FF", "label_weight": 1000}
    writes = [_write("create", None, body), _write("archive", 3, {}), _write("restore", 4, {})]
    create = respx.post(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=_spool_row(21, rgba="FF0000FF"))
    )
    archive = respx.post(f"{API}/inventory/spools/3/archive").mock(
        return_value=httpx.Response(200, json=_spool_row(3, archived_at="2026-11-18T12:00:00"))
    )
    restore = respx.post(f"{API}/inventory/spools/4/restore").mock(
        return_value=httpx.Response(200, json=_spool_row(4))
    )
    sync = FakeSync([_plan(writes)])

    await _run(sync)

    assert_recorded_body(SYNC, "SpoolCreate", json.loads(create.calls.last.request.content))
    assert archive.calls.last.request.content == b""
    assert restore.calls.last.request.content == b""
    [(_, _, [made, archived, restored], _)] = sync.sent
    assert made.ok and made.bb_id == 21
    assert archived.ok and archived.answer is not None and archived.answer["archived_at"]
    assert restored.ok and restored.bb_id == 4


@respx.mock
async def test_a_plan_for_another_instance_stops_the_pass() -> None:
    _listings()
    sync = FakeSync([_plan([], instance_changed=True)])

    result = await _run(sync)

    assert result.instance_changed is True and len(sync.plans) == 1
    # R-T7-7: SB's links belong to another Bambuddy; the pass records nothing.
    assert sync.recorded == []


@respx.mock
async def test_one_activity_call_is_one_pass_and_keeps_more() -> None:
    """R-T7-1: the activity runs one pass and passes ``more`` through; the workflow
    (Task 8) runs the next pass and bounds them (R-T4-21)."""
    routes = _listings()
    sync = FakeSync([_plan([], deferred=1) for _ in range(3)])

    result = await FilamentSyncActivities(sync, _load).one_pass(
        SyncPassInput(instance=INSTANCE, full=False, interval_s=60.0), lambda: None
    )

    assert len(sync.plans) == 1 and sync.recorded == [result]
    assert routes["spools"].call_count == 1
    assert result.more is True and result.deferred == 1
    [(snap, _)] = sync.plans
    assert snap.full is False


@respx.mock
async def test_a_pass_with_nothing_left_ends_the_activity() -> None:
    _listings()
    sync = FakeSync()

    result = await FilamentSyncActivities(sync, _load).one_pass(
        SyncPassInput(instance=INSTANCE, full=True), lambda: None
    )

    assert len(sync.plans) == 1 and result.more is False


def test_the_activity_is_named_for_the_workflow() -> None:
    definition = FilamentSyncActivities(FakeSync(), _load).sync_pass
    assert getattr(definition, "__temporal_activity_definition").name == "filament_bb_sync_pass"
