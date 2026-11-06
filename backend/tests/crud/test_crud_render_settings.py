"""The render worker's store settings route (#5298 Task 13, inventory R1, plan D8): it
answers what `load_render_store_settings` reads, secrets opened, only to the render
caller, never cached, and logs which secrets it released, never their values.
`RemoteRenderSettingsSource` keeps the SQL source's TTL and its last good settings."""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from scadbuddy.auth.service_credentials import (
    InProcessProvider,
    ServiceAuth,
    ServiceCredentialStore,
)
from scadbuddy.core.settings import Settings
from scadbuddy.crud import listener
from scadbuddy.crud.client import CrudClient, CrudUnavailableError
from scadbuddy.crud.guard import CRUD_HEADERS
from scadbuddy.crud.render import BASE, RemoteRenderSettingsSource
from scadbuddy.library.settings_store import (
    RenderStoreSettings,
    SettingsPatch,
    SettingsStore,
    load_render_store_settings,
)
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import FixedToken, render_crud_app, served_render_crud

pytestmark = pytest.mark.requires_postgres

RENDER_KEY = "render-key-123456"
FULL_KEY = "full-key-123456"
S3_ID = "GK1-123456"
S3_SECRET = "s3-secret-123456"


@pytest.fixture
def keyed(tmp_path: Path, pg_conninfo: str) -> Settings:
    key = tmp_path / "secret.key"
    key.write_text(base64.b64encode(b"\x5a" * 32).decode() + "\n")
    return Settings(
        data_dir=tmp_path,
        database_url=pg_conninfo,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        secret_key_file=key,
        auth_mode="enforce",
    )


@pytest.fixture
def store(keyed: Settings) -> Iterator[SettingsStore]:
    opened = SettingsStore(keyed)
    opened.open()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
async def crud(store: SettingsStore, keyed: Settings) -> AsyncIterator[CrudClient]:
    async with served_render_crud(store.pool, keyed, unavailable_budget=0.2) as client:
        yield client


def _save_secrets(store: SettingsStore, *, render_key: bool = True) -> None:
    store.save(
        SettingsPatch(
            store_backend="s3",
            bambuddy_url="http://bambuddy.test",
            library_folder_id=7,
            bambuddy_api_key=FULL_KEY,
            bambuddy_render_api_key=RENDER_KEY if render_key else None,
            store_s3_endpoint="http://garage:3900",
            store_s3_bucket="sb",
            store_s3_access_key_id=S3_ID,
            store_s3_secret_access_key=S3_SECRET,
        )
    )


async def test_the_answer_is_what_the_api_reads(
    crud: CrudClient, store: SettingsStore, keyed: Settings
) -> None:
    _save_secrets(store)
    current = await RemoteRenderSettingsSource(crud).current()
    assert current == load_render_store_settings(store.pool, keyed)
    # Opened, not sealed: the worker has no KEK to open them with.
    assert (current.store_backend, current.api_key, current.key_is_fallback) == (
        "s3",
        RENDER_KEY,
        False,
    )
    assert current.s3 is not None and current.s3.secret_access_key == S3_SECRET


async def test_the_answer_is_never_cached(crud: CrudClient, store: SettingsStore) -> None:
    _save_secrets(store)
    response = await crud.request("GET", f"{BASE}/settings")
    assert response.headers["Cache-Control"] == "no-store"


async def test_the_log_names_the_secrets_released_never_their_values(
    crud: CrudClient, store: SettingsStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="scadbuddy.api.crud_render")
    _save_secrets(store)
    await crud.request("GET", f"{BASE}/settings")
    released = [r.getMessage() for r in caplog.records if r.name == "scadbuddy.api.crud_render"]
    assert released == [
        "released the render settings to the render worker, secrets:"
        " bambuddy_render_api_key, store_s3_access_key_id, store_s3_secret_access_key"
    ]
    caplog.clear()
    # No render key saved: the full key is released, and the log says so.
    _save_secrets(store, render_key=False)
    await crud.request("GET", f"{BASE}/settings")
    assert "secrets: bambuddy_api_key," in caplog.text
    for secret in (RENDER_KEY, FULL_KEY, S3_ID, S3_SECRET):
        assert secret not in caplog.text


async def test_another_callers_credential_is_refused(store: SettingsStore, keyed: Settings) -> None:
    _save_secrets(store)
    credentials = ServiceCredentialStore(store.pool)
    app = render_crud_app(store.pool, keyed, credentials)
    await listener.start(app, 0)
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{listener.crud_listener_port(app)}",
            auth=ServiceAuth(InProcessProvider("print", lambda: credentials)),
        ) as http:
            response = await http.get(f"{BASE}/settings")
    finally:
        await listener.stop(app)
    assert response.status_code == 403
    assert RENDER_KEY not in response.text and S3_SECRET not in response.text


async def test_the_render_areas_list_settings(crud: CrudClient) -> None:
    response = await crud.request("GET", "/api/v1/internal/crud/render/areas")
    assert "settings" in response.json()["areas"]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _flaky(answers: list[RenderStoreSettings | None]) -> CrudClient:
    """A client of an API that answers ``answers`` in turn, a 503 for each None."""

    def answer(request: httpx.Request) -> httpx.Response:
        current = answers.pop(0)
        if current is None:
            return httpx.Response(503, headers=CRUD_HEADERS)
        return httpx.Response(200, json=current.model_dump(mode="json"), headers=CRUD_HEADERS)

    return CrudClient(
        "http://api",
        "render",
        ServiceAuth(FixedToken()),
        transport=httpx.MockTransport(answer),
        unavailable_budget=0.0,
    )


async def test_a_failed_re_read_keeps_the_last_good_settings() -> None:
    first = RenderStoreSettings(store_backend="bambuddy", api_key="one", library_folder_id=1)
    second = first.model_copy(update={"api_key": "two"})
    client = _flaky([first, None, second])
    clock = _Clock()
    source = RemoteRenderSettingsSource(client, clock=clock)
    try:
        assert source.ttl == 30.0
        assert await source.current() == first
        clock.now = 29.0
        # Inside the TTL: no request (a second would take the 503).
        assert await source.current() == first and source.fresh
        clock.now = 30.0
        assert await source.current() == first
        assert not source.fresh
        clock.now = 60.0
        assert await source.current() == second
        assert source.fresh
    finally:
        await client.aclose()


async def test_a_failed_first_read_raises() -> None:
    client = _flaky([None])
    try:
        with pytest.raises(CrudUnavailableError):
            await RemoteRenderSettingsSource(client).current()
    finally:
        await client.aclose()
