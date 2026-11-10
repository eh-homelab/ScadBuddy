"""Plaid's webhooks for the linked account (#4961). Each is checked by its
``Plaid-Verification`` JWT before it is read
(https://plaid.com/docs/api/webhooks/webhook-verification/): ES256, the key from
``/webhook_verification_key/get``, ``iat`` at most 5 minutes old, and the body's SHA-256 in
``request_body_sha256``. The tests sign with a key of their own, served by respx in the
JWK shape Plaid documents; the bodies are Plaid's examples
(https://plaid.com/docs/api/products/transactions/#sync_updates_available,
https://plaid.com/docs/api/items/#error)."""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import time
import types
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from joserfc import jwt
from joserfc.jwk import ECKey, OctKey

from scadbuddy.orders.store import OrderStore
from scadbuddy.payments import bank as bank_module
from scadbuddy.payments import plaid
from scadbuddy.payments.bank import BankLinks, WebhookKeyLookupLimitedError
from scadbuddy.payments.plaid import PlaidError
from scadbuddy.payments.provider import BadSignatureError
from tests.payments.test_bank import ACCESS, KEK, KEYS, SANDBOX, linked, payload

pytestmark = pytest.mark.requires_postgres

PAYLOADS = Path(__file__).parent / "plaid_payloads"
KEY = ECKey.generate_key("P-256")
OTHER = ECKey.generate_key("P-256")
KID = "6c5516e1-92dc-479e-a8ff-5a51992e0001"
VERIFY = f"{SANDBOX}/webhook_verification_key/get"


def body_of(name: str) -> bytes:
    return (PAYLOADS / name).read_bytes()


def signed(
    body: bytes,
    *,
    key: ECKey = KEY,
    alg: str = "ES256",
    iat: float | None = None,
    digest: str | None = None,
    kid: str = KID,
) -> dict[str, str]:
    claims = {
        "iat": int(time.time() if iat is None else iat),
        "request_body_sha256": digest or hashlib.sha256(body).hexdigest(),
    }
    token = jwt.encode({"alg": alg, "kid": kid, "typ": "JWT"}, claims, key)
    return {"Plaid-Verification": token}


def plaid_key(router: respx.MockRouter) -> respx.Route:
    """``/webhook_verification_key/get`` as Plaid answers it: the public JWK."""
    jwk: dict[str, Any] = {
        **KEY.as_dict(private=False),
        "alg": "ES256",
        "kid": KID,
        "use": "sig",
        "created_at": 1560466150,
        "expired_at": None,
    }
    return router.post(VERIFY).mock(
        return_value=httpx.Response(200, json={"key": jwk, "request_id": "RZ6Omi1bzzwDaLo"})
    )


def any_key(router: respx.MockRouter) -> respx.Route:
    """``/webhook_verification_key/get`` giving ``KEY``'s public JWK under whichever
    ``key_id`` it is asked for."""

    def answer(request: httpx.Request) -> httpx.Response:
        kid = json.loads(request.content)["key_id"]
        jwk = {**KEY.as_dict(private=False), "alg": "ES256", "kid": kid, "use": "sig"}
        return httpx.Response(200, json={"key": jwk, "request_id": "RZ6Omi1bzzwDaLo"})

    return router.post(VERIFY).mock(side_effect=answer)


def unknown_key(router: respx.MockRouter) -> respx.Route:
    """Plaid's answer for a ``key_id`` it does not have
    (https://plaid.com/docs/errors/invalid-input/#invalid_webhook_verification_key_id)."""
    return router.post(VERIFY).mock(
        return_value=httpx.Response(400, json=payload("error.invalid_webhook_key_id.json"))
    )


def kid_of(n: int) -> str:
    return f"6c5516e1-92dc-479e-a8ff-{n:012x}"


class Clock:
    """``time.monotonic`` for one ``BankLinks``, moved by the test."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def lookups_unspaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The key lookup limits are process-wide; a test that does not test them lifts them."""
    unlimited = bank_module.KeyLookups(burst=10_000, source_burst=10_000)
    monkeypatch.setattr(bank_module, "_KEY_LOOKUPS", unlimited)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def bank(rows: OrderStore, clock: Clock) -> BankLinks:
    made = BankLinks(rows, KEK, clock=clock)
    with respx.mock as router:
        await linked(made, router)
    return made


async def test_sync_updates_available_asks_for_a_sync(bank: BankLinks) -> None:
    body = body_of("webhook.sync_updates_available.json")
    with respx.mock as router:
        key = plaid_key(router)
        assert await bank.handle_webhook(signed(body), body) == "sync"
        assert json.loads(key.calls.last.request.content) == {"key_id": KID}
        # The key is kept: a second webhook asks Plaid for nothing.
        assert await bank.handle_webhook(signed(body), body) == "sync"
        assert key.call_count == 1


async def test_item_login_required_marks_the_link_and_login_repaired_clears_it(
    bank: BankLinks,
) -> None:
    required = body_of("webhook.item_login_required.json")
    repaired = body_of("webhook.login_repaired.json")
    with respx.mock as router:
        plaid_key(router)
        assert await bank.handle_webhook(signed(required), required) == "login_required"
        assert bank.status().login_required is True
        assert await bank.handle_webhook(signed(repaired), repaired) == "login_repaired"
    assert bank.status().login_required is False


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Plaid-Verification": "not-a-jwt"},
        signed(b"x", key=OTHER),
        signed(b"x", iat=time.time() - 301),
        signed(b"x", digest="0" * 64),
    ],
    ids=["missing", "garbage", "other key", "too old", "other body"],
)
async def test_a_webhook_that_does_not_verify_changes_nothing(
    bank: BankLinks, headers: dict[str, str]
) -> None:
    body = body_of("webhook.item_login_required.json")
    with respx.mock as router:
        plaid_key(router)
        with pytest.raises(BadSignatureError):
            await bank.handle_webhook(headers, body)
    assert bank.status().login_required is False


async def test_only_es256_is_taken(bank: BankLinks) -> None:
    body = body_of("webhook.sync_updates_available.json")
    with respx.mock as router:
        key = plaid_key(router)
        token = jwt.encode(
            {"alg": "HS256", "kid": KID},
            {"iat": int(time.time()), "request_body_sha256": hashlib.sha256(body).hexdigest()},
            OctKey.import_key(b"0" * 32),
        )
        with pytest.raises(BadSignatureError):
            await bank.handle_webhook({"plaid-verification": token}, body)
        assert not key.called


async def test_a_webhook_for_another_item_is_ignored(bank: BankLinks) -> None:
    found = json.loads(body_of("webhook.item_login_required.json"))
    body = json.dumps({**found, "item_id": "another-item"}).encode()
    with respx.mock as router:
        plaid_key(router)
        assert await bank.handle_webhook(signed(body), body) == "ignored"
    assert bank.status().login_required is False


async def test_a_key_plaid_cannot_give_is_a_plaid_error(bank: BankLinks) -> None:
    body = body_of("webhook.sync_updates_available.json")
    with respx.mock as router:
        router.post(VERIFY).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(PlaidError):
            await bank.handle_webhook(signed(body), body)


@pytest.mark.parametrize(
    "kid",
    ["1", "../webhook_verification_key", "6C5516E1-92DC-479E-A8FF-5A51992E0001 ", "a" * 4096],
    ids=["short", "path", "trailing space", "long"],
)
async def test_a_kid_not_shaped_like_plaids_asks_plaid_for_nothing(
    bank: BankLinks, kid: str
) -> None:
    body = body_of("webhook.sync_updates_available.json")
    with respx.mock as router:
        key = any_key(router)
        with pytest.raises(BadSignatureError):
            await bank.handle_webhook(signed(body, kid=kid), body)
        assert not key.called


async def test_a_kid_plaid_does_not_know_is_refused_and_remembered_for_a_while(
    bank: BankLinks, clock: Clock
) -> None:
    body = body_of("webhook.sync_updates_available.json")
    with respx.mock as router:
        unknown = unknown_key(router)
        for _ in range(3):
            with pytest.raises(BadSignatureError):
                await bank.handle_webhook(signed(body), body)
        assert unknown.call_count == 1
        clock.now += bank_module.UNKNOWN_KEY_SECONDS + 1
        with pytest.raises(BadSignatureError):
            await bank.handle_webhook(signed(body), body)
        assert unknown.call_count == 2


def limited(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    """The real limits, on the test's clock."""
    monkeypatch.setattr(bank_module, "_KEY_LOOKUPS", bank_module.KeyLookups(clock=clock))


async def test_key_lookups_take_a_burst_then_one_per_interval_process_wide(
    rows: OrderStore, bank: BankLinks, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    limited(monkeypatch, clock)
    other = BankLinks(rows, KEK, clock=clock)
    body = body_of("webhook.sync_updates_available.json")
    burst = bank_module.KEY_LOOKUP_BURST
    with respx.mock as router:
        key = any_key(router)
        for n in range(burst):
            # A source each, so only the process bucket counts.
            hook = signed(body, kid=kid_of(n))
            assert await bank.handle_webhook(hook, body, source=f"192.0.2.{n}") == "sync"
        # The burst is spent, from either instance.
        for links in (bank, other):
            with pytest.raises(WebhookKeyLookupLimitedError) as refused:
                await links.handle_webhook(signed(body, kid=kid_of(99)), body, source="192.0.2.99")
            assert 0 < refused.value.retry_after <= bank_module.KEY_LOOKUP_SECONDS
        # A key that verified needs no lookup, so it is never limited.
        assert await bank.handle_webhook(signed(body, kid=kid_of(0)), body) == "sync"
        assert key.call_count == burst
        clock.now += bank_module.KEY_LOOKUP_SECONDS
        hook = signed(body, kid=kid_of(99))
        assert await bank.handle_webhook(hook, body, source="192.0.2.99") == "sync"
        assert key.call_count == burst + 1


async def test_one_source_is_limited_before_the_process_bucket(
    bank: BankLinks, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    limited(monkeypatch, clock)
    body = body_of("webhook.sync_updates_available.json")
    with respx.mock as router:
        unknown = unknown_key(router)
        for n in range(bank_module.SOURCE_LOOKUP_BURST):
            with pytest.raises(BadSignatureError):
                await bank.handle_webhook(signed(body, kid=kid_of(n)), body, source="attacker")
        for n in range(100, 110):
            with pytest.raises(WebhookKeyLookupLimitedError):
                await bank.handle_webhook(signed(body, kid=kid_of(n)), body, source="attacker")
        assert unknown.call_count == bank_module.SOURCE_LOOKUP_BURST
    # The attacker's refusals spent nothing of the process bucket: Plaid's webhook still
    # gets its lookup.
    with respx.mock as router:
        key = any_key(router)
        assert await bank.handle_webhook(signed(body), body, source="plaid") == "sync"
        assert key.call_count == 1


async def test_random_kids_never_evict_a_key_that_verified(bank: BankLinks) -> None:
    body = body_of("webhook.sync_updates_available.json")

    def answer(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["key_id"] == KID:
            jwk = {**KEY.as_dict(private=False), "alg": "ES256", "kid": KID, "use": "sig"}
            return httpx.Response(200, json={"key": jwk, "request_id": "r"})
        return httpx.Response(400, json=payload("error.invalid_webhook_key_id.json"))

    with respx.mock as router:
        route = router.post(VERIFY).mock(side_effect=answer)
        assert await bank.handle_webhook(signed(body), body) == "sync"
        for n in range(bank_module.MAX_UNKNOWN_KEYS + bank_module.MAX_WEBHOOK_KEYS + 10):
            with pytest.raises(BadSignatureError):
                await bank.handle_webhook(signed(body, kid=kid_of(n)), body)
        calls = route.call_count
        assert await bank.handle_webhook(signed(body), body) == "sync"
        assert route.call_count == calls


async def test_a_key_that_verified_nothing_is_not_kept(bank: BankLinks) -> None:
    body = body_of("webhook.sync_updates_available.json")
    with respx.mock as router:
        key = any_key(router)
        with pytest.raises(BadSignatureError):
            await bank.handle_webhook(signed(body, key=OTHER, kid=kid_of(7)), body)
        assert await bank.handle_webhook(signed(body, kid=kid_of(7)), body) == "sync"
        assert key.call_count == 2


async def test_webhooks_waiting_on_one_lookup_share_it_when_the_bucket_is_empty(
    bank: BankLinks, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bank_module, "_KEY_LOOKUPS", bank_module.KeyLookups(clock=clock, burst=1))
    body = body_of("webhook.sync_updates_available.json")
    release = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        await release.wait()
        jwk = {**KEY.as_dict(private=False), "alg": "ES256", "kid": KID, "use": "sig"}
        return httpx.Response(200, json={"key": jwk, "request_id": "r"})

    with respx.mock as router:
        route = router.post(VERIFY).mock(side_effect=slow)
        first = asyncio.create_task(bank.handle_webhook(signed(body), body, source="plaid-1"))
        await asyncio.sleep(0.05)
        # The bucket is empty, but this kid's lookup is in flight: the second waits for it.
        second = asyncio.create_task(bank.handle_webhook(signed(body), body, source="plaid-2"))
        await asyncio.sleep(0.05)
        release.set()
        assert await first == "sync"
        assert await second == "sync"
        assert route.call_count == 1
        # Another kid, with the bucket still empty, is refused.
        with pytest.raises(WebhookKeyLookupLimitedError):
            await bank.handle_webhook(signed(body, kid=kid_of(5)), body, source="plaid-1")


async def test_a_full_key_cache_drops_its_least_recently_used_key_only(
    bank: BankLinks,
) -> None:
    body = body_of("webhook.sync_updates_available.json")
    with respx.mock as router:
        key = any_key(router)
        for n in range(bank_module.MAX_WEBHOOK_KEYS):
            await bank.handle_webhook(signed(body, kid=kid_of(n)), body)
        # Key 0 is used again, so key 1 is now the least recently used.
        await bank.handle_webhook(signed(body, kid=kid_of(0)), body)
        await bank.handle_webhook(signed(body, kid=kid_of(99)), body)
        assert key.call_count == bank_module.MAX_WEBHOOK_KEYS + 1
        for n in [0, *range(2, bank_module.MAX_WEBHOOK_KEYS), 99]:
            await bank.handle_webhook(signed(body, kid=kid_of(n)), body)
        assert key.call_count == bank_module.MAX_WEBHOOK_KEYS + 1
        await bank.handle_webhook(signed(body, kid=kid_of(1)), body)
        assert key.call_count == bank_module.MAX_WEBHOOK_KEYS + 2


async def test_reconnect_link_token_is_update_mode(bank: BankLinks) -> None:
    with respx.mock as router:
        route = router.post(f"{SANDBOX}/link/token/create").mock(
            return_value=httpx.Response(200, json=payload("link_token.create.json"))
        )
        await bank.link_token()
    sent = json.loads(route.calls.last.request.content)
    # Update mode: the Item's token and no products (https://plaid.com/docs/link/update-mode/).
    assert sent["access_token"] == ACCESS and "products" not in sent


async def test_the_link_token_carries_the_webhook_url(rows: OrderStore) -> None:
    bank = BankLinks(
        rows, KEK, webhook_url=lambda: "https://shop.example/api/v1/payments/bank/webhook"
    )
    with respx.mock as router:
        routes = {
            "token": router.post(f"{SANDBOX}/link/token/create").mock(
                return_value=httpx.Response(200, json=payload("link_token.create.json"))
            )
        }
        await bank.save_keys(KEYS)
        await bank.link_token()
    sent = json.loads(routes["token"].calls.last.request.content)
    assert sent["webhook"] == "https://shop.example/api/v1/payments/bank/webhook"
    assert sent["products"] == ["transactions"]


async def test_reconnected_clears_the_mark(bank: BankLinks) -> None:
    body = body_of("webhook.item_login_required.json")
    with respx.mock as router:
        plaid_key(router)
        await bank.handle_webhook(signed(body), body)
    bank.reconnected()
    assert bank.status().login_required is False


def test_the_header_name_is_plaids() -> None:
    assert plaid.VERIFICATION_HEADER == "plaid-verification"


async def test_the_webhook_s_sync_tells_the_order_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sync a webhook asks for publishes `order.bank_deposits` on the app's bus, as the
    sweep's does, so an open order page shows a deposit suggested early."""
    from scadbuddy.api import bank_link

    heard: list[object] = []

    async def sync(bank: object, bus: object = None) -> int:
        heard.append(bus)
        return 0

    monkeypatch.setattr(bank_link, "sync_deposits", sync)
    bus = object()
    payments = types.SimpleNamespace(bank=object())
    await bank_link._sync_logged(payments, bus)  # type: ignore[arg-type]
    assert heard == [bus]


def _sync_error(bank: BankLinks) -> str | None:
    with bank.store.transaction() as conn:
        row = conn.execute("SELECT sync_error FROM bank_links").fetchone()
    assert row is not None
    found: str | None = row["sync_error"]
    return found


def _failed_on_login(bank: BankLinks, code: str = plaid.LOGIN_REQUIRED) -> str:
    with bank.store.transaction() as conn:
        conn.execute("UPDATE bank_links SET sync_error = %s", (code,))
        row = conn.execute("SELECT item_id FROM bank_links").fetchone()
    assert row is not None
    bank.mark_login(str(row["item_id"]), required=True)
    return str(row["item_id"])


async def test_reconnecting_clears_the_sign_in_failure(bank: BankLinks) -> None:
    """The card's "could not be read (ITEM_LOGIN_REQUIRED)" goes with "Reconnect"."""
    _failed_on_login(bank)
    bank.reconnected()
    assert _sync_error(bank) is None


async def test_login_repaired_clears_the_sign_in_failure_only(bank: BankLinks) -> None:
    item_id = _failed_on_login(bank)
    bank.mark_login(item_id, required=False)
    assert _sync_error(bank) is None
    _failed_on_login(bank, "INSTITUTION_DOWN")
    bank.mark_login(item_id, required=False)
    # Another failure is not the sign-in's: it stays until a sync reads Plaid.
    assert _sync_error(bank) == "INSTITUTION_DOWN"


async def test_a_key_plaid_says_expired_is_refused(bank: BankLinks) -> None:
    """``expired_at`` is "the timestamp when the key expired"
    (https://plaid.com/docs/api/webhooks/webhook-verification/): such a key verifies
    nothing."""
    body = body_of("webhook.sync_updates_available.json")
    with respx.mock as router:
        route = plaid_key(router)
        jwk = json.loads(route.return_value.content)  # type: ignore[union-attr]
        jwk["key"]["expired_at"] = 1700000000
        route.mock(return_value=httpx.Response(200, json=jwk))
        with pytest.raises(BadSignatureError):
            await bank.handle_webhook(signed(body), body)


async def test_a_kept_key_is_asked_for_again_after_a_day(bank: BankLinks, clock: Clock) -> None:
    """A key Plaid expires later is noticed: a kept key is read again once a day."""
    body = body_of("webhook.sync_updates_available.json")
    with respx.mock as router:
        key = plaid_key(router)
        assert await bank.handle_webhook(signed(body), body) == "sync"
        clock.now += bank_module.KEY_RECHECK_SECONDS + 1
        assert await bank.handle_webhook(signed(body), body) == "sync"
        assert key.call_count == 2


async def test_reconnect_needs_the_stored_access_token(bank: BankLinks) -> None:
    """Update mode needs the Item's token; without it, Link would make a new Item."""
    row = bank._read()
    assert row is not None
    sealed = bank._seal(str(row["id"]), {"client_id": "client-1", "secret": "plaid-s3cret"})
    with bank.store.transaction() as conn:
        conn.execute(
            "UPDATE bank_links SET secret_sealed = %s, dek_sealed = %s, kek_id = %s",
            (sealed.secret_sealed, sealed.dek_sealed, sealed.kek_id),
        )
    with respx.mock, pytest.raises(bank_module.AccessTokenMissingError):  # nothing reaches Plaid
        await bank.link_token()


async def test_a_refused_lookup_reads_no_keys(
    bank: BankLinks, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forged ``kid`` over the limit costs no database read and no unseal: the caches
    and the bucket come before the keys."""
    monkeypatch.setattr(bank_module, "_KEY_LOOKUPS", bank_module.KeyLookups(clock=clock, burst=0))
    reads: list[int] = []
    real = bank.access

    def counted() -> Any:
        reads.append(1)
        return real()

    monkeypatch.setattr(bank, "access", counted)
    body = body_of("webhook.sync_updates_available.json")
    with respx.mock, pytest.raises(WebhookKeyLookupLimitedError):
        await bank.handle_webhook(signed(body, kid=kid_of(7)), body, source="192.0.2.7")
    assert reads == []


async def test_a_token_from_the_future_is_refused(bank: BankLinks) -> None:
    """``iat`` more than a minute ahead is not a token Plaid just made: a captured one
    cannot be replayed for longer than the five minutes."""
    body = body_of("webhook.sync_updates_available.json")
    with respx.mock as router:
        plaid_key(router)
        with pytest.raises(BadSignatureError):
            await bank.handle_webhook(signed(body, iat=time.time() + 120), body)
        # A little skew is fine.
        assert await bank.handle_webhook(signed(body, iat=time.time() + 30), body) == "sync"


async def test_a_lookup_nobody_waits_for_any_more_logs_nothing(bank: BankLinks) -> None:
    """A lookup that fails after its webhook's client went away keeps its error to itself."""
    loop = asyncio.get_running_loop()
    seen: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _, context: seen.append(context))
    release = asyncio.Event()

    async def failing(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(500, json={"error_code": "INTERNAL_SERVER_ERROR"})

    body = body_of("webhook.sync_updates_available.json")
    try:
        with respx.mock as router:
            router.post(VERIFY).mock(side_effect=failing)
            waiter = asyncio.create_task(bank.handle_webhook(signed(body), body))
            await asyncio.sleep(0.05)
            waiter.cancel()
            release.set()
            await asyncio.sleep(0.05)
        del waiter
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)
    assert [c.get("message") for c in seen] == []


def test_refused_sources_never_push_out_another_source_s_bucket(clock: Clock) -> None:
    """A sender cycling addresses while the process bucket is empty keeps no bucket, so a
    source that spent its own burst is not pushed out and refilled."""
    lookups = bank_module.KeyLookups(clock=clock, burst=1, source_burst=1)
    assert lookups.take("192.0.2.1") == 0
    for n in range(bank_module.MAX_LOOKUP_SOURCES + 5):
        assert lookups.take(f"198.51.100.{n}") > 0
    clock.now += bank_module.KEY_LOOKUP_SECONDS
    # The process bucket holds a token again, but the first source's own is still spent.
    assert lookups.take("192.0.2.1") > 0


def test_the_webhook_route_names_both_its_503s() -> None:
    from scadbuddy.api.bank_link import router

    [hook] = [r for r in router.routes if getattr(r, "path", "") == "/payments/bank/webhook"]
    text = getattr(hook, "description", "")
    assert "`provider_unavailable`" in text and "`credential_unreadable`" in text


def test_a_refused_source_keeps_its_place_among_the_buckets(clock: Clock) -> None:
    """Only an admitted lookup moves its source to the end: a source refused again and
    again is still the first forgotten once newer sources are admitted."""
    lookups = bank_module.KeyLookups(clock=clock, burst=10_000, source_burst=1)
    assert lookups.take("192.0.2.1") == 0
    assert lookups.take("192.0.2.2") == 0
    assert lookups.take("192.0.2.1") > 0  # refused: its own bucket is spent
    for n in range(bank_module.MAX_LOOKUP_SOURCES - 1):
        assert lookups.take(f"198.51.100.{n}") == 0
    # The second source was admitted after the first's refusals: it is kept, spent.
    assert lookups.take("192.0.2.2") > 0
    # The first was forgotten, so it starts again with a full bucket.
    assert lookups.take("192.0.2.1") == 0
