from __future__ import annotations

import asyncio
from contextlib import ExitStack

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from temporalio.client import Client

from scadbuddy.api.deps import IMPORT_CONCURRENCY, STATE_ATTR, ImportPermits
from scadbuddy.api.models import MAX_SOURCE_CHARS, RESOLVER_RETRY_AFTER
from scadbuddy.core.paths import DataPaths
from scadbuddy.library import url_import
from scadbuddy.library.url_import import resolve_host as real_resolve_host
from scadbuddy.library.url_import import shown_url
from scadbuddy.operations.component import OPERATIONS
from scadbuddy.workflows.operation_models import FINISH_ACTIVITY

RAW_URL = "https://raw.githubusercontent.com/someone/models/main/Gridfinity%20Bin.scad"
SOURCE = "width = 10;\ncube(width);\n"

# respx only intercepts real transports, so the TestClient's own requests to the app
# pass straight through while the app's outbound fetch is mocked.
pytestmark = pytest.mark.usefixtures("fake_dns")


@respx.mock
def test_a_raw_url_imports_through_the_create_path(client: TestClient, paths: DataPaths) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text=SOURCE))

    response = client.post("/api/v1/models/import", json={"url": RAW_URL})

    assert response.status_code == 201
    body = response.json()
    assert (body["slug"], body["name"]) == ("gridfinity-bin", "Gridfinity Bin")
    assert body["origin_url"] == RAW_URL
    assert client.get("/api/v1/models/gridfinity-bin/source").text == SOURCE
    # The same schema the create path stores, so the customizer opens without a rerun.
    assert paths.model_schema_cache("gridfinity-bin").exists()
    assert client.get("/api/v1/models/gridfinity-bin").json()["origin_url"] == RAW_URL


@respx.mock
def test_a_name_overrides_the_one_taken_from_the_url(client: TestClient) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text=SOURCE))

    response = client.post("/api/v1/models/import", json={"url": RAW_URL, "name": "My Bin"})

    assert response.status_code == 201
    assert response.json()["slug"] == "my-bin"


@respx.mock
def test_a_name_too_long_for_a_slug_is_refused_and_nothing_saved(client: TestClient) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text=SOURCE))

    response = client.post("/api/v1/models/import", json={"url": RAW_URL, "name": "x" * 300})

    assert response.status_code == 422
    assert "longer than" in response.json()["detail"]
    assert client.get("/api/v1/models").json() == []


@respx.mock
def test_an_import_over_an_existing_slug_conflicts(client: TestClient) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text=SOURCE))
    assert client.post("/api/v1/models/import", json={"url": RAW_URL}).status_code == 201

    response = client.post("/api/v1/models/import", json={"url": RAW_URL})

    assert response.status_code == 409


@respx.mock
def test_source_openscad_cannot_parse_is_refused_and_nothing_is_saved(client: TestClient) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text="%%FAIL%%\n"))

    response = client.post("/api/v1/models/import", json={"url": RAW_URL})

    assert response.status_code == 422
    assert response.json()["log_tail"] == ["ERROR: Parser error: syntax error"]
    assert client.get("/api/v1/models").json() == []


@respx.mock
def test_force_saves_an_import_that_does_not_parse(client: TestClient) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text="%%FAIL%%\n"))

    response = client.post("/api/v1/models/import", json={"url": RAW_URL, "force": True})

    assert response.status_code == 201


def test_a_plain_http_url_is_a_problem_422(client: TestClient) -> None:
    response = client.post("/api/v1/models/import", json={"url": "http://example.com/model.scad"})

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert "https" in response.json()["detail"]


def test_a_makerworld_url_is_a_problem_422_that_says_what_to_do(client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        response = client.post(
            "/api/v1/models/import", json={"url": "https://makerworld.com/en/models/1398039"}
        )

    assert response.status_code == 422
    assert "MakerWorld" in response.json()["detail"]
    assert not mock.calls
    assert client.get("/api/v1/models").json() == []


@respx.mock
def test_a_public_server_error_status_is_a_422_that_names_it(client: TestClient) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(404))

    response = client.post("/api/v1/models/import", json={"url": RAW_URL})

    assert response.status_code == 422
    assert "404" in response.json()["detail"]


def test_a_cluster_address_reads_exactly_like_one_that_did_not_answer(
    client: TestClient, fake_dns: dict[str, list[str]]
) -> None:
    fake_dns["bambuddy.bambuddy.svc.cluster.local"] = ["10.43.0.12"]
    fake_dns["slow.example.com"] = ["93.184.215.15"]

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get("https://slow.example.com/x").mock(
            side_effect=httpx.ConnectTimeout("slow")
        )
        internal = client.post(
            "/api/v1/models/import", json={"url": "https://bambuddy.bambuddy.svc.cluster.local/x"}
        )
        slow = client.post("/api/v1/models/import", json={"url": "https://slow.example.com/x"})

    assert internal.status_code == slow.status_code == 422
    assert internal.json()["detail"] == slow.json()["detail"].replace(
        "slow.example.com", "bambuddy.bambuddy.svc.cluster.local"
    )
    # The internal name has no route, so reaching respx at all would have raised.
    assert route.call_count == 1
    assert client.get("/api/v1/models").json() == []


@respx.mock
def test_a_source_longer_than_a_paste_may_be_is_refused(client: TestClient) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text="x" * (MAX_SOURCE_CHARS + 1)))

    response = client.post("/api/v1/models/import", json={"url": RAW_URL})

    assert response.status_code == 422
    assert str(MAX_SOURCE_CHARS) in response.json()["detail"]


def test_a_model_created_any_other_way_has_no_origin(client: TestClient) -> None:
    response = client.post("/api/v1/models", json={"name": "Pasted", "source": SOURCE})

    assert response.json()["origin_url"] is None


@respx.mock
def test_an_import_never_targets_a_built_in(client: TestClient) -> None:
    """The name is slugified like any create's, so `builtin:` cannot survive into the id."""
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text=SOURCE))

    response = client.post("/api/v1/models/import", json={"url": RAW_URL, "name": "builtin:bin"})

    assert response.status_code == 201
    assert (response.json()["slug"], response.json()["origin"]) == ("builtin-bin", "mine")


def test_the_fetch_budget_is_the_resolver_s_threads() -> None:
    assert IMPORT_CONCURRENCY == url_import.RESOLVER_THREADS


def test_an_import_over_the_fetch_budget_is_a_503_with_retry_after(client: TestClient) -> None:
    """Retry-After is when the oldest held fetch must end (#631), not a flat guess."""
    imports: ImportPermits = getattr(client.app.state, STATE_ATTR).imports  # type: ignore[attr-defined]
    with ExitStack() as held:
        for _ in range(IMPORT_CONCURRENCY):
            held.enter_context(imports.hold())
        oldest = next(iter(imports._taken))
        imports._taken[oldest] -= url_import.IMPORT_TIMEOUT - 10
        with respx.mock(assert_all_called=False) as mock:
            response = client.post("/api/v1/models/import", json={"url": RAW_URL})

    assert response.status_code == 503
    # About 10 s left on the oldest fetch, less however long the request took.
    retry_after = response.json()["retry_after"]
    assert 5 <= retry_after <= 10
    assert response.headers["retry-after"] == str(retry_after)
    assert not mock.calls
    assert client.get("/api/v1/models").json() == []


def test_an_import_with_every_resolver_thread_busy_is_a_503_not_the_refusal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Library installs share the import's resolver threads: with none free, the
    import is told to retry, not that the host is not public."""
    monkeypatch.setattr(url_import, "resolve_host", real_resolve_host)
    taken = 0
    while url_import._RESOLVER_SLOTS.acquire(blocking=False):
        taken += 1
    try:
        with respx.mock(assert_all_called=False) as mock:
            response = client.post("/api/v1/models/import", json={"url": RAW_URL})
    finally:
        url_import._RESOLVER_SLOTS.release(taken)

    assert response.status_code == 503
    assert response.headers["retry-after"] == str(RESOLVER_RETRY_AFTER)
    assert "resolver" in response.json()["detail"]
    assert not mock.calls
    assert client.get("/api/v1/models").json() == []


@respx.mock
def test_every_import_gives_its_fetch_permit_back(client: TestClient) -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(404))
    for _ in range(IMPORT_CONCURRENCY + 1):
        assert client.post("/api/v1/models/import", json={"url": RAW_URL}).status_code == 422

    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text=SOURCE))
    assert client.post("/api/v1/models/import", json={"url": RAW_URL}).status_code == 201


def test_import_retry_after_counts_down_from_the_oldest_held_fetch() -> None:
    permits = ImportPermits(2)
    with permits.hold(), permits.hold():
        assert permits.full()
        assert permits.retry_after() == url_import.IMPORT_TIMEOUT
        second = list(permits._taken)[1]
        permits._taken[second] -= 20
        assert permits.retry_after() == url_import.IMPORT_TIMEOUT - 20
        # A fetch past its deadline is about to give its permit back.
        permits._taken[second] -= 60
        assert permits.retry_after() == 1
    assert not permits.full()
    assert permits.retry_after() == 1


@respx.mock
def test_an_imports_operation_names_the_host_never_the_url(client: TestClient) -> None:
    """Review 3c M6: the subject is a search attribute, shown in the Temporal UI; a URL
    may carry a token, and past 2 KB it would fail the workflow task."""
    url = RAW_URL + "?token=secret"
    respx.get(url).mock(return_value=httpx.Response(200, text=SOURCE))
    assert client.post("/api/v1/models/import", json={"url": url}).status_code == 201
    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    with state.components.get(OPERATIONS).store._require().connection() as conn:
        rows = conn.execute("SELECT subject FROM operations WHERE kind = 'model_import'").fetchall()
    assert [row["subject"] for row in rows] == ["raw.githubusercontent.com"]


@respx.mock
def test_an_import_never_records_the_urls_query(client: TestClient, paths: DataPaths) -> None:
    """Review 3c 1.5: a URL's query may carry a token. The URL goes by claim, and the
    model records it without its query or fragment, so neither the operation's record
    nor its history, nor the model.json, ever holds the token."""
    url = RAW_URL + "?token=secret#secret"
    respx.get(RAW_URL + "?token=secret").mock(return_value=httpx.Response(200, text=SOURCE))
    created = client.post("/api/v1/models/import", json={"url": url})
    assert created.status_code == 201, created.text
    assert created.json()["origin_url"] == RAW_URL
    assert "secret" not in paths.model_meta("gridfinity-bin").read_text("utf-8")
    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    with state.components.get(OPERATIONS).store._require().connection() as conn:
        (row,) = conn.execute(
            "SELECT request::text AS request, result::text AS result, workflow_id"
            " FROM operations WHERE kind = 'model_import'"
        ).fetchall()
    assert "secret" not in row["request"] and "secret" not in row["result"]
    assert RAW_URL in row["request"]

    async def history() -> str:
        client = await Client.connect(state.settings.temporal_address, namespace="default")
        fetched = await client.get_workflow_handle(row["workflow_id"]).fetch_history()
        finishes = [
            event
            for event in fetched.events
            if event.activity_task_scheduled_event_attributes.activity_type.name == FINISH_ACTIVITY
        ]
        assert finishes, "the history has its finish input"
        return fetched.to_json()

    assert "secret" not in asyncio.run(history())


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("https://u:p@example.com:8443/a/b.scad?t=1#f", "https://example.com:8443/a/b.scad"),
        ("https://example.com/b.scad", "https://example.com/b.scad"),
        ("https://[::1]:8443/b.scad?x", "https://[::1]:8443/b.scad"),
    ],
)
def test_a_shown_url_keeps_scheme_host_port_and_path(url: str, shown: str) -> None:
    assert shown_url(url) == shown


def _import_records(client: TestClient) -> list[str]:
    """Every ``model_import`` operation's row and its workflow's history, as text."""
    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    with state.components.get(OPERATIONS).store._require().connection() as conn:
        rows = conn.execute(
            "SELECT row_to_json(operations)::text AS row, workflow_id"
            " FROM operations WHERE kind = 'model_import'"
        ).fetchall()

    async def histories() -> list[str]:
        temporal = await Client.connect(state.settings.temporal_address, namespace="default")
        return [
            (await temporal.get_workflow_handle(row["workflow_id"]).fetch_history()).to_json()
            for row in rows
        ]

    return [row["row"] for row in rows] + (asyncio.run(histories()) if rows else [])


@pytest.mark.parametrize(
    ("url", "detail"),
    [
        ("http://example.com/model.scad?token=secret", "only https URLs can be imported"),
        ("https://[nope/model.scad?token=secret", "is not a URL"),
        ("https:///model.scad?token=secret", "names no host"),
    ],
)
def test_a_url_refused_on_its_shape_starts_no_operation(
    client: TestClient, url: str, detail: str
) -> None:
    """#1054: these refusals quote the URL, query and all. Made in the route, before
    any operation, they are recorded nowhere."""
    with respx.mock(assert_all_called=False) as mock:
        response = client.post("/api/v1/models/import", json={"url": url})

    assert response.status_code == 422, response.text
    assert detail in response.json()["detail"]
    assert not mock.calls
    assert _import_records(client) == []


@respx.mock
def test_a_refused_redirect_records_no_query(client: TestClient) -> None:
    """#1054: a refusal the run makes is recorded in the operation's error and the
    history, so it quotes the hop without its query."""
    respx.get(RAW_URL).mock(
        return_value=httpx.Response(
            302, headers={"Location": "http://example.com/model.scad?token=secret"}
        )
    )

    response = client.post("/api/v1/models/import", json={"url": RAW_URL})

    assert response.status_code == 422, response.text
    assert "http://example.com/model.scad" in response.json()["detail"]
    records = _import_records(client)
    assert records and all("secret" not in record for record in records)


def test_a_host_that_is_not_public_records_no_query(
    client: TestClient, fake_dns: dict[str, list[str]]
) -> None:
    fake_dns["internal.example.com"] = ["10.43.0.12"]

    response = client.post(
        "/api/v1/models/import", json={"url": "https://internal.example.com/m.scad?token=secret"}
    )

    assert response.status_code == 422, response.text
    records = _import_records(client)
    assert records and all("secret" not in record for record in records)
