"""A storefront customer's own models on the CRUD side (#6133, spec 2026-11-13 §5, §6):
``POST …/storefront/uploads`` answers a presigned PUT per file into the store's staging
area, ``POST …/storefront/draft/models`` claims them after a ``HEAD`` each and adds a
request item with its files and links, and the storefront drafts sweep deletes what is
never claimed."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from scadbuddy.orders.storefront import model_requests
from scadbuddy.orders.storefront.model_requests import EXPIRED, sweep_staged
from scadbuddy.store.s3 import S3ContentBackend, staging_key
from scadbuddy.storefront.wire import MAX_MODEL_FILE_BYTES, STOREFRONT_PROBLEMS
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
    assert slot["headers"] == {"Content-Type": "model/stl"}
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


# -- the sweep --------------------------------------------------------------------------


def staged(app: TestClient, slot_id: str) -> int | None:
    store = backend(app)
    return on_app(app, store.staged_size, staging_key(store.settings.prefix, slot_id))


def sweep(app: TestClient, now: datetime) -> int:
    return on_app(app, lambda: sweep_staged(_state(app), now=now))


def test_the_sweep_takes_a_slot_never_claimed(asks: CrudCaller, s3_api: TestClient) -> None:  # noqa: F811
    [slot_id] = uploaded(asks, [("cube.stl", STL, "model/stl")])
    now = datetime.now(UTC)
    assert sweep(s3_api, now) == 0
    assert staged(s3_api, slot_id) == len(STL)
    assert sweep(s3_api, now + timedelta(hours=2)) == 1
    assert staged(s3_api, slot_id) is None
    assert rows(s3_api, "SELECT id FROM storefront_uploads") == []


def test_the_sweep_takes_a_file_whose_line_is_gone(asks: CrudCaller, s3_api: TestClient) -> None:  # noqa: F811
    [slot_id] = uploaded(asks, [("cube.stl", STL, "model/stl")])
    item = ask(asks, {"uploads": [slot_id]}).json()["items"][0]
    assert asks.delete(f"/draft/items/{item['id']}", key="d1").status_code == 200
    assert sweep(s3_api, datetime.now(UTC)) == 1
    assert staged(s3_api, slot_id) is None


def test_the_sweep_expires_a_file_of_a_cart_not_sent(asks: CrudCaller, s3_api: TestClient) -> None:  # noqa: F811
    [slot_id] = uploaded(asks, [("cube.stl", STL, "model/stl")])
    ask(asks, {"uploads": [slot_id]})
    assert sweep(s3_api, datetime.now(UTC) + timedelta(hours=1)) == 0
    assert sweep(s3_api, datetime.now(UTC) + timedelta(hours=73)) == 1
    assert staged(s3_api, slot_id) is None
    [source] = asks.get("/draft").json()["items"][0]["sources"]
    assert (source["check_status"], source["check_reason"]) == ("refused", EXPIRED)


def test_the_sweep_keeps_a_file_of_a_sent_order(asks: CrudCaller, s3_api: TestClient) -> None:  # noqa: F811
    [slot_id] = uploaded(asks, [("cube.stl", STL, "model/stl")])
    order_id = ask(asks, {"uploads": [slot_id]}).json()["id"]
    with _state(s3_api).projection.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO customers (display_name) VALUES ('A customer') RETURNING id"
        ).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE orders SET submitted_at = now(), number = 'ORD-2026-9', customer_id = %s"
            " WHERE id = %s",
            (row["id"], order_id),
        )
    assert sweep(s3_api, datetime.now(UTC) + timedelta(days=30)) == 0
    assert staged(s3_api, slot_id) == len(STL)


def test_the_sweep_takes_an_old_object_no_row_names(asks: CrudCaller, s3_api: TestClient) -> None:  # noqa: F811
    store = backend(s3_api)
    stray = staging_key(store.settings.prefix, "e" * 32)
    kept = uploaded(asks, [("cube.stl", STL, "model/stl")])[0]

    async def plant() -> None:
        client = await store.client()
        await client.put_object(Bucket=store.settings.bucket, Key=stray, Body=b"x")

    on_app(s3_api, plant)
    sweep(s3_api, datetime.now(UTC) + timedelta(minutes=10))
    assert on_app(s3_api, store.staged_size, stray) == 1
    sweep(s3_api, datetime.now(UTC) + timedelta(days=2))
    assert on_app(s3_api, store.staged_size, stray) is None
    # The swept slot was never claimed, so it went too; a named object only goes with its row.
    assert staged(s3_api, kept) is None


def test_the_sweep_does_nothing_on_another_store(api: TestClient) -> None:  # noqa: F811
    assert sweep(api, datetime.now(UTC)) == 0


@pytest.fixture(autouse=True)
def _short_batches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(model_requests, "SWEEP_BATCH", 50)
    yield
