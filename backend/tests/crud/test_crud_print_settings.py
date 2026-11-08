"""The print worker's settings route (#5298 Task 23, plan D8): it answers the API's
settings with the Bambuddy key opened and every other sealed field null, only to the
print caller, never cached, and logs which secrets it released, never their values."""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.core.settings import SECRET_FIELDS, Settings
from scadbuddy.crud.client import CrudRefusedError, CrudSyncClient
from scadbuddy.crud.print import BASE, RemoteSettingsStore
from scadbuddy.library.settings_store import (
    PRINT_SECRETS,
    PRINT_UNREAD,
    S3_FIELDS,
    SettingsPatch,
    SettingsStore,
    StoredSettings,
    print_projection,
)
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import threaded_print_crud

FULL_KEY = "full-key-123456"
RENDER_KEY = "render-key-123456"
S3_SECRET = "s3-secret-123456"

#: The sealed settings the print worker never gets: each is null in its answer.
BLANKED = frozenset({"bambuddy_render_api_key", "store_s3_secret_access_key"})


def test_every_sealed_setting_is_released_or_blanked_by_name() -> None:
    """A new sealed field fails here until it is named in `PRINT_SECRETS` (the worker
    needs it) or in `BLANKED` (it does not); the projection leaves it null meanwhile."""
    sealed = SECRET_FIELDS & StoredSettings.model_fields.keys()
    assert sealed == PRINT_SECRETS | BLANKED
    assert not PRINT_SECRETS & BLANKED


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
        lan_require_plate_clear=True,
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
def crud(store: SettingsStore) -> Iterator[CrudSyncClient]:
    with threaded_print_crud(store) as client:
        yield client


def _save_secrets(store: SettingsStore) -> None:
    store.save(
        SettingsPatch(
            store_backend="s3",
            bambuddy_url="http://bambuddy.test",
            library_folder_id=7,
            bambuddy_api_key=FULL_KEY,
            bambuddy_render_api_key=RENDER_KEY,
            store_s3_endpoint="http://garage:3900",
            store_s3_bucket="sb",
            store_s3_access_key_id="GK1-123456",
            store_s3_secret_access_key=S3_SECRET,
        )
    )


@pytest.mark.requires_postgres
def test_the_bambuddy_key_is_opened_and_every_other_sealed_field_is_null(
    crud: CrudSyncClient, store: SettingsStore
) -> None:
    _save_secrets(store)
    current = RemoteSettingsStore(crud).load()
    assert current.bambuddy_api_key == FULL_KEY
    for name in SECRET_FIELDS & StoredSettings.model_fields.keys() - PRINT_SECRETS:
        assert getattr(current, name) is None, name
    # The S3 store's connection is not the print worker's: null, or the default where
    # the field takes no null; the backend flag stays.
    assert set(S3_FIELDS) <= set(PRINT_UNREAD) | SECRET_FIELDS
    defaults = StoredSettings()
    for name in PRINT_UNREAD:
        assert getattr(current, name) == getattr(defaults, name), name
    for name in ("store_s3_endpoint", "store_s3_bucket", "store_s3_access_key_id"):
        assert getattr(current, name) is None, name
    assert "GK1-123456" not in current.model_dump_json()
    # Everything else is the API's own read.
    assert current == print_projection(store.snapshot())
    assert (current.bambuddy_url, current.store_backend) == ("http://bambuddy.test", "s3")
    # A runtime field `StoredSettings` does not hold, which a native run reads (#4939).
    assert RemoteSettingsStore(crud).plate_clear_required() is True


@pytest.mark.requires_postgres
def test_the_answer_is_never_cached(crud: CrudSyncClient, store: SettingsStore) -> None:
    _save_secrets(store)
    response = crud.request("GET", f"{BASE}/settings")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-ScadBuddy-Crud"] == "1"
    for secret in (RENDER_KEY, S3_SECRET):
        assert secret not in response.text


@pytest.mark.requires_postgres
def test_the_log_names_the_secrets_released_never_their_values(
    crud: CrudSyncClient, store: SettingsStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="scadbuddy.api.crud_print")
    _save_secrets(store)
    # Read on every use: INFO once per credential, DEBUG after.
    crud.request("GET", f"{BASE}/settings")
    crud.request("GET", f"{BASE}/settings")
    released = [
        r.getMessage()
        for r in caplog.records
        if r.name == "scadbuddy.api.crud_print" and r.levelno == logging.INFO
    ]
    [credential] = [c for c in ServiceCredentialStore(store.pool).list() if c.caller == "print"]
    assert released == [
        f"released the print settings to service credential {credential.id},"
        " secrets: bambuddy_api_key"
    ]
    debug = [
        r
        for r in caplog.records
        if r.name == "scadbuddy.api.crud_print" and r.levelno == logging.DEBUG
    ]
    assert len(debug) == 1
    for secret in (FULL_KEY, RENDER_KEY, S3_SECRET):
        assert secret not in caplog.text


@pytest.mark.requires_postgres
def test_another_callers_credential_is_refused(store: SettingsStore) -> None:
    _save_secrets(store)
    with (
        threaded_print_crud(store, caller="render") as client,
        pytest.raises(CrudRefusedError) as refused,
    ):
        client.request("GET", f"{BASE}/settings")
    assert refused.value.status == 403
    assert refused.value.problem.get("reason") == "wrong_caller"


@pytest.mark.requires_postgres
def test_the_print_areas_list_settings_and_the_runs(crud: CrudSyncClient) -> None:
    response = crud.request("GET", "/api/v1/internal/crud/print/areas")
    assert response.json()["areas"] == [
        "settings",
        "runs",
        "operations",
        "events",
        "printer-ensure",
        "reprints",
        "uploads",
        "previews",
    ]
