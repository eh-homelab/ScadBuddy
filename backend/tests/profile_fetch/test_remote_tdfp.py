"""#7025: the 3DFP search and create over the browser service's RPC. The answers cross as
JSON the worker validates; a failure crosses as its kind only."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.profile_fetch.activities import SiteHolder, TdfpActivities
from scadbuddy.profile_fetch.remote import RemoteSite
from scadbuddy.profile_fetch.steps import SiteError
from scadbuddy.profile_fetch.wire import CreateAnswer, CreateAsk, SearchAnswer, SearchAsk
from tests.profile_fetch.test_activities import SEARCH_ANSWER, FakeSite
from tests.profile_fetch.test_remote import INJECTED, TOKEN, Service, _with_remote
from tests.support.waits import until

ASK = CreateAsk(
    brand="SUNLU",
    material="PLA+/Pro",
    material_type="Silk",
    colour="Copper",
    hexes=["#B87333"],
    gtin="06975373321234",
)


async def test_a_search_answers_the_sites_candidates() -> None:
    site = FakeSite()
    async with Service(site) as running:
        answer = await _with_remote(
            running.url, lambda remote: remote.search(SearchAsk(brand="SUNLU"))
        )
    assert answer == SEARCH_ANSWER
    assert site.searches == [SearchAsk(brand="SUNLU")]


async def test_a_create_answers_what_the_site_did() -> None:
    site = FakeSite()
    async with Service(site) as running:
        answer = await _with_remote(running.url, lambda remote: remote.create(ASK))
    assert answer == site.create_answer
    assert site.creates == [ASK]


@pytest.mark.parametrize(
    "kind", ["no_defaults", "not_created", "option_missing", "browser", "timeout", "bot_check"]
)
async def test_a_create_failure_crosses_as_its_kind_only(kind: str) -> None:
    site = FakeSite()
    site.tdfp_error = SiteError(kind, INJECTED)  # type: ignore[arg-type]
    async with Service(site) as running:
        with pytest.raises(SiteError) as caught:
            await _with_remote(running.url, lambda remote: remote.create(ASK))
    assert caught.value.kind == kind
    assert INJECTED not in str(caught.value)
    assert "3DFP create of SUNLU PLA+/Pro Silk Copper" in str(caught.value)


def _fake(answer: httpx.Response) -> RemoteSite:
    return RemoteSite(
        "http://browser.example", TOKEN, transport=httpx.MockTransport(lambda r: answer)
    )


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        json.dumps({"status": "created", "tdfp_id": -1}).encode(),
        json.dumps({"status": "created", "extra": INJECTED}).encode(),
        json.dumps({"status": "deleted"}).encode(),
    ],
)
async def test_a_create_answer_that_is_not_one_may_have_saved(body: bytes) -> None:
    """The service may be compromised: its answer is checked, and its text never crosses.
    A create whose answer is lost may have saved, so it is ``not_created`` (R-7025-11)."""
    with pytest.raises(SiteError) as caught:
        async with _fake(httpx.Response(200, content=body)) as remote:
            await remote.create(ASK)
    assert caught.value.kind == "not_created"
    assert INJECTED not in str(caught.value)
    assert "may exist" in str(caught.value)


async def test_a_create_the_service_never_answered_may_have_saved() -> None:
    """R-7025-11: no answer from the service is not a failure before the save."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("gone", request=request)

    remote = RemoteSite("http://browser.example", TOKEN, transport=httpx.MockTransport(refuse))
    with pytest.raises(SiteError) as caught:
        async with remote:
            await remote.create(ASK)
    assert caught.value.kind == "not_created"


async def test_a_create_the_service_refused_with_no_kind_may_have_saved() -> None:
    with pytest.raises(SiteError) as caught:
        async with _fake(httpx.Response(500, content=b"{}")) as remote:
            await remote.create(ASK)
    assert caught.value.kind == "not_created"


async def test_a_search_answer_past_its_cap_is_refused() -> None:
    body = json.dumps({"query": "x", "candidates": [], "pad": "x" * 70_000}).encode()
    with pytest.raises(SiteError) as caught:
        async with _fake(httpx.Response(200, content=body)) as remote:
            await remote.search(SearchAsk(brand="SUNLU"))
    assert caught.value.kind == "browser"


async def test_the_create_ask_fits_the_services_ask_cap() -> None:
    """The longest create ask the worker can send is read whole by the service."""
    longest = CreateAsk(
        brand="b" * 200,
        material="m" * 200,
        material_type="t" * 200,
        colour="c" * 200,
        hexes=["#000000"] * 4,
        nominal_weight_g=100_000,
        mpn="p" * 200,
        gtin="1" * 14,
        asin="B" * 10,
        manufacturer_url="https://example.com/" + "u" * 980,  # type: ignore[arg-type]
        empty_spool_weight_g=10_000,
        td=100,
        not_duplicates=list(range(10**9, 10**9 + 25)),
    )
    site = FakeSite()
    async with Service(site) as running:
        answer = await _with_remote(running.url, lambda remote: remote.create(longest))
    assert answer == CreateAnswer(status="created", tdfp_id=40000, sent=["mpn"])


class _HeldExport(FakeSite):
    """An export step that holds the browser until the test lets it go."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.steps: list[str] = []

    async def export(self, tdfp_id: int, printer_model: str, nozzle: str) -> Any:
        self.started.set()
        await self.release.wait()
        self.steps.append("export")
        return await super().export(tdfp_id, printer_model, nozzle)

    async def search(self, ask: SearchAsk) -> SearchAnswer:
        self.steps.append("search")
        return await super().search(ask)


async def test_a_search_takes_its_turn_after_the_export_step_in_hand() -> None:
    """#7062 R-7062-2: a search sent while an export step holds the browser runs as soon as
    that step ends, on its own connection, before the export's next step."""
    site = _HeldExport()
    async with (
        Service(site) as running,
        RemoteSite(running.url, TOKEN) as fetch,
        RemoteSite(running.url, TOKEN) as searching,
    ):
        exporting = asyncio.create_task(fetch.export(30173, "H2C", "0.4"))
        await asyncio.wait_for(site.started.wait(), 5)
        search = asyncio.create_task(searching.search(SearchAsk(brand="SUNLU")))
        # The search's call reaches the service and waits for its turn.
        await _until(lambda: running.service.lock._waiters)
        site.release.set()
        answer = await asyncio.wait_for(search, 5)
        await asyncio.wait_for(exporting, 5)
        await asyncio.wait_for(fetch.export(30173, "H2C", "0.6"), 5)
    assert answer == SEARCH_ANSWER
    assert site.steps == ["export", "search", "export"]


async def test_a_search_given_up_before_its_turn_runs_nothing_and_keeps_the_browser() -> None:
    """A search whose worker went away while it waited for its turn runs nothing, and the
    browser the export signed in stays open (only a step cut mid-way closes it)."""
    site = _HeldExport()
    async with Service(site) as running, RemoteSite(running.url, TOKEN) as fetch:
        exporting = asyncio.create_task(fetch.export(30173, "H2C", "0.4"))
        await asyncio.wait_for(site.started.wait(), 5)
        searching = RemoteSite(running.url, TOKEN)
        await searching.__aenter__()
        search = asyncio.create_task(searching.search(SearchAsk(brand="SUNLU")))
        await _until(lambda: running.service.lock._waiters)
        search.cancel()
        with pytest.raises(asyncio.CancelledError):
            await search
        # The worker's connection closes with its activity; no DELETE /session is sent.
        assert searching._client is not None
        await searching._client.aclose()
        searching._client = None
        await _until(lambda: not running.service.lock._waiters)
        site.release.set()
        await asyncio.wait_for(exporting, 5)
        await asyncio.wait_for(fetch.export(30173, "H2C", "0.6"), 5)
        assert site.steps == ["export", "export"]
        assert site.closed == 0


def _searches(url: str) -> TdfpActivities:
    """The worker's search activities, on a client that never closes the browser
    (`worker.run`'s ``search_sites``, R-7062-3)."""
    return TdfpActivities(SiteHolder(lambda: RemoteSite(url, TOKEN, closes_browser=False)))


async def test_a_search_cancelled_while_an_export_step_runs_keeps_the_browser() -> None:
    """R-7062-3: a search activity cancelled while it waits for the export's step closes
    only its own connection. The service's browser is not dropped, and the export's next
    step runs on the same browser."""
    site = _HeldExport()
    async with Service(site) as running, RemoteSite(running.url, TOKEN) as fetch:
        await fetch.login()
        exporting = asyncio.create_task(fetch.export(30173, "H2C", "0.4"))
        await asyncio.wait_for(site.started.wait(), 5)
        searches = _searches(running.url)
        search = asyncio.create_task(
            ActivityEnvironment().run(searches.search, SearchAsk(brand="SUNLU"))
        )
        await _until(lambda: running.service.lock._waiters)
        search.cancel()
        # The cancel ends at once: it sends no DELETE /session, which would wait behind the
        # export's step and then close its browser.
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(search, 5)
        finally:
            site.release.set()
        await asyncio.wait_for(exporting, 5)
        await asyncio.wait_for(fetch.export(30173, "H2C", "0.6"), 5)
        assert site.steps == ["export", "export"]
        assert (site.opened, site.closed) == (1, 0)


async def test_a_search_that_fails_fresh_lets_the_export_go_on() -> None:
    """R-7062-4: a search failing ``browser`` closes the browser on the service, under the
    lock; the export's next step signs in again and succeeds."""
    site = _HeldExport()
    site.tdfp_error = SiteError("browser", "the page crashed")
    async with Service(site) as running, RemoteSite(running.url, TOKEN) as fetch:
        await fetch.login()
        searches = _searches(running.url)
        with pytest.raises(ApplicationError) as caught:
            await ActivityEnvironment().run(searches.search, SearchAsk(brand="SUNLU"))
        assert caught.value.type == "browser"
        site.release.set()
        exported = await asyncio.wait_for(fetch.export(30173, "H2C", "0.4"), 5)
        assert exported.text
        assert site.steps == ["search", "export"]


@pytest.mark.parametrize("kind", ["browser", "timeout"])
async def test_a_failed_search_resets_the_browser_for_the_next(kind: str) -> None:
    """R-7062-4: a search failing ``browser`` or ``timeout`` closes the service's browser
    while it holds the lock, so the next search gets a fresh one and answers, with no fetch
    or create to reset it."""
    site = FakeSite()
    site.tdfp_error = SiteError(kind, "the page hung")  # type: ignore[arg-type]
    async with Service(site) as running:
        searches = _searches(running.url)
        with pytest.raises(ApplicationError):
            await ActivityEnvironment().run(searches.search, SearchAsk(brand="SUNLU"))
        assert (site.opened, site.closed) == (1, 1)
        site.tdfp_error = None
        answer = await ActivityEnvironment().run(searches.search, SearchAsk(brand="SUNLU"))
    assert answer == SEARCH_ANSWER
    assert site.opened == 2


async def test_a_search_that_fails_otherwise_keeps_the_browser() -> None:
    """Only ``browser`` and ``timeout`` reset it: a search the site answered with another
    failure leaves the signed-in browser for the next step."""
    site = FakeSite()
    site.tdfp_error = SiteError("bot_check", "a check")
    async with Service(site) as running:
        searches = _searches(running.url)
        with pytest.raises(ApplicationError):
            await ActivityEnvironment().run(searches.search, SearchAsk(brand="SUNLU"))
        assert (site.opened, site.closed) == (1, 0)


async def test_a_search_cut_mid_step_keeps_the_browser() -> None:
    """R-7062-3: the service cancels a search whose worker went away during it, and keeps
    the browser open (an export's step cut so closes it, as before)."""
    started = asyncio.Event()

    class Hanging(FakeSite):
        async def search(self, ask: SearchAsk) -> SearchAnswer:
            started.set()
            await asyncio.sleep(60)
            raise AssertionError("not cancelled")

    site = Hanging()
    async with Service(site) as running, RemoteSite(running.url, TOKEN) as fetch:
        await fetch.login()
        searching = RemoteSite(running.url, TOKEN, closes_browser=False)
        await searching.__aenter__()
        search = asyncio.create_task(searching.search(SearchAsk(brand="SUNLU")))
        await asyncio.wait_for(started.wait(), 5)
        search.cancel()
        with pytest.raises(asyncio.CancelledError):
            await search
        await searching.__aexit__(None, None, None)
        await _until(lambda: not running.service.lock.locked())
        await asyncio.wait_for(fetch.export(30173, "H2C", "0.4"), 5)
        assert (site.opened, site.closed) == (1, 0)


async def test_a_call_cancelled_once_it_has_the_lock_gives_it_back() -> None:
    """A handler cancelled after its turn took the lock, before it woke, releases the lock
    and runs nothing."""
    service = Service(FakeSite()).service
    ran: list[bytes] = []

    async def step(ask: bytes) -> Any:
        ran.append(ask)

    handler = asyncio.create_task(service._run(asyncio.StreamReader(), step, b"x"))
    # The turn takes the free lock on its first step; the handler wakes a step later, so
    # one loop pass at a time finds the moment between the two.
    for _ in range(100):
        if service.lock.locked():
            break
        await asyncio.sleep(0)
    assert service.lock.locked() and not ran
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler
    assert ran == []
    assert not service.lock.locked()


async def test_a_worker_gone_as_the_lock_frees_runs_nothing_and_gives_it_back() -> None:
    """`_turn`'s worker-gone path: the worker goes away while its call waits for the lock,
    and the lock frees in the same loop tick, so its turn and its watcher are both done
    when it wakes. It runs nothing, answers None, and gives the lock back."""
    service = Service(FakeSite()).service
    ran: list[bytes] = []

    async def step(ask: bytes) -> Any:
        ran.append(ask)

    reader = asyncio.StreamReader()
    await service.lock.acquire()
    handler = asyncio.create_task(service._run(reader, step, b"x"))
    await _until(lambda: service.lock._waiters)
    # One tick: the turn's acquire and the watcher's read both end before the handler wakes.
    reader.feed_eof()
    service.lock.release()
    assert await asyncio.wait_for(handler, 5) is None
    assert ran == []
    assert not service.lock.locked()


async def _until(ready: Any) -> None:
    """Wait (bounded, no fixed sleep) until ``ready()`` holds."""
    await until(ready, 5)
