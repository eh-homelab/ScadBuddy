"""The CRUD side of a customer file's check (#6133, spec 2026-11-13 §7): the intake worker's
routes, the check's start after a claim, and the backstop that starts a lost one. None of
it opens the file: the routes presign, and store the worker's preview as opaque bytes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.orders.storefront.intake import PREVIEW_HOLDER, preview_key
from scadbuddy.store.s3 import staging_key
from tests.orders.storefront.test_drafts import (  # noqa: F401  (fixtures)
    CrudCaller,
    _state,
    asks,
    s3_api,
)
from tests.orders.storefront.test_model_requests import (  # noqa: F401  (fixtures)
    STL,
    FakeChecker,
    FakeWatcher,
    ask,
    backend,
    checker,
    on_app,
    rows,
    staged,
    sweep,
    uploaded,
    watcher,
)
from tests.support.store import s3  # noqa: F401  (fixture)

pytestmark = pytest.mark.requires_postgres

INTAKE = "/api/v1/internal/crud/intake/sources"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SHA = "a" * 64
FACTS = {
    "format": "stl",
    "plates": [{"index": 1, "triangles": 12, "size_mm": [20.0, 10.0, 5.0], "colours": []}],
    "triangles": 12,
}


class Intake:
    """The intake worker's calls, with its credential."""

    def __init__(self, api: TestClient, caller: str = "intake") -> None:
        self.api = api
        pool = _state(api).settings_store.pool
        issued = ServiceCredentialStore(pool).issue(caller, "bootstrap", rate_limited=False)  # type: ignore[arg-type]
        self.bearer = {"Authorization": f"Bearer {issued.token}"}

    def read(self, source_id: str) -> Any:
        return self.api.post(f"{INTAKE}/{source_id}/read", headers=self.bearer)

    def preview(self, source_id: str, size: int) -> Any:
        return self.api.post(
            f"{INTAKE}/{source_id}/preview", json={"size": size}, headers=self.bearer
        )

    def check(self, source_id: str, body: dict[str, Any], key: str | None = "k1") -> Any:
        headers = {**self.bearer, **({"Idempotency-Key": key} if key else {})}
        return self.api.put(f"{INTAKE}/{source_id}/check", json=body, headers=headers)


@pytest.fixture
def worker(s3_api: TestClient) -> Intake:  # noqa: F811
    return Intake(s3_api)


def claimed(asks: CrudCaller) -> tuple[str, str]:  # noqa: F811
    [upload_id] = uploaded(asks, [("cube.stl", STL, "model/stl")])
    ask(asks, {"uploads": [upload_id]})
    [row] = rows(asks.api, "SELECT id FROM order_item_sources WHERE upload_id = %s", upload_id)
    return row["id"], upload_id


def put_preview(worker: Intake, source_id: str) -> None:
    slot = worker.preview(source_id, len(PNG))
    assert slot.status_code == 200, slot.text
    answer = httpx.put(slot.json()["url"], content=PNG, headers=slot.json()["headers"])
    answer.raise_for_status()


def test_a_claim_starts_its_files_checks(asks: CrudCaller, checker: FakeChecker) -> None:  # noqa: F811
    source_id, _ = claimed(asks)
    assert checker.started == [source_id]


def test_the_worker_reads_the_staged_file_through_a_presigned_get(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,
) -> None:
    source_id, _ = claimed(asks)
    r = worker.read(source_id)
    assert r.status_code == 200, r.text
    read = r.json()
    assert (read["filename"], read["size"], read["media_type"]) == (
        "cube.stl",
        len(STL),
        "model/stl",
    )
    assert httpx.get(read["url"]).content == STL


def test_a_ready_verdict_keeps_its_facts_and_its_preview(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
    worker: Intake,
    watcher: FakeWatcher,  # noqa: F811
) -> None:
    source_id, upload_id = claimed(asks)
    put_preview(worker, source_id)
    body = {"outcome": "ready", "sha256": SHA, "facts": FACTS}
    r = worker.check(source_id, body)
    assert r.status_code == 200, r.text
    assert r.json() == {"recorded": True}
    [source] = rows(
        s3_api,
        "SELECT check_status, check_reason, facts, sha256, preview_key FROM order_item_sources"
        " WHERE id = %s",
        source_id,
    )
    assert source == {
        "check_status": "ready",
        "check_reason": None,
        "facts": FACTS,
        "sha256": SHA,
        "preview_key": preview_key(SHA),
    }
    [hold] = rows(s3_api, "SELECT key, holder_kind FROM blob_refs WHERE holder_id = %s", source_id)
    assert hold == {"key": preview_key(SHA), "holder_kind": PREVIEW_HOLDER}
    store = backend(s3_api)
    key = staging_key(store.settings.prefix, upload_id, preview=True)
    assert on_app(s3_api, store.staged_size, key) is None  # the staged copy is gone
    assert staged(s3_api, upload_id) == len(STL)  # the file waits for its import
    assert (upload_id, "checked") in watcher.signals
    # The verdict sent again answers the first.
    assert worker.check(source_id, body).json() == {"recorded": True}


def test_a_refused_verdict_keeps_its_reason(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
    worker: Intake,
) -> None:
    source_id, _ = claimed(asks)
    body = {"outcome": "refused", "reason": "The file is not a 3MF or an STL."}
    assert worker.check(source_id, body).json() == {"recorded": True}
    [source] = rows(
        s3_api, "SELECT check_status, check_reason FROM order_item_sources WHERE id = %s", source_id
    )
    assert source == {"check_status": "refused", "check_reason": body["reason"]}


def test_a_ready_verdict_with_no_preview_is_refused(asks: CrudCaller, worker: Intake) -> None:  # noqa: F811
    source_id, _ = claimed(asks)
    r = worker.check(source_id, {"outcome": "ready", "sha256": SHA, "facts": FACTS})
    assert r.status_code == 409
    assert r.json()["type"].endswith("preview_missing")


@pytest.mark.parametrize(
    "body",
    [
        {"outcome": "ready", "facts": FACTS},
        {"outcome": "refused"},
        {"outcome": "failed", "facts": FACTS},
        {"outcome": "ready", "sha256": "nothex", "facts": FACTS},
    ],
)
def test_a_verdict_that_breaks_its_shape_is_422(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,
    body: dict[str, Any],
) -> None:
    source_id, _ = claimed(asks)
    assert worker.check(source_id, body).status_code == 422


def test_a_verdict_needs_its_key(asks: CrudCaller, worker: Intake) -> None:  # noqa: F811
    source_id, _ = claimed(asks)
    assert worker.check(source_id, {"outcome": "failed"}, key=None).status_code == 428


def test_a_source_that_stopped_waiting_records_nothing(
    asks: CrudCaller,  # noqa: F811
    worker: Intake,
) -> None:
    source_id, _ = claimed(asks)
    assert worker.check(source_id, {"outcome": "failed"}, key="k1").json() == {"recorded": True}
    assert worker.read(source_id).status_code == 409
    assert worker.preview(source_id, 10).status_code == 409
    again = worker.check(source_id, {"outcome": "refused", "reason": "x"}, key="k2")
    assert again.json() == {"recorded": False}
    assert worker.read("00000000-0000-4000-8000-000000000000").status_code == 404


def test_only_the_intake_credential_reaches_its_routes(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
) -> None:
    source_id, _ = claimed(asks)
    assert Intake(s3_api, "storefront").read(source_id).status_code == 403
    assert s3_api.post(f"{INTAKE}/{source_id}/read").status_code in (401, 403)


def test_a_removed_line_drops_its_previews_hold(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
    worker: Intake,
) -> None:
    source_id, _ = claimed(asks)
    put_preview(worker, source_id)
    worker.check(source_id, {"outcome": "ready", "sha256": SHA, "facts": FACTS})
    [item] = asks.get("/draft").json()["items"]
    assert asks.delete(f"/draft/items/{item['id']}", key="d1").status_code == 200
    assert rows(s3_api, "SELECT key FROM blob_refs WHERE holder_id = %s", source_id) == []


def test_the_backstop_starts_a_check_that_was_lost(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
    checker: FakeChecker,  # noqa: F811
) -> None:
    source_id, _ = claimed(asks)
    checker.stopped = {source_id}
    assert sweep(s3_api, datetime.now(UTC)) == 0  # not stale yet
    assert sweep(s3_api, datetime.now(UTC) + timedelta(hours=2)) == 1
    assert checker.started == [source_id, source_id]
    assert sweep(s3_api, datetime.now(UTC) + timedelta(hours=2)) == 0  # running now
