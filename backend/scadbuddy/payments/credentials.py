"""The shop's provider credentials (#4957). A secret is sealed in its row with
``core/secrets.py`` (the columns ``printers/store.py`` uses for an access code); the
AAD binds the seal to the row and the provider. No exception, log line or ``repr`` here
carries a secret: a failure names the provider only."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel

from scadbuddy.core.secrets import Envelope, Kek, SealError, open_secret, seal_secret
from scadbuddy.orders.store import Conn, OrderStore
from scadbuddy.payments.provider import Credential
from scadbuddy.payments.store import has_open_requests

logger = logging.getLogger(__name__)

_ROW = "provider, account_label, settings, is_default, connected_at, last_webhook_at"


def credential_aad(credential_id: str, provider: str) -> str:
    return f"payment_credentials:{credential_id}:{provider}"


class KekMissingError(Exception):
    def __init__(self) -> None:
        super().__init__("payment credentials need SCADBUDDY_SECRET_KEY_FILE")


class CredentialUnreadableError(Exception):
    """The sealed secret of a provider will not open: another key, or altered bytes."""

    def __init__(self, provider: str) -> None:
        super().__init__(f"the stored credential for {provider} cannot be read")
        self.provider = provider


class ProviderBusyError(Exception):
    """An open payment request still names the provider."""

    def __init__(self, provider: str) -> None:
        super().__init__(f"{provider} has open payment requests")
        self.provider = provider


class ProviderNotConnectedError(LookupError):
    def __init__(self, provider: str) -> None:
        super().__init__(f"{provider} is not connected")
        self.provider = provider


class ProviderRow(BaseModel):
    provider: str
    account_label: str
    settings: dict[str, str]
    is_default: bool
    connected_at: datetime
    last_webhook_at: datetime | None


class PaymentCredentials:
    def __init__(self, store: OrderStore, kek: Kek | None) -> None:
        self._store = store
        self._kek = kek

    @property
    def can_seal(self) -> bool:
        """Whether a KEK is loaded, so ``connect`` can seal a secret."""
        return self._kek is not None

    def _lock(self, conn: Conn) -> None:
        # The table holds a handful of rows; one writer at a time keeps "at most one
        # default" and "the first row is the default" true under concurrent calls.
        conn.execute("LOCK TABLE payment_credentials IN EXCLUSIVE MODE")

    def connect(
        self,
        provider: str,
        *,
        settings: Mapping[str, str],
        secrets: Mapping[str, str],
        account_label: str,
    ) -> ProviderRow:
        """Upsert. The first row connected becomes the default."""
        if self._kek is None:
            raise KekMissingError()
        plaintext = json.dumps(dict(secrets), sort_keys=True)
        with self._store.transaction() as conn:
            self._lock(conn)
            found = conn.execute(
                "SELECT id FROM payment_credentials WHERE provider = %s", (provider,)
            ).fetchone()
            row_id = str(found["id"]) if found else str(uuid.uuid4())
            sealed = seal_secret(self._kek, plaintext, credential_aad(row_id, provider))
            values = (
                account_label,
                json.dumps(dict(settings)),
                sealed.secret_sealed,
                sealed.dek_sealed,
                sealed.kek_id,
            )
            if found:
                row = conn.execute(
                    "UPDATE payment_credentials SET account_label = %s, settings = %s::jsonb,"
                    " secret_sealed = %s, dek_sealed = %s, kek_id = %s, connected_at ="
                    f" clock_timestamp() WHERE id = %s RETURNING {_ROW}",
                    (*values, row_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "INSERT INTO payment_credentials (id, provider, account_label, settings,"
                    " secret_sealed, dek_sealed, kek_id, is_default) VALUES (%s, %s, %s,"
                    " %s::jsonb, %s, %s, %s, NOT EXISTS (SELECT 1 FROM payment_credentials))"
                    f" RETURNING {_ROW}",
                    (row_id, provider, *values),
                ).fetchone()
        assert row is not None
        return ProviderRow.model_validate(row)

    def disconnect(self, provider: str) -> bool:
        """False when the provider was not connected. The default moves to the oldest
        other row."""
        with self._store.transaction() as conn:
            self._lock(conn)
            if has_open_requests(conn, provider):
                raise ProviderBusyError(provider)
            gone = conn.execute(
                "DELETE FROM payment_credentials WHERE provider = %s RETURNING is_default",
                (provider,),
            ).fetchone()
            if gone is None:
                return False
            if gone["is_default"]:
                conn.execute(
                    "UPDATE payment_credentials SET is_default = true WHERE id ="
                    " (SELECT id FROM payment_credentials ORDER BY connected_at, id LIMIT 1)"
                )
            return True

    def credential(self, provider: str) -> Credential | None:
        with self._store.transaction() as conn:
            row = conn.execute(
                "SELECT id, settings, secret_sealed, dek_sealed, kek_id FROM payment_credentials"
                " WHERE provider = %s",
                (provider,),
            ).fetchone()
        if row is None:
            return None
        if self._kek is None:
            raise KekMissingError()
        envelope = Envelope(
            secret_sealed=bytes(row["secret_sealed"]),
            dek_sealed=bytes(row["dek_sealed"]),
            kek_id=row["kek_id"],
        )
        try:
            secrets = json.loads(
                open_secret(self._kek, envelope, credential_aad(str(row["id"]), provider))
            )
        except (SealError, ValueError):
            logger.warning("the stored credential for %s cannot be opened", provider)
            raise CredentialUnreadableError(provider) from None
        if not isinstance(secrets, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in secrets.items()
        ):
            logger.warning("the stored credential for %s is not a map of strings", provider)
            raise CredentialUnreadableError(provider)
        return Credential(settings=dict(row["settings"]), secrets=secrets)

    def rows(self) -> list[ProviderRow]:
        with self._store.transaction() as conn:
            found = conn.execute(
                f"SELECT {_ROW} FROM payment_credentials ORDER BY connected_at, id"
            ).fetchall()
        return [ProviderRow.model_validate(r) for r in found]

    def set_default(self, provider: str) -> None:
        with self._store.transaction() as conn:
            self._lock(conn)
            conn.execute("UPDATE payment_credentials SET is_default = false WHERE is_default")
            moved = conn.execute(
                "UPDATE payment_credentials SET is_default = true WHERE provider = %s",
                (provider,),
            )
            if moved.rowcount == 0:
                raise ProviderNotConnectedError(provider)

    def touch_webhook(self, provider: str) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE payment_credentials SET last_webhook_at = clock_timestamp()"
                " WHERE provider = %s",
                (provider,),
            )
