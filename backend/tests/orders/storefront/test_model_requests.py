"""A storefront customer's own models on the CRUD side (#6133, spec 2026-11-13 §5, §6):
``POST …/storefront/uploads`` answers a presigned PUT per file into the store's staging
area, ``POST …/storefront/draft/models`` claims them after a ``HEAD`` each and adds a
request item with its files and links, and the storefront drafts sweep deletes what is
never claimed."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from scadbuddy.orders.storefront import intake, staged_files
from scadbuddy.orders.storefront.model_requests import EXPIRED, STAGING_TTL, UNCLAIMED_GRACE
from scadbuddy.orders.storefront.staged_files import WATCH_UNAVAILABLE, sweep_staged
from scadbuddy.store.s3 import S3ContentBackend, staging_key
from scadbuddy.storefront.wire import MAX_MODEL_FILE_BYTES, STOREFRONT_PROBLEMS
from scadbuddy.workflows.staged_file import StagedFileState
from tests.orders.storefront.test_drafts import (  # noqa: F401  (fixtures)
    TOKEN_A,
    TOKEN_B,
    CrudCaller,
    _orders,
    _state,
    api,
    asks,
    crud,
    requests_off,
    s3_api,
)
from tests.support.store import s3  # noqa: F401  (fixture)

pytestmark = pytest.mark.requires_postgres

STL = b"solid cube\nendsolid cube\n"
THREE_MF = b"PK\x03\x04" + b"\x00" * 60


def slots(
    caller: CrudCaller,
    files: list[tuple[str, bytes, str]],
    *,
    key: str = "u1",
    token: str = TOKEN_A,
) -> Any:
    body = {"files": [{"filename": n, "size": len(b), "media_type": t} for n, b, t in files]}
    return caller.post("/uploads", json=body, key=key, token=token)


def put(slot: dict[str, Any], data: bytes) -> None:
    answer = httpx.put(slot["url"], content=data, headers=slot["headers"])
    answer.raise_for_status()


def uploaded(
    caller: CrudCaller,
    files: list[tuple[str, bytes, str]],
    *,
    key: str = "u1",
    token: str = TOKEN_A,
) -> list[str]:
    r = slots(caller, files, key=key, token=token)
    assert r.status_code == 200, r.text
    answered = r.json()["uploads"]
    for slot, (_, data, _) in zip(answered, files, strict=True):
        put(slot, data)
    return [slot["id"] for slot in answered]


def ask(caller: CrudCaller, body: dict[str, Any], *, key: str = "m1", token: str = TOKEN_A) -> Any:
    return caller.post("/draft/models", json=body, key=key, token=token)


def backend(app: TestClient) -> S3ContentBackend:
    content = _state(app).store.content
    assert content is not None and isinstance(content.backend, S3ContentBackend)
    return content.backend


def on_app[T](app: TestClient, call: Callable[..., Awaitable[T]], *args: Any) -> T:
    """``call`` run on the app's own loop: its S3 client is bound to that loop."""
    assert app.portal is not None
    done: T = app.portal.call(call, *args)
    return done


def rows(app: TestClient, sql: str, *args: Any) -> list[dict[str, Any]]:
    with _state(app).projection.pool.connection() as conn:
        return list(conn.execute(sql, args).fetchall())


def test_a_slot_is_a_short_signed_put_of_one_staging_key(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
) -> None:
    r = slots(asks, [("cube.stl", STL, "model/stl")])
    assert r.status_code == 200, r.text
    [slot] = r.json()["uploads"]
    assert slot["filename"] == "cube.stl"
    assert slot["headers"] == {"Content-Type": "model/stl", "x-amz-acl": "private"}
    assert f"/staging/{slot['id']}?" in slot["url"]
    assert "X-Amz-SignedHeaders=content-length%3Bcontent-type%3Bhost" in slot["url"]
    assert "X-Amz-Expires=900" in slot["url"]
    [row] = rows(s3_api, "SELECT claimed_at, size FROM storefront_uploads")
    assert row == {"claimed_at": None, "size": len(STL)}


@pytest.mark.parametrize(
    ("files", "slug"),
    [
        ([("cube.stl", STL, "model/3mf")], "upload_name"),
        ([("cube.exe", STL, "model/stl")], "upload_name"),
    ],
)
def test_a_slot_must_be_named_as_its_type(asks: CrudCaller, files: Any, slug: str) -> None:  # noqa: F811
    r = slots(asks, files)
    assert r.status_code == 422, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}{slug}"


def test_slots_are_capped_in_all(asks: CrudCaller) -> None:  # noqa: F811
    body = {
        "files": [
            {"filename": f"{n}.stl", "size": MAX_MODEL_FILE_BYTES, "media_type": "model/stl"}
            for n in range(5)
        ]
    }
    r = asks.post("/uploads", json=body, key="u9")
    assert r.status_code == 422, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}upload_total"


@pytest.mark.parametrize(
    "body",
    [
        {"files": []},
        {
            "files": [
                {"filename": "a.stl", "size": MAX_MODEL_FILE_BYTES + 1, "media_type": "model/stl"}
            ]
        },
        {"files": [{"filename": "a.stl", "size": 0, "media_type": "model/stl"}]},
        {"files": [{"filename": "a.obj", "size": 3, "media_type": "model/obj"}]},
        {
            "files": [
                {"filename": f"{n}.stl", "size": 1, "media_type": "model/stl"} for n in range(21)
            ]
        },
    ],
)
def test_a_body_that_is_not_slots_is_refused(asks: CrudCaller, body: Any) -> None:  # noqa: F811
    r = asks.post("/uploads", json=body, key="u8")
    assert r.status_code == 422, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}draft_body_invalid"


def test_a_resent_presign_answers_the_same_slots(asks: CrudCaller, s3_api: TestClient) -> None:  # noqa: F811
    first = slots(asks, [("cube.stl", STL, "model/stl")]).json()["uploads"]
    again = slots(asks, [("cube.stl", STL, "model/stl")]).json()["uploads"]
    assert [s["id"] for s in again] == [s["id"] for s in first]
    assert len(rows(s3_api, "SELECT id FROM storefront_uploads")) == 1


def test_a_request_claims_its_files_and_keeps_its_links_as_text(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
) -> None:
    ids = uploaded(asks, [("cube.stl", STL, "model/stl"), ("box.3mf", THREE_MF, "model/3mf")])
    r = ask(
        asks,
        {
            "uploads": ids,
            "links": [
                {"url": "https://makerworld.com/en/models/123456-frog"},
                {"url": "https://www.printables.com/model/98765-stand"},
                {"url": "https://example.com/<script>", "note": "a link"},
                {"url": "https://shop.example/magnets", "purpose": "purchase", "note": "20 x 6 mm"},
            ],
            "quantity": 2,
            "note": "Two of each, please.",
        },
    )
    assert r.status_code == 200, r.text
    [item] = r.json()["items"]
    assert (item["kind"], item["title"], item["quantity"], item["notes"], item["own_model"]) == (
        "request",
        "Your models",
        2,
        "Two of each, please.",
        True,
    )
    shown = [
        (s["kind"], s["purpose"], s["filename"], s["url"], s["check_status"], s["status"])
        for s in item["sources"]
    ]
    assert shown == [
        ("file", "print", "cube.stl", None, "checking", "open"),
        ("file", "print", "box.3mf", None, "checking", "open"),
        ("link", "print", None, "https://makerworld.com/en/models/123456-frog", None, "open"),
        ("link", "print", None, "https://www.printables.com/model/98765-stand", None, "manual"),
        ("link", "print", None, "https://example.com/<script>", None, "manual"),
        ("link", "purchase", None, "https://shop.example/magnets", None, "pending"),
    ]
    assert item["sources"][5]["note"] == "20 x 6 mm"
    types = rows(
        s3_api,
        "SELECT link_type, canonical FROM order_item_sources WHERE kind = 'link' ORDER BY position",
    )
    assert [(t["link_type"], t["canonical"]) for t in types] == [
        ("makerworld", "123456"),
        ("printables", "98765"),
        ("manual", None),
        ("manual", None),
    ]
    claimed = rows(s3_api, "SELECT claimed_at FROM storefront_uploads")
    assert all(row["claimed_at"] is not None for row in claimed)
    [prefs] = rows(s3_api, "SELECT colour_preferences FROM order_item_model_requests")
    assert prefs["colour_preferences"] == {"shop_choice": True}


def test_a_resent_request_adds_one_item(asks: CrudCaller) -> None:  # noqa: F811
    ids = uploaded(asks, [("cube.stl", STL, "model/stl")])
    body = {"uploads": ids}
    first = ask(asks, body)
    again = ask(asks, body)
    assert first.status_code == again.status_code == 200, again.text
    assert len(again.json()["items"]) == 1


def test_a_file_that_never_arrived_is_refused(asks: CrudCaller) -> None:  # noqa: F811
    r = slots(asks, [("cube.stl", STL, "model/stl")])
    [slot] = r.json()["uploads"]
    r = ask(asks, {"uploads": [slot["id"]]})
    assert r.status_code == 422, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}upload_missing"
    assert r.json()["index"] == 0
    assert asks.get("/draft").json() is None


def test_a_file_of_another_size_is_refused(asks: CrudCaller, s3_api: TestClient) -> None:  # noqa: F811
    [slot] = slots(asks, [("cube.stl", STL, "model/stl")]).json()["uploads"]
    store = backend(s3_api)

    async def short() -> None:
        client = await store.client()
        key = staging_key(store.settings.prefix, slot["id"])
        await client.put_object(Bucket=store.settings.bucket, Key=key, Body=STL[:-1])

    on_app(s3_api, short)
    r = ask(asks, {"uploads": [slot["id"]]})
    assert r.status_code == 422, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}upload_size"


def test_another_visitors_file_is_not_found(asks: CrudCaller) -> None:  # noqa: F811
    ids = uploaded(asks, [("cube.stl", STL, "model/stl")], token=TOKEN_B)
    r = ask(asks, {"uploads": ids})
    assert r.status_code == 422, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}upload_missing"


def test_a_file_is_claimed_once(asks: CrudCaller) -> None:  # noqa: F811
    ids = uploaded(asks, [("cube.stl", STL, "model/stl")])
    assert ask(asks, {"uploads": ids}).status_code == 200
    r = ask(asks, {"uploads": ids}, key="m2")
    assert r.status_code == 422, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}upload_missing"


@pytest.mark.parametrize(
    ("body", "slug"),
    [
        ({}, "model_request_empty"),
        (
            {"links": [{"url": "https://shop.example/m", "purpose": "purchase"}]},
            "model_request_empty",
        ),
        ({"links": [{"url": "javascript:alert(1)"}]}, "link_invalid"),
        ({"links": [{"url": "https://u:p@example.com/"}]}, "link_invalid"),
        (
            {
                "links": [{"url": "https://example.com/m"}]
                + [{"url": f"https://shop.example/{n}", "purpose": "purchase"} for n in range(6)]
            },
            "too_many_links",
        ),
        (
            {"links": [{"url": f"https://example.com/{n}"} for n in range(11)]},
            "too_many_links",
        ),
        ({"links": [{"url": "https://example.com/m"}], "note": "a\x00b"}, "request_text"),
        (
            {"links": [{"url": "https://example.com/m"}], "colours": {"shop_choice": False}},
            "colour_missing",
        ),
        (
            {
                "links": [{"url": "https://example.com/m"}],
                "colours": {"shop_choice": False, "filaments": ["nope"]},
            },
            "colour_unknown",
        ),
        ({"uploads": ["not-an-id"]}, "upload_missing"),
    ],
)
def test_a_request_that_breaks_a_rule_is_refused(
    asks: CrudCaller,  # noqa: F811
    body: dict[str, Any],
    slug: str,
) -> None:
    r = ask(asks, body)
    assert r.status_code == 422, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}{slug}"
    assert asks.get("/draft").json() is None


def test_model_requests_are_off_with_design_requests(requests_off: CrudCaller) -> None:  # noqa: F811
    sent: list[tuple[str, dict[str, Any]]] = [("/uploads", {"files": []}), ("/draft/models", {})]
    for path, body in sent:
        r = requests_off.post(path, json=body, key="off")
        assert r.status_code == 404, r.text
        assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}requests_disabled"


def test_uploads_need_the_s3_store(crud: CrudCaller, api: TestClient) -> None:  # noqa: F811
    on = api.put("/api/v1/settings", json={"storefront_requests_enabled": True})
    assert on.status_code == 200, on.text
    r = slots(crud, [("cube.stl", STL, "model/stl")])
    assert r.status_code == 503, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}storefront_needs_s3"
    assert crud.get("/uploads/config").status_code == 503


def test_the_upload_config_names_the_puts_origin(asks: CrudCaller, s3_api: TestClient) -> None:  # noqa: F811
    origin = asks.get("/uploads/config").json()["origin"]
    assert origin == backend(s3_api).settings.endpoint


# -- each file's watcher (#6133, spec §6.5) ----------------------------------------------


class FakeWatcher:
    """What the routes start and signal, recorded; ``refuse`` makes every start fail, and
    ``stopped`` names the uploads whose watcher reads as not running."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.signals: list[tuple[str, str]] = []
        self.refuse = False
        self.stopped: set[str] = set()

    async def watch(self, upload_ids: Sequence[str]) -> None:
        if self.refuse:
            raise RuntimeError("Temporal is down")
        self.started.extend(upload_ids)
        self.stopped -= set(upload_ids)

    async def poke(self, upload_ids: Sequence[str], signal: str) -> None:
        self.signals.extend((upload_id, signal) for upload_id in upload_ids)

    async def running(self, upload_id: str) -> bool | None:
        return upload_id not in self.stopped


@pytest.fixture(autouse=True)
def watcher(monkeypatch: pytest.MonkeyPatch) -> FakeWatcher:
    fake = FakeWatcher()
    monkeypatch.setattr(staged_files, "watcher", lambda _state: fake)
    return fake


class FakeChecker:
    """The files' checks the claim and the backstop start, recorded (#6133 PR 2);
    ``stopped`` names the sources whose check reads as not running."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: set[str] = set()

    async def start(self, source_ids: Sequence[str]) -> None:
        self.started.extend(source_ids)
        self.stopped -= set(source_ids)

    async def running(self, source_id: str) -> bool | None:
        return source_id not in self.stopped


@pytest.fixture(autouse=True)
def checker(monkeypatch: pytest.MonkeyPatch) -> FakeChecker:
    fake = FakeChecker()
    monkeypatch.setattr(intake, "checker", lambda _state: fake)
    return fake


def staged(app: TestClient, slot_id: str) -> int | None:
    store = backend(app)
    return on_app(app, store.staged_size, staging_key(store.settings.prefix, slot_id))


def step(app: TestClient, slot_id: str, now: datetime) -> StagedFileState:
    return on_app(app, lambda: staged_files.step(_state(app), slot_id, now=now))


def sweep(app: TestClient, now: datetime) -> int:
    return on_app(app, lambda: sweep_staged(_state(app), now=now))


def submitted(asks: CrudCaller, order_id: str) -> None:  # noqa: F811
    body = {"draft_id": order_id, "name": "Ada", "email": "ada@example.com"}
    r = asks.post("/draft/submit", json=body, key="submit-1")
    assert r.status_code == 200, r.text


def test_every_slot_has_a_watcher_before_its_url_is_answered(
    asks: CrudCaller,  # noqa: F811
    watcher: FakeWatcher,
) -> None:
    files = [("a.stl", STL, "model/stl"), ("b.3mf", THREE_MF, "model/3mf")]
    r = slots(asks, files)
    assert r.status_code == 200, r.text
    ids = [slot["id"] for slot in r.json()["uploads"]]
    assert watcher.started == ids
    # A presign sent again re-signs the same slots and joins their watchers.
    again = slots(asks, files)
    assert again.status_code == 200, again.text
    assert watcher.started == ids * 2


def test_no_url_is_answered_when_no_watcher_can_start(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
    watcher: FakeWatcher,
) -> None:
    watcher.refuse = True
    r = slots(asks, [("cube.stl", STL, "model/stl")])
    assert r.status_code == 503, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}{WATCH_UNAVAILABLE}"
    assert "uploads" not in r.json()
    assert rows(s3_api, "SELECT id FROM storefront_uploads") == []
    # The retry the 503 asks for, with the same key, makes its slots afresh.
    watcher.refuse = False
    again = slots(asks, [("cube.stl", STL, "model/stl")])
    assert again.status_code == 200, again.text
    [slot] = again.json()["uploads"]
    assert watcher.started == [slot["id"]]
    assert [r["id"] for r in rows(s3_api, "SELECT id FROM storefront_uploads")] == [slot["id"]]


def test_a_slot_never_claimed_is_cleaned_up_an_hour_past_its_url(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
) -> None:
    [slot_id] = uploaded(asks, [("cube.stl", STL, "model/stl")])
    [row] = rows(s3_api, "SELECT expires_at FROM storefront_uploads")
    found = step(s3_api, slot_id, datetime.now(UTC))
    assert (found.phase, found.due) == ("unclaimed", row["expires_at"] + UNCLAIMED_GRACE)
    assert staged(s3_api, slot_id) == len(STL)
    found = step(s3_api, slot_id, row["expires_at"] + UNCLAIMED_GRACE)
    assert (found.phase, found.reason) == ("ended", "unclaimed")
    assert staged(s3_api, slot_id) is None
    assert rows(s3_api, "SELECT id FROM storefront_uploads") == []


def test_a_claim_tells_the_watcher_and_moves_the_deadline_to_the_claim(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
    watcher: FakeWatcher,
) -> None:
    [slot_id] = uploaded(asks, [("cube.stl", STL, "model/stl")])
    ask(asks, {"uploads": [slot_id]})
    assert watcher.signals == [(slot_id, "claimed")]
    [row] = rows(s3_api, "SELECT claimed_at FROM storefront_uploads")
    # Past the slot's own expiry: a claimed file waits for its cart.
    found = step(s3_api, slot_id, datetime.now(UTC) + timedelta(hours=2))
    assert (found.phase, found.due) == ("claimed", row["claimed_at"] + STAGING_TTL)


def test_a_removed_line_tells_the_watcher_and_frees_its_file(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
    watcher: FakeWatcher,
) -> None:
    [slot_id] = uploaded(asks, [("cube.stl", STL, "model/stl")])
    item = ask(asks, {"uploads": [slot_id]}).json()["items"][0]
    assert asks.delete(f"/draft/items/{item['id']}", key="d1").status_code == 200
    assert (slot_id, "unclaimed") in watcher.signals
    found = step(s3_api, slot_id, datetime.now(UTC))
    assert (found.phase, found.reason) == ("ended", "removed")
    assert staged(s3_api, slot_id) is None


def test_a_file_of_a_cart_not_sent_expires_72_hours_after_its_claim(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
) -> None:
    [slot_id] = uploaded(asks, [("cube.stl", STL, "model/stl")])
    ask(asks, {"uploads": [slot_id]})
    [row] = rows(s3_api, "SELECT claimed_at FROM storefront_uploads")
    assert step(s3_api, slot_id, row["claimed_at"] + timedelta(hours=71)).phase == "claimed"
    found = step(s3_api, slot_id, row["claimed_at"] + STAGING_TTL)
    assert (found.phase, found.reason) == ("ended", "expired")
    assert staged(s3_api, slot_id) is None
    [source] = asks.get("/draft").json()["items"][0]["sources"]
    assert (source["check_status"], source["check_reason"]) == ("refused", EXPIRED)


def test_a_file_of_a_sent_order_has_no_deadline(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
    watcher: FakeWatcher,
) -> None:
    [slot_id] = uploaded(asks, [("cube.stl", STL, "model/stl")])
    order_id = ask(asks, {"uploads": [slot_id]}).json()["id"]
    submitted(asks, order_id)
    assert (slot_id, "submitted") in watcher.signals
    found = step(s3_api, slot_id, datetime.now(UTC) + timedelta(days=30))
    assert (found.phase, found.due) == ("submitted", None)
    assert staged(s3_api, slot_id) == len(STL)
    assert sweep(s3_api, datetime.now(UTC) + timedelta(days=30)) == 0
    assert staged(s3_api, slot_id) == len(STL)


def test_a_declined_file_of_a_sent_order_is_cleaned_up(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
) -> None:
    [slot_id] = uploaded(asks, [("cube.stl", STL, "model/stl")])
    order_id = ask(asks, {"uploads": [slot_id]}).json()["id"]
    submitted(asks, order_id)
    with _state(s3_api).projection.pool.connection() as conn:
        conn.execute("UPDATE order_item_sources SET status = 'declined'")
    found = step(s3_api, slot_id, datetime.now(UTC))
    assert (found.phase, found.reason) == ("ended", "declined")
    assert staged(s3_api, slot_id) is None


def test_a_step_run_again_after_its_row_went_deletes_the_objects(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
) -> None:
    """A crash between the clean-up's commit and its deletes: the retry finds no row and
    deletes the object anyway; a missing object is no error."""
    [slot_id] = uploaded(asks, [("cube.stl", STL, "model/stl")])
    with _state(s3_api).projection.pool.connection() as conn:
        conn.execute("DELETE FROM storefront_uploads")
    for _ in range(2):
        found = step(s3_api, slot_id, datetime.now(UTC))
        assert (found.phase, found.reason) == ("ended", "gone")
    assert staged(s3_api, slot_id) is None


def test_the_backstop_takes_an_old_object_no_row_names(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
) -> None:
    store = backend(s3_api)
    stray = staging_key(store.settings.prefix, "e" * 32)
    kept = uploaded(asks, [("cube.stl", STL, "model/stl")])[0]

    async def plant() -> None:
        client = await store.client()
        await client.put_object(Bucket=store.settings.bucket, Key=stray, Body=b"x")

    on_app(s3_api, plant)
    assert sweep(s3_api, datetime.now(UTC) + timedelta(minutes=10)) == 0
    assert on_app(s3_api, store.staged_size, stray) == 1
    assert sweep(s3_api, datetime.now(UTC) + timedelta(days=2)) == 1
    assert on_app(s3_api, store.staged_size, stray) is None
    # A named object only goes with its row, by its watcher.
    assert staged(s3_api, kept) == len(STL)


def test_the_backstop_restarts_a_watcher_that_stopped(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
    watcher: FakeWatcher,
) -> None:
    [lost, fine] = uploaded(asks, [("a.stl", STL, "model/stl"), ("b.stl", STL, "model/stl")])
    watcher.stopped = {lost}
    later = datetime.now(UTC) + timedelta(days=2)
    assert sweep(s3_api, later) == 1
    assert watcher.started[-1] == lost
    assert watcher.started.count(fine) == 1
    assert sweep(s3_api, later) == 0


def test_the_backstop_walks_every_old_row_a_batch_at_a_time(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
    watcher: FakeWatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the first batch too: healthy old rows (a submitted file waits for days) never
    hide a newer row whose watcher stopped."""
    monkeypatch.setattr(staged_files, "SWEEP_BATCH", 1)
    files = [("a.stl", STL, "model/stl"), ("b.stl", STL, "model/stl"), ("c.stl", STL, "model/stl")]
    ids = uploaded(asks, files)
    newest = rows(s3_api, "SELECT id FROM storefront_uploads ORDER BY created_at DESC, id DESC")
    watcher.stopped = {newest[0]["id"]}
    assert sweep(s3_api, datetime.now(UTC) + timedelta(days=2)) == 1
    assert watcher.started[len(ids) :] == [newest[0]["id"]]


def test_a_backstop_pass_is_bounded_and_the_next_goes_on_from_there(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
    watcher: FakeWatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One pass checks at most `SWEEP_PASS_BATCHES` batches, so the drafts sweep keeps
    inside its timeout; the next pass checks the rest, then starts again from the oldest."""
    monkeypatch.setattr(staged_files, "SWEEP_BATCH", 1)
    monkeypatch.setattr(staged_files, "SWEEP_PASS_BATCHES", 2)
    files = [("a.stl", STL, "model/stl"), ("b.stl", STL, "model/stl"), ("c.stl", STL, "model/stl")]
    ids = uploaded(asks, files)
    order = rows(s3_api, "SELECT id FROM storefront_uploads ORDER BY created_at, id")
    watcher.stopped = {order[2]["id"]}
    later = datetime.now(UTC) + timedelta(days=2)
    assert sweep(s3_api, later) == 0  # the first two rows only
    assert sweep(s3_api, later) == 1  # the third, from where the first pass stopped
    assert watcher.started[len(ids) :] == [order[2]["id"]]
    watcher.stopped = {order[0]["id"]}
    assert sweep(s3_api, later) == 1  # past the end: from the oldest again


def test_a_concurrent_first_send_whose_watchers_failed_is_refused(
    asks: CrudCaller,  # noqa: F811
    s3_api: TestClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second first send of a key answers the slots the first committed; when the first
    dropped them meanwhile (its watchers failed), it answers 503, never an empty 200."""
    from scadbuddy.crud.idempotency import Replay, idempotent_write
    from scadbuddy.orders.storefront import model_requests

    def first_send_won(conn: Any, *args: Any) -> Any:
        idempotent_write(conn, *args)
        return Replay(200, {"ids": ["dropped-by-the-first-send"]})

    monkeypatch.setattr(model_requests, "idempotent_write", first_send_won)
    r = slots(asks, [("cube.stl", STL, "model/stl")])
    assert r.status_code == 503, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}{WATCH_UNAVAILABLE}"


def test_the_backstop_does_nothing_on_another_store(api: TestClient) -> None:  # noqa: F811
    assert sweep(api, datetime.now(UTC)) == 0


@pytest.fixture(autouse=True)
def _short_batches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(staged_files, "SWEEP_BATCH", 50)
    staged_files._resume.clear()
    yield
    staged_files._resume.clear()
