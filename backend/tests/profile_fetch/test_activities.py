"""The profile-fetch worker's activities (#6625 Task 6) against a fake site and a fake CRUD
service: each exported file is PUT as its bytes, named by their sha256; a key the dialog
lacks is a failure, not an error; the site's failures keep Decision 13's retry classes and
carry no secret."""

from __future__ import annotations

import asyncio
import hashlib
import json
import traceback
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Self
from urllib.parse import parse_qs

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.bambuddy.client import DEFAULT_TIMEOUT as BAMBUDDY_TIMEOUT
from scadbuddy.crud.client import CrudClient
from scadbuddy.profile_fetch.activities import (
    BASE,
    RETRYABLE_KINDS,
    ProfileFetchActivities,
    SiteHolder,
    site_failure,
)
from scadbuddy.profile_fetch.steps import ExportedFile, Kind, SiteError
from scadbuddy.profile_fetch.wire import (
    MAX_FILE_BYTES,
    NOT_RUNNING,
    ExportFailure,
    ExportReport,
    ExportTarget,
    FetchAsk,
)
from scadbuddy.profile_fetch.workflow import IMPORT_TIMEOUT
from tests.crud.test_client import Source

HEADER = {"X-ScadBuddy-Crud": "1"}
RUN = "0f0e0d0c-0000-4000-8000-000000000066"
EMAIL = "owner-6625@example.invalid"
PASSWORD = "pw-6625-not-a-real-password"
ASK = FetchAsk(
    run_id=RUN,
    type_id="0f0e0d0c-0000-4000-8000-000000000001",
    tdfp_id=30173,
    targets=[ExportTarget(printer_model="H2C", nozzles=["0.2", "0.4", "0.6", "0.8"])],
)


def preset(nozzle: str) -> str:
    return json.dumps({"name": f"Inland PLA+ @BBL H2C {nozzle} nozzle", "inherits": "x"})


class FakeSite:
    """`ThreeDfpSite`'s surface. ``missing`` nozzles are ``option_missing``; ``fail``
    makes every export raise it; ``login_error`` makes the login raise it."""

    def __init__(
        self,
        *,
        missing: frozenset[str] = frozenset(),
        too_large: frozenset[str] = frozenset(),
        fail: BaseException | None = None,
        login_error: BaseException | None = None,
        text: Callable[[str], str] = preset,
    ) -> None:
        self.missing = missing
        self.too_large = too_large
        self.fail = fail
        self.login_error = login_error
        self.text = text
        self.opened = 0
        self.closed = 0
        self.logins = 0
        self.ensured: list[int] = []
        self.exports: list[tuple[int, str, str]] = []

    async def __aenter__(self) -> Self:
        self.opened += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.closed += 1

    async def login(self) -> None:
        self.logins += 1
        if self.login_error is not None:
            raise self.login_error

    async def ensure_in_my_spools(self, tdfp_id: int) -> None:
        self.ensured.append(tdfp_id)

    async def export(self, tdfp_id: int, printer_model: str, nozzle: str) -> ExportedFile:
        self.exports.append((tdfp_id, printer_model, nozzle))
        if self.fail is not None:
            raise self.fail
        if nozzle in self.missing:
            raise SiteError("option_missing", f"the export dialog offers no '{nozzle} mm' option")
        if nozzle in self.too_large:
            raise SiteError("file_too_large", f"the exported file of H2C {nozzle} mm is too large")
        return ExportedFile(name=f"../{nozzle}/evil.json", text=self.text(nozzle))


class FakeCrud:
    """The CRUD service's profile-fetch routes, answering from what they hold."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, dict[str, list[str]], bytes, str | None]] = []
        self.imports: list[tuple[dict[str, Any], str | None]] = []
        #: The read timeout each import request was sent with.
        self.import_reads: list[float | None] = []
        self.failed: list[dict[str, Any]] = []
        #: A problem every route answers instead: ``(status, code)``.
        self.refuse: tuple[int, str] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.refuse is not None:
            status, code = self.refuse
            problem = {"type": f"x/{code}", "status": status, "detail": code, "code": code}
            return httpx.Response(status, json=problem, headers=HEADER)
        if path.startswith(f"{BASE}/{RUN}/files/"):
            self.puts.append(
                (
                    path.rsplit("/", 1)[1],
                    parse_qs(request.url.query.decode()),
                    request.content,
                    request.headers.get("Content-Type"),
                )
            )
            return httpx.Response(204, headers=HEADER)
        if path == f"{BASE}/{RUN}/import":
            self.imports.append(
                (json.loads(request.content), request.headers.get("Idempotency-Key"))
            )
            self.import_reads.append(request.extensions["timeout"]["read"])
            outcome = {"status": "partial", "presets": 3, "k_values": 0, "missing": [], "notes": []}
            return httpx.Response(200, json=outcome, headers=HEADER)
        if path == f"{BASE}/{RUN}/failed":
            self.failed.append(json.loads(request.content))
            return httpx.Response(204, headers=HEADER)
        return httpx.Response(404)


def run[T](
    site: FakeSite,
    crud: FakeCrud,
    call: Callable[[ProfileFetchActivities], Awaitable[T]],
    beats: list[object] | None = None,
) -> T:
    client = CrudClient(
        "http://api",
        "profile-fetch",
        ServiceAuth(Source()),
        transport=httpx.MockTransport(crud),
        unavailable_budget=0.2,
        backoff=0.01,
    )
    sites = SiteHolder(lambda: site)
    activities = ProfileFetchActivities(client, sites)
    env = ActivityEnvironment()
    if beats is not None:
        env.on_heartbeat = lambda *details: beats.append(details)

    async def inside() -> T:
        return await call(activities)

    async def go() -> T:
        try:
            return await env.run(inside)
        finally:
            await sites.aclose()
            await client.aclose()

    return asyncio.run(go())


def export(site: FakeSite, crud: FakeCrud, beats: list[object] | None = None) -> ExportReport:
    return run(site, crud, lambda a: a.export(ASK), beats)


def test_export_puts_each_file_and_reports_failures() -> None:
    site, crud = FakeSite(missing=frozenset({"0.2"})), FakeCrud()
    beats: list[object] = []
    report = export(site, crud, beats)
    assert report.exported == 3
    [failure] = report.failures
    assert (failure.printer_model, failure.nozzle) == ("H2C", "0.2")
    assert "0.2 mm" in failure.reason
    assert [(n, q) for _, q, _, _ in crud.puts for n in q["nozzle"]] == [
        ("0.4", {"printer_model": ["H2C"], "nozzle": ["0.4"]}),
        ("0.6", {"printer_model": ["H2C"], "nozzle": ["0.6"]}),
        ("0.8", {"printer_model": ["H2C"], "nozzle": ["0.8"]}),
    ]
    for sha256, query, body, kind in crud.puts:
        # The bytes, named by their sha256; the site's name is never a path.
        assert hashlib.sha256(body).hexdigest() == sha256
        assert body == preset(query["nozzle"][0]).encode()
        assert kind == "application/json"
    assert site.ensured == [30173]
    assert site.logins == 1
    assert beats  # it heartbeats while it runs


def test_a_file_over_the_cap_is_a_failure_not_a_put() -> None:
    site = FakeSite(
        text=lambda nozzle: "x" * (MAX_FILE_BYTES + 1) if nozzle == "0.4" else preset(nozzle)
    )
    crud = FakeCrud()
    report = export(site, crud)
    assert report.exported == 3
    assert [(f.nozzle, f.reason) for f in report.failures] == [
        ("0.4", "the exported file is larger than 512 KiB")
    ]
    assert len(crud.puts) == 3


def test_a_file_the_page_refused_is_that_keys_failure() -> None:
    """The page's hook refusing a file over 512 KiB (``file_too_large``, R-F7) skips that
    key, reported; the other keys go on, and the attempt is not failed or retried."""
    crud = FakeCrud()
    report = export(FakeSite(too_large=frozenset({"0.6"})), crud)
    assert report.exported == 3
    assert [(f.nozzle, f.reason) for f in report.failures] == [
        ("0.6", "the exported file of H2C 0.6 mm is too large")
    ]
    assert len(crud.puts) == 3


def test_a_file_the_api_refuses_is_a_failure() -> None:
    """A 422 (not a filament preset) is that key's failure; the others still go."""

    class Refusing(FakeCrud):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if "nozzle=0.6" in str(request.url):
                problem = {"type": "x/422", "status": 422, "detail": "not a filament preset"}
                return httpx.Response(422, json=problem, headers=HEADER)
            return super().__call__(request)

    report = export(FakeSite(), Refusing())
    assert report.exported == 3
    [failure] = report.failures
    assert failure.nozzle == "0.6"
    assert failure.reason == "ScadBuddy refused the exported file: not a filament preset"


@pytest.mark.parametrize(
    ("kind", "retryable"),
    [
        ("browser", True),
        ("timeout", True),
        ("bot_check", True),
        ("login_failed", False),
        ("tdfp_not_found", False),
        ("option_missing", False),
        ("file_too_large", False),
    ],
)
def test_the_retry_classes(kind: Kind, retryable: bool) -> None:
    """Decision 13 (ruling b): a browser failure, a timeout and a bot check are tried
    again; a refused login, an unknown id and a missing option are not."""
    assert (kind in RETRYABLE_KINDS) is retryable
    mapped = site_failure(SiteError(kind))
    assert (mapped.type, mapped.non_retryable) == (kind, not retryable)
    if kind in ("option_missing", "file_too_large"):
        return  # a key's failure, reported (`test_export_puts_each_file_and_reports_failures`)
    with pytest.raises(ApplicationError) as caught:
        export(FakeSite(fail=SiteError(kind)), FakeCrud())
    assert caught.value.type == kind
    assert caught.value.non_retryable is not retryable


def test_login_failed_is_non_retryable() -> None:
    site = FakeSite(login_error=SiteError("login_failed"))
    crud = FakeCrud()
    with pytest.raises(ApplicationError) as caught:
        export(site, crud)
    assert caught.value.type == "login_failed"
    assert caught.value.non_retryable
    assert crud.puts == [] and site.exports == []


def _chain(error: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    todo = [error]
    while todo:
        current = todo.pop()
        if any(current is known for known in seen):
            continue
        seen.append(current)
        todo.extend(e for e in (current.__cause__, current.__context__) if e is not None)
    return seen


@pytest.mark.parametrize("where", ["login", "export"])
def test_failed_reason_carries_no_secret(where: str) -> None:
    """Review Focus 1: whatever the browser raised, the activity's error, which becomes
    the workflow's ``failed`` reason and sits in Temporal's history, holds neither the
    email nor the password, in its text, its traceback or its chain."""
    leak = RuntimeError(f'fill("{EMAIL}") fill("{PASSWORD}")')
    site = FakeSite(login_error=leak) if where == "login" else FakeSite(fail=leak)
    with pytest.raises(ApplicationError) as caught:
        export(site, FakeCrud())
    assert caught.value.type == "browser"
    assert not caught.value.non_retryable
    texts = [str(e) for e in _chain(caught.value)] + [
        "".join(traceback.format_exception(caught.value)),
        repr(caught.value.details),
    ]
    for text in texts:
        assert EMAIL not in text
        assert PASSWORD not in text


@pytest.mark.parametrize(("kind", "launches"), [("browser", 2), ("timeout", 2), ("bot_check", 1)])
def test_a_browser_or_timeout_failure_opens_a_fresh_browser(kind: Kind, launches: int) -> None:
    """A crashed or wedged browser (``browser``, ``timeout``) is closed, and the next
    attempt launches and signs in again; a page wedged after the login would otherwise
    time out every later export (Task 6 review, M1). A bot check keeps the browser, and
    the check's cookie in it."""
    made: list[FakeSite] = []
    first = FakeSite(fail=SiteError(kind))

    def make() -> FakeSite:
        made.append(first if not made else FakeSite())
        return made[-1]

    crud = FakeCrud()
    client = CrudClient(
        "http://api", "profile-fetch", ServiceAuth(Source()), transport=httpx.MockTransport(crud)
    )
    sites = SiteHolder(make)
    activities = ProfileFetchActivities(client, sites)

    async def go() -> None:
        env = ActivityEnvironment()
        try:
            with pytest.raises(ApplicationError):
                await env.run(activities.export, ASK)
            first.fail = None
            await env.run(activities.export, ASK)
        finally:
            await sites.aclose()
            await client.aclose()

    asyncio.run(go())
    assert len(made) == launches
    assert made[0].closed == 1
    assert all((s.opened, s.closed, s.logins) == (1, 1, 1) for s in made)
    assert len(crud.puts) == 4


def test_a_cancelled_export_opens_a_fresh_browser() -> None:
    """An export Temporal cancels (a cancel request, or its start-to-close timeout, which
    the worker delivers as a cancellation) closes the browser as a ``timeout`` does, then
    ends cancelled: the page it left may be wedged mid-export."""
    made: list[FakeSite] = []
    first = FakeSite()
    entered = asyncio.Event()

    async def hang(tdfp_id: int, printer_model: str, nozzle: str) -> ExportedFile:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("never woken")

    first.export = hang  # type: ignore[method-assign]

    def make() -> FakeSite:
        made.append(first if not made else FakeSite())
        return made[-1]

    crud = FakeCrud()
    client = CrudClient(
        "http://api", "profile-fetch", ServiceAuth(Source()), transport=httpx.MockTransport(crud)
    )
    sites = SiteHolder(make)
    activities = ProfileFetchActivities(client, sites)

    async def go() -> None:
        env = ActivityEnvironment()
        try:
            task = asyncio.create_task(env.run(activities.export, ASK))
            await asyncio.wait_for(entered.wait(), 5)
            env.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert first.closed == 1
            await ActivityEnvironment().run(activities.export, ASK)
        finally:
            await sites.aclose()
            await client.aclose()

    asyncio.run(go())
    assert len(made) == 2
    assert (made[1].opened, made[1].logins) == (1, 1)
    assert len(crud.puts) == 4


@pytest.mark.parametrize(("status", "code"), [(409, "not_running"), (404, "run_not_found")])
def test_a_run_the_api_ended_is_not_running(status: int, code: str) -> None:
    """Decision 25: the API ended the run; the workflow ends without a retry."""
    crud = FakeCrud()
    crud.refuse = (status, code)
    with pytest.raises(ApplicationError) as caught:
        export(FakeSite(), crud)
    assert caught.value.type == NOT_RUNNING
    assert caught.value.non_retryable


def test_import_posts_the_failures_with_a_key() -> None:
    crud = FakeCrud()
    report = ExportReport(
        exported=3, failures=[ExportFailure(printer_model="H2C", nozzle="0.2", reason="r")]
    )
    outcome = run(FakeSite(), crud, lambda a: a.import_(RUN, report))
    assert outcome.status == "partial" and outcome.presets == 3
    [(body, key)] = crud.imports
    assert body == {"failures": [{"printer_model": "H2C", "nozzle": "0.2", "reason": "r"}]}
    assert key


def test_import_waits_out_the_routes_worst_case() -> None:
    """The import route does every Bambuddy call of the import in one request (about 12,
    each up to the Bambuddy client's own timeout): the request waits that long, not the
    CRUD client's 10 s, so a slow Bambuddy is not retried into a second import (R-F2)."""
    crud = FakeCrud()
    run(FakeSite(), crud, lambda a: a.import_(RUN, ExportReport(exported=3, failures=[])))
    [read] = crud.import_reads
    assert read is not None
    assert read >= 12 * BAMBUDDY_TIMEOUT
    assert read < IMPORT_TIMEOUT.total_seconds()


def test_import_of_an_ended_run_is_not_running() -> None:
    crud = FakeCrud()
    crud.refuse = (404, "run_not_found")
    with pytest.raises(ApplicationError) as caught:
        run(FakeSite(), crud, lambda a: a.import_(RUN, ExportReport(exported=0, failures=[])))
    assert caught.value.type == NOT_RUNNING


def test_failed_posts_the_reason() -> None:
    crud = FakeCrud()
    run(FakeSite(), crud, lambda a: a.failed(RUN, "r" * 600))
    assert crud.failed == [{"reason": "r" * 500}]


def test_failed_on_an_ended_run_does_nothing() -> None:
    crud = FakeCrud()
    crud.refuse = (409, "not_running")
    run(FakeSite(), crud, lambda a: a.failed(RUN, "gave up"))
