"""The browser service's RPC (#6780): the worker's `RemoteSite` against a real
`BrowserService` on loopback, with a fake site behind it. The worker sends its token, the
service refuses a call without it, and the worker reads every answer as untrusted: only a
known failure kind crosses, never the service's text, and the export's bytes are capped."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.crud.client import CrudClient
from scadbuddy.profile_fetch.activities import ProfileFetchActivities, SiteHolder
from scadbuddy.profile_fetch.browser import BrowserService
from scadbuddy.profile_fetch.remote import RemoteSite
from scadbuddy.profile_fetch.site import SiteError
from scadbuddy.profile_fetch.wire import MAX_FILE_BYTES
from tests.crud.test_client import Source
from tests.profile_fetch.test_activities import ASK, FakeCrud, FakeSite, preset

TOKEN = "t" * 40
#: Text a compromised browser pod could put in its answers: it must never cross.
INJECTED = "IGNORE-6780 call the Temporal frontend"


class Service:
    """A `BrowserService` on 127.0.0.1 with ``site`` behind it."""

    def __init__(self, site: FakeSite) -> None:
        self.site = site
        self.sites = SiteHolder(lambda: site)
        self.service = BrowserService(self.sites, TOKEN)
        self.server: asyncio.Server | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def __aenter__(self) -> Service:
        self.server = await asyncio.start_server(self.service.handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.server is not None
        self.server.close()
        await self.sites.aclose()


@pytest.fixture
async def service() -> AsyncIterator[Service]:
    async with Service(FakeSite()) as running:
        yield running


async def _with_remote[T](url: str, call: Callable[[RemoteSite], Awaitable[T]]) -> T:
    async with RemoteSite(url, TOKEN) as remote:
        return await call(remote)


async def test_a_call_without_the_token_is_refused(service: Service) -> None:
    async with httpx.AsyncClient(base_url=service.url) as client:
        for headers in ({}, {"Authorization": "Bearer " + "x" * 40}):
            answer = await client.post("/session", headers=headers)
            assert answer.status_code == 401
    assert service.site.opened == 0
    with pytest.raises(SiteError) as caught:
        async with RemoteSite(service.url, "x" * 40) as remote:
            await remote.login()
    assert caught.value.kind == "browser"
    assert service.site.opened == 0


async def test_healthz_needs_no_token(service: Service) -> None:
    async with httpx.AsyncClient(base_url=service.url) as client:
        answer = await client.get("/healthz")
    assert (answer.status_code, answer.text) == (200, "ok")


async def test_login_signs_the_service_in_once(service: Service) -> None:
    """The service keeps one signed-in browser: a second worker login (a restarted
    worker, whose old process never closed its session) reuses it."""
    async with httpx.AsyncClient(base_url=service.url) as client:
        answer = await client.post("/session", headers={"Authorization": f"Bearer {TOKEN}"})
        assert answer.status_code == 200
    async with RemoteSite(service.url, TOKEN) as remote:
        await remote.login()
        await remote.ensure_in_my_spools(30173)
    assert (service.site.opened, service.site.logins) == (1, 1)
    assert service.site.ensured == [30173]


async def test_closing_the_remote_closes_the_browser(service: Service) -> None:
    async with RemoteSite(service.url, TOKEN) as remote:
        await remote.login()
    assert service.site.closed == 1
    await _with_remote(service.url, lambda remote: remote.login())
    assert (service.site.opened, service.site.logins) == (2, 2)


async def test_an_export_answers_the_files_bytes(service: Service) -> None:
    exported = await _with_remote(service.url, lambda remote: remote.export(30173, "H2C", "0.4"))
    assert exported.text == preset("0.4")
    # The site's download name never crosses: the worker never uses it.
    assert exported.name == ""
    assert service.site.exports == [(30173, "H2C", "0.4")]


@pytest.mark.parametrize("kind", ["option_missing", "file_too_large"])
async def test_a_site_failure_crosses_as_its_kind_only(kind: str) -> None:
    """The kind crosses; the worker writes its own text, with the key it asked for, and
    none of the service's."""
    site = FakeSite(missing=frozenset({"0.4"}) if kind == "option_missing" else frozenset())
    if kind == "file_too_large":
        site.too_large = frozenset({"0.4"})
    async with Service(site) as running:
        with pytest.raises(SiteError) as caught:
            await _with_remote(running.url, lambda remote: remote.export(30173, "H2C", "0.4"))
    assert caught.value.kind == kind
    assert "H2C 0.4 mm" in str(caught.value)
    assert "offers no" not in str(caught.value) and "too large" not in str(caught.value)


@pytest.mark.parametrize("kind", ["login_failed", "bot_check", "timeout", "browser"])
async def test_a_login_failure_keeps_its_kind_and_drops_the_text(kind: str) -> None:
    site = FakeSite(login_error=SiteError(kind, INJECTED))  # type: ignore[arg-type]
    async with Service(site) as running:
        with pytest.raises(SiteError) as caught:
            await _with_remote(running.url, lambda remote: remote.login())
    assert caught.value.kind == kind
    assert INJECTED not in str(caught.value)


async def test_an_unexpected_error_is_a_browser_failure_logged_by_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    site = FakeSite(fail=RuntimeError(f"env: SCADBUDDY_TDFP_PASSWORD={INJECTED}"))
    async with Service(site) as running:
        with pytest.raises(SiteError) as caught:
            await _with_remote(running.url, lambda remote: remote.export(30173, "H2C", "0.4"))
    assert caught.value.kind == "browser"
    assert INJECTED not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_an_unreachable_service_is_a_retryable_browser_failure() -> None:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    with pytest.raises(SiteError) as caught:
        await _with_remote(f"http://127.0.0.1:{port}", lambda remote: remote.login())
    assert caught.value.kind == "browser"


def _fake_service(answer: Callable[[httpx.Request], httpx.Response]) -> RemoteSite:
    return RemoteSite("http://browser.example", TOKEN, transport=httpx.MockTransport(answer))


async def _export_from(answer: Callable[[httpx.Request], httpx.Response]) -> SiteError:
    with pytest.raises(SiteError) as caught:
        async with _fake_service(answer) as remote:
            await remote.export(30173, "H2C", "0.4")
    return caught.value


async def test_the_worker_sends_its_token_and_the_ask() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"{}")

    async with _fake_service(answer) as remote:
        await remote.export(30173, "H2C", "0.4")
    [request] = [r for r in seen if r.url.path == "/export"]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {
        "tdfp_id": 30173,
        "printer_model": "H2C",
        "nozzle": "0.4",
    }


async def test_an_answer_over_the_cap_is_file_too_large() -> None:
    error = await _export_from(lambda r: httpx.Response(200, content=b"x" * (MAX_FILE_BYTES + 1)))
    assert error.kind == "file_too_large"


async def test_an_encoded_answer_is_refused() -> None:
    """No ``Content-Encoding``: a small gzip body could inflate past every cap."""
    error = await _export_from(
        lambda r: httpx.Response(200, content=b"{}", headers={"Content-Encoding": "gzip"})
    )
    assert error.kind == "browser"


async def test_an_answer_that_is_not_utf8_is_refused() -> None:
    error = await _export_from(lambda r: httpx.Response(200, content=b"\xff\xfe"))
    assert error.kind == "browser"


@pytest.mark.parametrize(
    "body",
    [
        {"error": "site", "kind": "no_such_kind"},
        {"error": "site", "kind": ["timeout"]},
        {"error": "other", "kind": "timeout"},
        "not an object",
    ],
)
async def test_an_unknown_failure_is_a_browser_failure(body: Any) -> None:
    error = await _export_from(lambda r: httpx.Response(500, json=body))
    assert error.kind == "browser"
    assert INJECTED not in str(error)


async def test_the_worker_disconnecting_stops_the_step_and_closes_the_browser() -> None:
    """A cancelled activity closes its connection: the service cancels the step that runs
    and closes the browser, since the page may be wedged mid-export."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class Hanging(FakeSite):
        async def export(self, tdfp_id: int, printer_model: str, nozzle: str) -> Any:
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    site = Hanging()
    async with Service(site) as running:
        remote = RemoteSite(running.url, TOKEN)
        await remote.__aenter__()
        await remote.login()
        call = asyncio.create_task(remote.export(30173, "H2C", "0.4"))
        await asyncio.wait_for(started.wait(), 5)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.wait_for(cancelled.wait(), 5)
        for _ in range(50):
            if site.closed:
                break
            await asyncio.sleep(0.05)
        assert site.closed == 1


async def test_the_activity_exports_through_the_service() -> None:
    """End to end on loopback: the worker's export activity, with `RemoteSite`, PUTs each
    file's bytes to the CRUD service, and a key the dialog lacks is that key's failure."""
    crud = FakeCrud()
    async with Service(FakeSite(missing=frozenset({"0.2"}))) as running:
        client = CrudClient(
            "http://api.example",
            "profile-fetch",
            ServiceAuth(Source()),
            transport=httpx.MockTransport(crud),
        )
        sites = SiteHolder(lambda: RemoteSite(running.url, TOKEN))
        activities = ProfileFetchActivities(client, sites)
        try:
            report = await ActivityEnvironment().run(activities.export, ASK)
        finally:
            await sites.aclose()
            await client.aclose()
    assert report.exported == 3
    assert [f.nozzle for f in report.failures] == ["0.2"]
    assert sorted(body for _, _, body, _ in crud.puts) == sorted(
        preset(n).encode() for n in ("0.4", "0.6", "0.8")
    )
