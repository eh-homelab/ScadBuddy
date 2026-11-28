"""The Bambuddy pull's reads on the print worker (#6772): what ``FilamentBbPull``'s activity
reads of Bambuddy (respx; never a real Bambuddy) and what it sends to SB, on a fake
`BbPullApi` that records each call. SB's side is `tests/crud/test_crud_filament_pull.py`'s;
both in one process are `tests/filament/test_bambuddy_pull.py`'s."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.api.limits import MAX_TEXT_BODY_BYTES
from scadbuddy.bambuddy.errors import UpstreamAnswer
from scadbuddy.bambuddy.models import Archive, SpoolUsage
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.client import CrudRefusedError
from scadbuddy.filament import bb_pull_run
from scadbuddy.filament.bambuddy_pull import PullReport, Pulls
from scadbuddy.filament.bb_pull_models import (
    ARCHIVES_MAX,
    ColoursApplied,
    ColoursBody,
    HaBody,
    PullApplied,
    PullBody,
    PullInstanceChangedError,
    PullProblem,
    PullStart,
    TraysBody,
    UsageBody,
    UsageBooked,
)
from scadbuddy.filament.bb_pull_run import FilamentPullActivities, run_pull
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.library.settings_store import StoredSettings

BB = "https://bambuddy.test"
API = f"{BB}/api/v1"
INSTANCE = instance_for(BB)
PULL_ID = f"filament-bb-pull-{'a' * 32}"
SETTINGS = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
SPOOL = {"id": 7, "material": "PLA", "label_weight": 1000, "weight_used": 100.0}
USAGE = {
    "id": 3,
    "spool_id": 7,
    "printer_id": 1,
    "print_name": "job",
    "weight_used": 12.5,
    "created_at": "2026-11-14T10:00:00",
}
ARCHIVE = {"id": 40, "printer_id": 1, "completed_at": "2026-11-14T10:00:00", "print_name": "x"}


class FakeApi:
    """`BbPullApi` recording each body; ``since`` is what `apply` answers as the archives'
    date, ``changed`` makes `apply` refuse as for another Bambuddy, ``refused`` makes it
    raise that error (SB's refusal of the body)."""

    def __init__(
        self, since: date | None = None, changed: bool = False, refused: Exception | None = None
    ) -> None:
        self.since = since
        self.changed = changed
        self.refused = refused
        self.calls: list[tuple[str, Any]] = []

    async def apply(self, body: PullBody) -> PullApplied:
        self.calls.append(("apply", body))
        if self.changed:
            raise PullInstanceChangedError(body.instance)
        if self.refused is not None:
            raise self.refused
        return PullApplied(report=PullReport(created=1), archives_since=self.since)

    async def book_usage(self, body: UsageBody) -> UsageBooked:
        self.calls.append(("usage", body))
        return UsageBooked(booked=len(body.usage), unlinked=0)

    async def ha_states(self, body: HaBody) -> None:
        self.calls.append(("ha", body))

    async def reconcile_trays(self, body: TraysBody) -> int:
        self.calls.append(("trays", body))
        return 0

    async def colours(self, body: ColoursBody) -> ColoursApplied:
        self.calls.append(("colours", body))
        if self.refused is not None:
            raise self.refused
        return ColoursApplied(added=len(body.colours), kept=1)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def body(self, name: str) -> Any:
        return next(body for called, body in self.calls if called == name)


async def _load() -> StoredSettings:
    return SETTINGS


def _start(**over: Any) -> PullStart:
    return PullStart.model_validate(
        {"instance": INSTANCE, "pull_id": PULL_ID, "pulls": Pulls(), "reconcile": True} | over
    )


def _bambuddy(router: respx.MockRouter, *, usage: list[Any] | None = None) -> None:
    router.get(f"{API}/inventory/spools").respond(json=[SPOOL, ["not", "a", "spool"]])
    router.get(f"{API}/inventory/usage").respond(json=usage or [])
    router.get(f"{API}/inventory/catalog").respond(json=[{"id": 5, "name": "Reel", "weight": 250}])
    router.get(f"{API}/inventory/assignments").respond(json=[])


async def test_the_worker_reads_bambuddy_and_sends_sb_one_body() -> None:
    """The spools as Bambuddy sent them (a malformed row too: SB counts it), the usage rows
    and the catalog, in one body; then the usage rows to book, then the trays."""
    api = FakeApi()
    with respx.mock(assert_all_called=False) as router:
        _bambuddy(router, usage=[USAGE])
        archives = router.get(f"{API}/archives/").respond(json=[ARCHIVE])
        outcome = await run_pull(api, _load, _start())
        assert not archives.called
    assert outcome.outcome == "pulled"
    assert outcome.report is not None and outcome.report.created == 1
    assert outcome.report.usage_booked == 1
    assert api.names() == ["apply", "usage", "trays"]
    body: PullBody = api.body("apply")
    # The run's id goes back with the rows; no actor does (Decision 181).
    assert (body.instance, body.pull_id) == (INSTANCE, PULL_ID)
    assert "actor" not in body.model_dump()
    assert body.spools == [SPOOL, ["not", "a", "spool"]]
    assert [u.id for u in body.usage] == [3]
    assert body.catalog is not None and [c.id for c in body.catalog] == [5]
    assert (body.suppliers, body.locations, body.usage_failed) == (None, None, False)
    assert api.body("usage").archives == []


COLOUR = {
    "id": 9,
    "manufacturer": "Bambu Lab",
    "color_name": "Jade White",
    "hex_color": "#FFFFFF",
    "material": "PLA Basic",
    "is_default": True,
}


async def test_the_colour_catalog_goes_to_sb_after_the_spools() -> None:
    """#6907: under ``spools`` the worker reads ``GET /inventory/colors`` and sends it on
    its own route, after the pull's body."""
    api = FakeApi()
    with respx.mock(assert_all_called=False) as router:
        _bambuddy(router)
        colours = router.get(f"{API}/inventory/colors").respond(json=[COLOUR])
        outcome = await run_pull(api, _load, _start())
        assert colours.called
    assert api.names() == ["apply", "colours", "trays"]
    assert [c.id for c in api.body("colours").colours] == [9]
    assert outcome.report is not None
    assert (outcome.report.colours, outcome.report.colours_kept) == (1, 1)
    assert not outcome.report.colours_failed


@pytest.mark.parametrize("answer", [httpx.Response(404), httpx.Response(200, json={"no": 1})])
async def test_a_failed_colour_read_fails_alone(answer: httpx.Response) -> None:
    api = FakeApi()
    with respx.mock(assert_all_called=False) as router:
        _bambuddy(router)
        router.get(f"{API}/inventory/colors").mock(return_value=answer)
        outcome = await run_pull(api, _load, _start())
    assert outcome.outcome == "pulled" and outcome.report is not None
    assert outcome.report.colours_failed and outcome.report.created == 1
    assert "colours" not in api.names()


async def test_no_colours_are_read_without_the_spools() -> None:
    api = FakeApi()
    with respx.mock(assert_all_called=False) as router:
        _bambuddy(router)
        colours = router.get(f"{API}/inventory/colors").respond(json=[COLOUR])
        await run_pull(api, _load, _start(pulls=Pulls(spools=False)))
        assert not colours.called


async def test_the_archives_are_read_from_the_date_sb_names() -> None:
    api = FakeApi(since=date(2026, 11, 13))
    with respx.mock(assert_all_called=False) as router:
        _bambuddy(router, usage=[USAGE])
        archives = router.get(f"{API}/archives/").respond(json=[ARCHIVE])
        await run_pull(api, _load, _start())
        assert archives.call_count == 1
        assert archives.calls[0].request.url.params["date_from"] == "2026-11-13"
    (sent,) = api.body("usage").archives
    # Only what the booking matches by goes to SB.
    assert sent.model_dump(exclude_none=True, exclude_defaults=True) == {
        "id": 40,
        "printer_id": 1,
        # Naive, as Bambuddy sent it: `match_archive` reads it as UTC.
        "completed_at": datetime(2026, 11, 14, 10),
    }


async def test_a_failed_usage_read_is_said_and_books_nothing() -> None:
    api = FakeApi()
    with respx.mock(assert_all_called=False) as router:
        _bambuddy(router)
        router.get(f"{API}/inventory/usage").respond(503)
        await run_pull(api, _load, _start())
    assert api.body("apply").usage_failed is True
    assert "usage" not in api.names()


async def test_the_catalog_and_usage_follow_the_data_types() -> None:
    """No catalog read without ``spools``, no usage read without ``spools`` or
    ``weights``, suppliers and locations only under their own type (#6265 F1)."""
    api = FakeApi()
    pulls = Pulls(spools=False, weights=False, suppliers=True, locations=True)
    with respx.mock(assert_all_called=False) as router:
        _bambuddy(router)
        router.get(f"{API}/inventory/suppliers").respond(json=[])
        router.get(f"{API}/inventory/locations").respond(json=[])
        await run_pull(api, _load, _start(pulls=pulls))
        assert not router.routes[1].called and not router.routes[2].called
    body: PullBody = api.body("apply")
    assert (body.catalog, body.suppliers, body.locations, body.pulls) == ([], [], [], pulls)


async def test_a_failed_catalog_read_is_sent_as_none() -> None:
    api = FakeApi()
    with respx.mock(assert_all_called=False) as router:
        _bambuddy(router)
        router.get(f"{API}/inventory/catalog").respond(500)
        await run_pull(api, _load, _start())
    assert api.body("apply").catalog is None


async def test_ha_states_are_read_only_when_asked_and_a_failure_sends_none() -> None:
    api = FakeApi()
    entities = f"{API}/location-ha-sensors/entities"
    with respx.mock(assert_all_called=False) as router:
        route = router.get(entities).respond(
            json=[{"entity_id": "sensor.a", "state": "18.4"}, {"entity_id": "sensor.b"}]
        )
        assert (await run_pull(api, _load, _start(pulls=None))).outcome == "skipped"
        assert not route.called
        assert api.calls == []
        assert (await run_pull(api, _load, _start(pulls=None, ha=True))).outcome == "skipped"
        assert route.call_count == 1
    assert api.calls == [
        ("ha", HaBody(instance=INSTANCE, states={"sensor.a": "18.4", "sensor.b": None}))
    ]
    api.calls.clear()
    with respx.mock() as router:
        router.get(entities).respond(503)
        await run_pull(api, _load, _start(pulls=None, ha=True))
    assert api.calls == [("ha", HaBody(instance=INSTANCE, states=None))]


async def test_bambuddy_down_fails_the_schedules_pull_and_writes_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeApi()
    with respx.mock() as router:
        router.get(f"{API}/inventory/spools").respond(503)
        outcome = await run_pull(api, _load, _start())
    assert (outcome.outcome, outcome.error) == ("failed", None)
    assert api.calls == []
    assert "could not pull Bambuddy's spools" in caplog.text


async def test_an_import_answers_bambuddys_refusal() -> None:
    """A person's import (``strict``) gets Bambuddy's problem back, for the route."""
    api = FakeApi()
    with respx.mock() as router:
        router.get(f"{API}/inventory/spools").respond(401, json={"detail": "nope"})
        outcome = await run_pull(api, _load, _start(strict=True, reconcile=False))
    assert outcome.outcome == "failed"
    assert outcome.error is not None and outcome.error.status == outcome.error.error().status
    assert outcome.error.status >= 400
    assert api.calls == []


GATE_413 = CrudRefusedError(
    413, {"detail": "the body declares 9000000 bytes and this API reads at most 8388608"}
)


@pytest.mark.parametrize(
    "refused",
    [GATE_413, CrudRefusedError(422, {"detail": "spools: at most 20000 items"})],
)
async def test_an_import_answers_sbs_refusal_as_a_502_not_a_retry(
    refused: CrudRefusedError, caplog: pytest.LogCaptureFixture
) -> None:
    """SB refused the body (the gate's 413, a cap's or the schema's 422): sending it again
    gets the same answer, so an import is told so as a 502 naming the refusal, never the
    route's 503 "try again"; the Schedule's pull logs it as any failure."""
    with respx.mock(assert_all_called=False) as router:
        _bambuddy(router)
        strict = await run_pull(FakeApi(refused=refused), _load, _start(strict=True))
        scheduled = await run_pull(FakeApi(refused=refused), _load, _start())
    assert strict.outcome == "failed"
    assert strict.error is not None
    assert strict.error.status == 502
    assert f"{refused.status}: {refused.problem['detail']}" in strict.error.detail
    assert (scheduled.outcome, scheduled.error) == ("failed", None)
    assert "could not pull Bambuddy's spools" in caplog.text


async def test_an_import_raises_a_busy_answer_from_sb() -> None:
    """A 429 is not the body's fault: the import's run fails (the route's 503)."""
    busy = CrudRefusedError(429, {"detail": "busy"}, retry_after="5")
    with respx.mock(assert_all_called=False) as router:
        _bambuddy(router)
        with pytest.raises(CrudRefusedError):
            await run_pull(FakeApi(refused=busy), _load, _start(strict=True))


def test_a_problem_keeps_its_headers_and_upstream() -> None:
    """The import answers Bambuddy's refusal as it did when the API read Bambuddy: its
    headers (a Retry-After) and what Bambuddy itself answered cross the workflow too."""
    error = ApiError(503, "Bambuddy is busy", headers={"Retry-After": "7"}, reason="busy")
    error.upstream = UpstreamAnswer(status=503, retry_after="7", detail="later")
    crossed = PullProblem.model_validate_json(PullProblem.of(error).model_dump_json())
    again = crossed.error()
    assert again.headers == {"Retry-After": "7"}
    assert again.upstream == error.upstream
    assert (again.status, again.detail, again.extensions) == (
        503,
        "Bambuddy is busy",
        {"reason": "busy"},
    )


def test_a_start_with_a_field_of_a_later_build_is_read() -> None:
    """A start written by a newer API while an older print worker still takes the queue
    (a rolling deploy) is read, its unknown field dropped."""
    start = PullStart.model_validate(
        {"instance": INSTANCE, "pull_id": PULL_ID, "pulls": None, "later": True}
    )
    assert start.pull_id == PULL_ID


async def test_another_bambuddy_reads_nothing() -> None:
    """The settings name another Bambuddy than the start's: nothing is read or sent, and an
    import is told to import again."""
    api = FakeApi()
    with respx.mock() as router:
        outcome = await run_pull(api, _load, _start(instance="0" * 12))
        strict = await run_pull(api, _load, _start(instance="0" * 12, strict=True))
        assert not router.calls
    assert (outcome.outcome, outcome.error) == ("skipped", None)
    assert strict.error is not None and strict.error.status == 409
    # The page tells it apart from "no Bambuddy is set up", which shares its problem type.
    assert strict.error.extensions == {"code": "url_changed"}
    assert api.calls == []


async def test_sb_refusing_another_bambuddy_skips_the_pull() -> None:
    """The URL changed between the read and SB's apply: SB writes nothing (409), and the
    pull is skipped with no booking and no tray reconcile."""
    api = FakeApi(changed=True)
    with respx.mock(assert_all_called=False) as router:
        _bambuddy(router, usage=[USAGE])
        outcome = await run_pull(api, _load, _start())
    assert outcome.outcome == "skipped"
    assert api.names() == ["apply"]


async def test_the_activity_runs_the_pull() -> None:
    api = FakeApi()
    with respx.mock(assert_all_called=False) as router:
        _bambuddy(router)
        outcome = await FilamentPullActivities(api, _load).pull_read(_start(reconcile=False))
    assert outcome.outcome == "pulled"
    assert api.names() == ["apply"]


@pytest.mark.parametrize("status", [200, 503])
async def test_the_trays_are_reconciled_only_after_a_pull_that_asks(status: int) -> None:
    api = FakeApi()
    with respx.mock(assert_all_called=False) as router:
        _bambuddy(router)
        router.get(f"{API}/inventory/assignments").mock(
            return_value=httpx.Response(status, json=[])
        )
        await run_pull(api, _load, _start())
    assert ("trays" in api.names()) is (status == 200)


def test_a_full_usage_body_stays_under_the_body_gate() -> None:
    """#6860 R-6860-2: a booking body at its most (`USAGE_BODY_ROWS` rows and
    `ARCHIVES_MAX` archives, every field filled) is well under the API's gate."""
    row = SpoolUsage.model_validate(
        USAGE
        | {"id": 2**31, "spool_id": 2**31, "printer_id": 2**31, "print_name": "x" * 64}
        | {"percent_used": 100.0, "status": "completed"}
    )
    archive = Archive(id=2**31, printer_id=2**31, completed_at=datetime(2026, 11, 14, 10, 0, 0, 1))
    body = UsageBody(
        instance=INSTANCE,
        pull_id=PULL_ID,
        usage=[row] * bb_pull_run.USAGE_BODY_ROWS,
        archives=[archive] * ARCHIVES_MAX,
    )
    assert len(body.model_dump_json()) < MAX_TEXT_BODY_BYTES // 2


def test_a_full_usage_read_is_one_body_and_a_small_part_of_the_pull_body() -> None:
    """#7002 R-7002-1, R-7002-2: the read's most (`USAGE_READ_ROWS` rows of distinct prints,
    every field filled) books in one body, so `_cover` sees every row of the read, and it
    is about 1.2 MiB of the pull's body (measured), under a sixth of the gate."""
    rows = [
        SpoolUsage.model_validate(
            USAGE
            | {"id": 2**31 - i, "spool_id": 2**31, "printer_id": 2**31, "print_name": "x" * 64}
            | {"percent_used": 100.0, "status": "completed", "weight_used": 1234.5678}
            | {"created_at": datetime(2026, 11, 14, 10, 0, 0, 1 + i).isoformat()}
        )
        for i in range(bb_pull_run.USAGE_READ_ROWS)
    ]
    assert len(bb_pull_run.usage_bodies(rows, [], bb_pull_run.USAGE_BODY_ROWS)) == 1
    body = PullBody(instance=INSTANCE, pull_id=PULL_ID, pulls=Pulls(), spools=[], usage=rows)
    assert len(body.model_dump_json()) < MAX_TEXT_BODY_BYTES // 6
