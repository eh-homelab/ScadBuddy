"""The shop's linked bank account (#4961): the Plaid keys and the access token sealed in
one row, and only the account's name, type and mask in the clear. Plaid is answered by
respx with the payloads in ``plaid_payloads/``."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.core.secrets import kek_from_base64
from scadbuddy.orders.store import OrderStore
from scadbuddy.payments import plaid
from scadbuddy.payments.bank import (
    KEYS_UNREADABLE,
    REMOVE_AT_PLAID,
    TOKEN_MISSING,
    AccessTokenMissingError,
    BankAccount,
    BankChoiceError,
    BankKeys,
    BankKeysChangedError,
    BankLinkedError,
    BankLinks,
    BankNotConfiguredError,
    BankNotLinkedError,
)
from scadbuddy.payments.credentials import CredentialUnreadableError, KekMissingError
from scadbuddy.payments.plaid import PlaidError
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

PAYLOADS = Path(__file__).parent / "plaid_payloads"
SANDBOX = "https://sandbox.plaid.com"
KEK = kek_from_base64("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
OTHER_KEK = kek_from_base64("HxwdHh8gISIjJCUmJygpKissLS4vMDEyMzQ1Njc4OTo=")
KEYS = BankKeys(environment="sandbox", client_id="client-1", secret="plaid-s3cret")
ACCESS = "access-sandbox-de3ce8ef-33f8-452c-a685-8671031fc0f6"
ITEM = "M5eVJqLnv3tbzdngLDp9FL5OlDNxlNhlE55op"
CHECKING = "BxBXxLj1m4HMXBm9WZZmCWVbPjX16EHwv99vp"
CREDIT = "Pp1Vpkl9w8sajvK6oEEKtr7vZxBnGpf7LxxLE"


def payload(name: str) -> Any:
    return json.loads((PAYLOADS / name).read_text())


def plaid_answers(router: respx.MockRouter) -> dict[str, respx.Route]:
    """Plaid's sandbox answers to every call a link makes."""
    answers = {
        "/link/token/create": "link_token.create.json",
        "/item/public_token/exchange": "item.public_token.exchange.json",
        "/accounts/get": "accounts.get.json",
        "/item/remove": "item.remove.json",
    }
    return {
        path: router.post(f"{SANDBOX}{path}").mock(
            return_value=httpx.Response(200, json=payload(name))
        )
        for path, name in answers.items()
    }


def raw_row(pool: PgPool) -> dict[str, Any] | None:
    with pool.connection() as conn:
        return conn.execute("SELECT * FROM bank_links").fetchone()


async def linked(bank: BankLinks, router: respx.MockRouter) -> dict[str, respx.Route]:
    routes = plaid_answers(router)
    await bank.save_keys(KEYS)
    await bank.link("public-sandbox-1", [CHECKING])
    return routes


async def test_nothing_is_configured_at_first(rows: OrderStore) -> None:
    status = BankLinks(rows, KEK).status()
    assert (status.configured, status.environment, status.account) == (False, None, None)


async def test_keys_are_checked_by_plaid_then_sealed(rows: OrderStore, pool: PgPool) -> None:
    bank = BankLinks(rows, KEK)
    with respx.mock as router:
        routes = plaid_answers(router)
        status = await bank.save_keys(KEYS)
    assert routes["/link/token/create"].called
    assert (status.configured, status.environment, status.account) == (True, "sandbox", None)
    raw = raw_row(pool)
    assert raw is not None and raw["kek_id"] == KEK.id
    assert b"plaid-s3cret" not in bytes(raw["secret_sealed"])
    assert "plaid-s3cret" not in repr(raw) and "client-1" not in repr(raw)


async def test_keys_plaid_refuses_are_not_kept(rows: OrderStore) -> None:
    bank = BankLinks(rows, KEK)
    with respx.mock as router:
        router.post(f"{SANDBOX}/link/token/create").mock(
            return_value=httpx.Response(400, json=payload("error.invalid_api_keys.json"))
        )
        with pytest.raises(PlaidError, match="invalid client_id or secret"):
            await bank.save_keys(KEYS)
    assert bank.status().configured is False


async def test_without_a_kek_nothing_is_saved_or_sent(rows: OrderStore) -> None:
    with respx.mock, pytest.raises(KekMissingError):
        await BankLinks(rows, None).save_keys(KEYS)


async def test_a_link_keeps_the_name_type_and_mask_only(
    rows: OrderStore, pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    bank = BankLinks(rows, KEK)
    with caplog.at_level(logging.DEBUG), respx.mock as router:
        routes = await linked(bank, router)
    assert json.loads(routes["/item/public_token/exchange"].calls.last.request.content) == {
        "public_token": "public-sandbox-1"
    }
    status = bank.status()
    assert status.account == BankAccount(name="Plaid Checking", type="checking", mask="0000")
    assert status.linked_at is not None
    raw = raw_row(pool)
    assert raw is not None
    assert (raw["item_id"], raw["account_id"]) == (ITEM, CHECKING)
    # The access token is sealed; no balance or official name is kept.
    assert ACCESS.encode() not in bytes(raw["secret_sealed"])
    assert ACCESS not in repr(raw) and "Gold Standard" not in repr(raw)
    assert ACCESS not in caplog.text and "plaid-s3cret" not in caplog.text
    assert bank.access() is not None


@pytest.mark.parametrize("chosen", [[], [CHECKING, "BxBXxLj1m4HMXBm9WZZmCWVbPjX16EHwv99vq"]])
async def test_none_or_several_accounts_chosen_removes_the_item(
    rows: OrderStore, chosen: list[str]
) -> None:
    """Link made the Item whatever the count: it is exchanged so it can be removed."""
    bank = BankLinks(rows, KEK)
    with respx.mock as router:
        routes = plaid_answers(router)
        await bank.save_keys(KEYS)
        with pytest.raises(BankChoiceError, match="Choose one checking or savings account"):
            await bank.link("public-sandbox-1", chosen)
    assert routes["/item/public_token/exchange"].called
    assert json.loads(routes["/item/remove"].calls.last.request.content) == {"access_token": ACCESS}
    assert bank.status().account is None


async def test_an_account_that_is_not_checking_or_savings_is_removed_at_plaid(
    rows: OrderStore,
) -> None:
    bank = BankLinks(rows, KEK)
    with respx.mock as router:
        routes = plaid_answers(router)
        await bank.save_keys(KEYS)
        with pytest.raises(PlaidError, match="checking or savings"):
            await bank.link("public-sandbox-1", [CREDIT])
    assert json.loads(routes["/item/remove"].calls.last.request.content) == {"access_token": ACCESS}
    assert bank.status().account is None


async def test_a_second_link_is_refused_and_its_item_removed(rows: OrderStore) -> None:
    bank = BankLinks(rows, KEK)
    with respx.mock as router:
        routes = await linked(bank, router)
        with pytest.raises(BankLinkedError):
            await bank.link("public-sandbox-2", [CHECKING])
        # Refused before any exchange: no second Item exists to remove.
        assert routes["/item/public_token/exchange"].call_count == 1
        assert not routes["/item/remove"].called


async def test_new_keys_are_refused_while_linked(rows: OrderStore) -> None:
    bank = BankLinks(rows, KEK)
    with respx.mock as router:
        await linked(bank, router)
        with pytest.raises(BankLinkedError):
            await bank.save_keys(KEYS)
    with pytest.raises(BankLinkedError):
        bank.forget_keys()


async def test_a_link_needs_keys(rows: OrderStore) -> None:
    bank = BankLinks(rows, KEK)
    with respx.mock:
        with pytest.raises(BankNotConfiguredError):
            await bank.link_token()
        with pytest.raises(BankNotConfiguredError):
            await bank.link("public-sandbox-1", [CHECKING])


async def test_a_link_token_names_the_row_not_the_shop(rows: OrderStore, pool: PgPool) -> None:
    bank = BankLinks(rows, KEK)
    with respx.mock as router:
        routes = plaid_answers(router)
        await bank.save_keys(KEYS)
        token = await bank.link_token()
    assert token == "link-sandbox-af1a0311-da53-4636-b754-dd15cc058176"
    raw = raw_row(pool)
    assert raw is not None
    sent = json.loads(routes["/link/token/create"].calls.last.request.content)
    assert sent["user"] == {"client_user_id": raw["id"]}


async def test_unlink_removes_the_item_and_drops_the_token(rows: OrderStore, pool: PgPool) -> None:
    bank = BankLinks(rows, KEK)
    with respx.mock as router:
        routes = await linked(bank, router)
        await bank.unlink()
    assert json.loads(routes["/item/remove"].calls.last.request.content) == {"access_token": ACCESS}
    status = bank.status()
    assert (status.configured, status.account, status.linked_at) == (True, None, None)
    raw = raw_row(pool)
    assert raw is not None and raw["item_id"] is None and raw["account_id"] is None
    assert bank.access() is None
    # The keys stay, so the shop can link again; then they can be forgotten.
    bank.forget_keys()
    assert raw_row(pool) is None


async def test_unlink_keeps_the_token_when_plaid_does_not_answer(rows: OrderStore) -> None:
    bank = BankLinks(rows, KEK)
    with respx.mock as router:
        await linked(bank, router)
        router.post(f"{SANDBOX}/item/remove").mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(PlaidError):
            await bank.unlink()
    assert bank.status().account is not None


@pytest.mark.parametrize("code", ["INVALID_API_KEYS", "UNAUTHORIZED_ENVIRONMENT"])
async def test_unlink_with_keys_plaid_refuses_unlinks_here_and_says_so(
    rows: OrderStore, pool: PgPool, code: str
) -> None:
    """The shop rotated its secret at Plaid: the Item cannot be removed with these keys,
    so it is unlinked here and the shop removes it in the Plaid Dashboard."""
    bank = BankLinks(rows, KEK)
    refusal = {**payload("error.invalid_api_keys.json"), "error_code": code}
    with respx.mock as router:
        await linked(bank, router)
        router.post(f"{SANDBOX}/item/remove").mock(return_value=httpx.Response(400, json=refusal))
        unlinked = await bank.unlink()
    assert unlinked.removed_at_plaid is False
    assert unlinked.note == REMOVE_AT_PLAID
    assert "Plaid refused the stored keys" in unlinked.note
    status = bank.status()
    assert (status.configured, status.account) == (True, None)
    raw = raw_row(pool)
    assert raw is not None and raw["item_id"] is None
    assert bank.access() is None
    # The keys can now be replaced.
    with respx.mock as router:
        plaid_answers(router)
        await bank.save_keys(KEYS)


async def test_unlink_with_keys_that_will_not_open_unlinks_here(
    rows: OrderStore, pool: PgPool
) -> None:
    with respx.mock as router:
        await linked(BankLinks(rows, KEK), router)
    with respx.mock:  # nothing may reach Plaid
        unlinked = await BankLinks(rows, OTHER_KEK).unlink()
    assert unlinked.removed_at_plaid is False
    # Plaid was never asked: the note names the key file, and says to save the keys again.
    assert unlinked.note == KEYS_UNREADABLE
    assert "could not be opened with this key file" in unlinked.note
    assert "save the keys again" in unlinked.note
    assert "Plaid refused" not in unlinked.note
    # The sealed token cannot be opened to reseal the keys: the row goes, keys and all.
    assert raw_row(pool) is None


async def test_unlink_without_a_kek_is_refused_and_keeps_the_link(
    rows: OrderStore, pool: PgPool
) -> None:
    """A key file not mounted may be mounted again: nothing is dropped meanwhile."""
    with respx.mock as router:
        await linked(BankLinks(rows, KEK), router)
    with respx.mock, pytest.raises(KekMissingError):  # nothing may reach Plaid
        await BankLinks(rows, None).unlink()
    raw = raw_row(pool)
    assert raw is not None and raw["item_id"] is not None
    assert BankLinks(rows, KEK).access() is not None


async def test_two_first_saves_at_once_keep_one_row(
    rows: OrderStore, pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both saves read no row; the second's insert meets the first's ``singleton`` and
    updates that row, sealed under its id, instead of failing."""
    first, second = BankLinks(rows, KEK), BankLinks(rows, KEK)
    with respx.mock as router:
        plaid_answers(router)
        await first.save_keys(KEYS)
        real = BankLinks._row

        def unseen(self: BankLinks, conn: Any, *, lock: bool = False) -> Any:
            if not lock:
                return real(self, conn, lock=lock)
            # The second save's locked read ran before the first save committed.
            monkeypatch.setattr(BankLinks, "_row", real)
            return None

        monkeypatch.setattr(BankLinks, "_row", unseen)
        other = BankKeys(environment="sandbox", client_id="client-2", secret="plaid-s3cret-2")
        await second.save_keys(other)
    with pool.connection() as conn:
        counted = conn.execute("SELECT count(*) AS n FROM bank_links").fetchone()
    assert counted is not None and counted["n"] == 1
    # The row opens under its own id, with the second save's keys.
    linked_keys = first._keys(first._read())[2]
    assert linked_keys.client_id == "client-2"


async def test_a_link_beaten_by_new_keys_says_so(
    rows: OrderStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The keys were replaced while Link was open: not "already linked"."""
    bank = BankLinks(rows, KEK)
    with respx.mock as router:
        routes = plaid_answers(router)
        await bank.save_keys(KEYS)
        real = plaid.exchange

        async def replaced(*args: Any, **kwargs: Any) -> Any:
            await BankLinks(rows, KEK).save_keys(KEYS)
            return await real(*args, **kwargs)

        monkeypatch.setattr(plaid, "exchange", replaced)
        with pytest.raises(BankKeysChangedError):
            await bank.link("public-sandbox-1", [CHECKING])
        assert routes["/item/remove"].called


async def test_a_link_beaten_by_forgotten_keys_says_so(
    rows: OrderStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    bank = BankLinks(rows, KEK)
    with respx.mock as router:
        plaid_answers(router)
        await bank.save_keys(KEYS)
        real = plaid.exchange

        async def forgotten(*args: Any, **kwargs: Any) -> Any:
            BankLinks(rows, KEK).forget_keys()
            return await real(*args, **kwargs)

        monkeypatch.setattr(plaid, "exchange", forgotten)
        with pytest.raises(BankKeysChangedError):
            await bank.link("public-sandbox-1", [CHECKING])


async def test_the_database_is_called_off_the_event_loop(
    rows: OrderStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every Postgres call of save_keys, link_token, link and unlink runs in a thread, as
    ``Payments.link`` does."""
    loop_thread = threading.get_ident()
    on_loop: list[str] = []
    real = BankLinks._row

    def watched(self: BankLinks, conn: Any, *, lock: bool = False) -> Any:
        if threading.get_ident() == loop_thread:
            on_loop.append("locked" if lock else "read")
        return real(self, conn, lock=lock)

    monkeypatch.setattr(BankLinks, "_row", watched)
    bank = BankLinks(rows, KEK)
    with respx.mock as router:
        plaid_answers(router)
        await bank.save_keys(KEYS)
        await bank.link_token()
        await bank.link("public-sandbox-1", [CHECKING])
        await bank.unlink()
    assert on_loop == []


async def test_unlink_of_a_link_with_no_token_says_so_and_keeps_the_keys(
    rows: OrderStore, pool: PgPool
) -> None:
    """A linked row whose sealed secrets hold no access token: not a key file problem."""
    bank = BankLinks(rows, KEK)
    with respx.mock as router:
        await linked(bank, router)
    row = bank._read()
    assert row is not None
    sealed = bank._seal(str(row["id"]), {"client_id": "client-1", "secret": "plaid-s3cret"})
    with pool.connection() as conn:
        conn.execute(
            "UPDATE bank_links SET secret_sealed = %s, dek_sealed = %s, kek_id = %s",
            (sealed.secret_sealed, sealed.dek_sealed, sealed.kek_id),
        )
    with pytest.raises(AccessTokenMissingError):
        bank.access()
    with respx.mock:  # nothing may reach Plaid
        unlinked = await bank.unlink()
    assert unlinked.removed_at_plaid is False
    assert unlinked.note == TOKEN_MISSING
    assert "key file" not in unlinked.note
    raw = raw_row(pool)
    assert raw is not None and raw["item_id"] is None
    status = bank.status()
    assert (status.configured, status.account) == (True, None)


async def test_a_link_whose_write_fails_removes_its_item(
    rows: OrderStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    bank = BankLinks(rows, KEK)
    with respx.mock as router:
        routes = plaid_answers(router)
        await bank.save_keys(KEYS)

        def broken(*args: object) -> object:
            raise RuntimeError("the database is gone")

        monkeypatch.setattr(bank, "_seal", broken)
        with pytest.raises(RuntimeError):
            await bank.link("public-sandbox-1", [CHECKING])
    assert json.loads(routes["/item/remove"].calls.last.request.content) == {"access_token": ACCESS}


async def test_unlink_of_an_item_plaid_already_removed_drops_it(rows: OrderStore) -> None:
    bank = BankLinks(rows, KEK)
    with respx.mock as router:
        await linked(bank, router)
        router.post(f"{SANDBOX}/item/remove").mock(
            return_value=httpx.Response(400, json=payload("error.item_not_found.json"))
        )
        await bank.unlink()
    assert bank.status().account is None


async def test_unlink_with_nothing_linked_is_refused(rows: OrderStore) -> None:
    with pytest.raises(BankNotLinkedError):
        await BankLinks(rows, KEK).unlink()


async def test_a_row_sealed_under_another_key_is_not_opened(rows: OrderStore) -> None:
    with respx.mock as router:
        plaid_answers(router)
        await BankLinks(rows, OTHER_KEK).save_keys(KEYS)
    with pytest.raises(CredentialUnreadableError, match="plaid"):
        await BankLinks(rows, KEK).link_token()
