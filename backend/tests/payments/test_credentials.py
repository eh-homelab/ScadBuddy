"""Sealed payment credentials (#4957). The rows live in Postgres."""

from __future__ import annotations

import logging

import pytest

from scadbuddy.core.secrets import kek_from_base64, seal_secret
from scadbuddy.orders.models import Actor
from scadbuddy.orders.store import OrderStore
from scadbuddy.payments.credentials import (
    CredentialUnreadableError,
    KekMissingError,
    PaymentCredentials,
    ProviderBusyError,
    credential_aad,
)
from scadbuddy.payments.provider import Credential
from scadbuddy.payments.store import insert_request
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

KEK = kek_from_base64("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
OTHER_KEK = kek_from_base64("HxwdHh8gISIjJCUmJygpKissLS4vMDEyMzQ1Njc4OTo=")
BY = Actor(kind="browser")


def _connect(creds: PaymentCredentials, provider: str = "fake", secret: str = "s3cret"):  # type: ignore[no-untyped-def]
    return creds.connect(
        provider,
        settings={"account": "a"},
        secrets={"signing_secret": secret},
        account_label=f"Fake account {provider}",
    )


def test_a_secret_is_sealed_in_its_row_and_opens_back(rows: OrderStore, pool: PgPool) -> None:
    creds = PaymentCredentials(rows, KEK)
    row = _connect(creds)
    with pool.connection() as conn:
        raw = conn.execute("SELECT * FROM payment_credentials").fetchone()
    assert raw is not None
    assert b"s3cret" not in bytes(raw["secret_sealed"]) and raw["kek_id"] == KEK.id
    assert "s3cret" not in repr(raw["settings"]) and "s3cret" not in repr(row)
    cred = creds.credential("fake")
    assert cred == Credential(settings={"account": "a"}, secrets={"signing_secret": "s3cret"})
    assert creds.credential("absent") is None


def test_connect_again_replaces_the_secret_and_keeps_the_default(rows: OrderStore) -> None:
    creds = PaymentCredentials(rows, KEK)
    _connect(creds)
    again = _connect(creds, secret="n3w")
    assert again.is_default and len(creds.rows()) == 1
    cred = creds.credential("fake")
    assert cred is not None and cred.secrets == {"signing_secret": "n3w"}


def test_the_seal_is_bound_to_the_row_and_the_provider(
    rows: OrderStore, pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    creds = PaymentCredentials(rows, KEK)
    _connect(creds)
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO payment_credentials (provider, account_label, secret_sealed,"
            " dek_sealed, kek_id) SELECT 'other', account_label, secret_sealed, dek_sealed,"
            " kek_id FROM payment_credentials WHERE provider = 'fake'"
        )
    with caplog.at_level(logging.DEBUG), pytest.raises(CredentialUnreadableError) as err:
        creds.credential("other")
    assert "other" in str(err.value) and "s3cret" not in str(err.value)
    assert "s3cret" not in caplog.text and "other" in caplog.text


def test_a_row_sealed_under_another_key_is_unreadable(rows: OrderStore) -> None:
    _connect(PaymentCredentials(rows, OTHER_KEK))
    with pytest.raises(CredentialUnreadableError):
        PaymentCredentials(rows, KEK).credential("fake")


@pytest.mark.parametrize("plaintext", ['["s3cret"]', '{"signing_secret": 7}', '"s3cret"'])
def test_a_secret_that_is_not_a_map_of_strings_is_unreadable(
    rows: OrderStore, pool: PgPool, plaintext: str
) -> None:
    creds = PaymentCredentials(rows, KEK)
    _connect(creds)
    with pool.connection() as conn:
        row = conn.execute("SELECT id FROM payment_credentials").fetchone()
        assert row is not None
        sealed = seal_secret(KEK, plaintext, credential_aad(str(row["id"]), "fake"))
        conn.execute(
            "UPDATE payment_credentials SET secret_sealed = %s, dek_sealed = %s",
            (sealed.secret_sealed, sealed.dek_sealed),
        )
    with pytest.raises(CredentialUnreadableError) as e:
        creds.credential("fake")
    assert "s3cret" not in str(e.value) and str(e.value).endswith("for fake cannot be read")


def test_no_kek_refuses_a_connect_and_a_read(rows: OrderStore) -> None:
    with pytest.raises(KekMissingError):
        _connect(PaymentCredentials(rows, None))
    _connect(PaymentCredentials(rows, KEK))
    with pytest.raises(KekMissingError):
        PaymentCredentials(rows, None).credential("fake")


def test_the_first_connected_is_the_default_and_disconnect_moves_it(rows: OrderStore) -> None:
    creds = PaymentCredentials(rows, KEK)
    first = _connect(creds, "fake")
    second = _connect(creds, "alpha")
    third = _connect(creds, "beta")
    assert (first.is_default, second.is_default, third.is_default) == (True, False, False)
    creds.set_default("beta")
    assert {r.provider: r.is_default for r in creds.rows()} == {
        "fake": False, "alpha": False, "beta": True,
    }  # fmt: skip
    assert creds.disconnect("beta") is True
    assert {r.provider: r.is_default for r in creds.rows()} == {"fake": True, "alpha": False}
    assert creds.disconnect("beta") is False


def test_touch_webhook_stamps_the_row(rows: OrderStore) -> None:
    creds = PaymentCredentials(rows, KEK)
    _connect(creds)
    assert creds.rows()[0].last_webhook_at is None
    creds.touch_webhook("fake")
    assert creds.rows()[0].last_webhook_at is not None


def test_disconnect_is_refused_while_a_request_is_open(rows: OrderStore) -> None:
    creds = PaymentCredentials(rows, KEK)
    _connect(creds)
    customer = rows.insert_customer(
        display_name="Ada", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    with rows.transaction() as conn:
        order = rows.insert_order(conn, customer_id=customer.id, currency="USD", created_by=BY)
        insert_request(
            conn, order_id=order.id, kind="deposit", provider="fake", amount_minor=500,
            currency="USD", created_by=BY,
        )  # fmt: skip
    with pytest.raises(ProviderBusyError):
        creds.disconnect("fake")
    assert len(creds.rows()) == 1


def test_the_aad_names_the_row_and_the_provider() -> None:
    assert credential_aad("c1", "fake") == "payment_credentials:c1:fake"
