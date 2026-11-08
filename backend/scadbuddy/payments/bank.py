"""The shop's bank account, linked through Plaid (#4961). It sits beside the payments seam,
not in it: Plaid opens no payment request. One ``bank_links`` row holds the Plaid keys
and, once linked, the access token, sealed with ``core/secrets.py`` under AAD
``bank_links:<id>``; in the clear it holds Plaid's ids and the account's name, type and
mask only. Plaid is called outside every transaction, as ``Payments.link`` calls a
provider. No exception, log line, ``repr`` or answer here carries a key or the token."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.core.secrets import Envelope, Kek, SealError, open_secret, seal_secret
from scadbuddy.orders.store import Conn, OrderStore
from scadbuddy.payments import plaid
from scadbuddy.payments.credentials import CredentialUnreadableError, KekMissingError
from scadbuddy.payments.plaid import AccountSubtype, Environment, PlaidError, PlaidKeys

logger = logging.getLogger(__name__)

_ROW = (
    "id, environment, secret_sealed, dek_sealed, kek_id, saved_at, item_id, account_id,"
    " account_name, account_type, account_mask, linked_at"
)

#: The longest key, token or id a body takes.
MAX_CHARS = 200
Short = Annotated[str, Field(min_length=1, max_length=MAX_CHARS)]


def bank_aad(row_id: str) -> str:
    return f"bank_links:{row_id}"


class BankNotConfiguredError(LookupError):
    def __init__(self) -> None:
        super().__init__("save the Plaid keys first")


class BankLinkedError(Exception):
    def __init__(self) -> None:
        super().__init__("a bank account is already linked; unlink it first")


class BankNotLinkedError(LookupError):
    def __init__(self) -> None:
        super().__init__("no bank account is linked")


class BankKeys(BaseModel):
    """The shop's Plaid API keys. The answer never repeats them."""

    model_config = ConfigDict(extra="forbid")

    environment: Environment
    client_id: Short = Field(repr=False)
    secret: Short = Field(repr=False)


class BankLinkCreate(BaseModel):
    """What Link's ``onSuccess`` gives the page: the public token and the chosen
    account's id (https://plaid.com/docs/link/web/#onsuccess)."""

    model_config = ConfigDict(extra="forbid")

    public_token: Short = Field(repr=False)
    account_id: Short


class BankAccount(BaseModel):
    """The linked account as Settings shows it: never its number."""

    name: str
    type: AccountSubtype
    #: The last 2-4 characters Plaid gives (``mask``), when it gives them.
    mask: str | None


class BankStatus(BaseModel):
    #: Whether the Plaid keys are saved.
    configured: bool
    environment: Environment | None
    account: BankAccount | None
    linked_at: datetime | None


class LinkToken(BaseModel):
    link_token: str


@dataclass(frozen=True)
class LinkedAccess:
    """What a call on the linked Item needs."""

    keys: PlaidKeys
    access_token: str = field(repr=False)
    item_id: str
    account_id: str


class BankLinks:
    def __init__(self, store: OrderStore, kek: Kek | None) -> None:
        self._store = store
        self._kek = kek

    # -- rows -------------------------------------------------------------------------

    def _row(self, conn: Conn, *, lock: bool = False) -> dict[str, Any] | None:
        return conn.execute(
            f"SELECT {_ROW} FROM bank_links" + (" FOR UPDATE" if lock else "")
        ).fetchone()

    def _read(self) -> dict[str, Any] | None:
        with self._store.transaction() as conn:
            return self._row(conn)

    def _open(self, row: dict[str, Any]) -> dict[str, str]:
        if self._kek is None:
            raise KekMissingError()
        envelope = Envelope(
            secret_sealed=bytes(row["secret_sealed"]),
            dek_sealed=bytes(row["dek_sealed"]),
            kek_id=row["kek_id"],
        )
        try:
            secrets = json.loads(open_secret(self._kek, envelope, bank_aad(str(row["id"]))))
        except (SealError, ValueError):
            logger.warning("the stored Plaid keys cannot be opened")
            raise CredentialUnreadableError("plaid") from None
        if not isinstance(secrets, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in secrets.items()
        ):
            logger.warning("the stored Plaid keys are not a map of strings")
            raise CredentialUnreadableError("plaid")
        return secrets

    def _seal(self, row_id: str, secrets: dict[str, str]) -> Envelope:
        assert self._kek is not None
        return seal_secret(self._kek, json.dumps(secrets, sort_keys=True), bank_aad(row_id))

    def _keys(self, row: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, str], PlaidKeys]:
        if row is None:
            raise BankNotConfiguredError()
        secrets = self._open(row)
        keys = PlaidKeys(
            environment=row["environment"],
            client_id=secrets.get("client_id", ""),
            secret=secrets.get("secret", ""),
        )
        return row, secrets, keys

    # -- reads ------------------------------------------------------------------------

    def status(self) -> BankStatus:
        row = self._read()
        if row is None:
            return BankStatus(configured=False, environment=None, account=None, linked_at=None)
        account = None
        if row["item_id"] is not None:
            account = BankAccount(
                name=row["account_name"], type=row["account_type"], mask=row["account_mask"]
            )
        return BankStatus(
            configured=True,
            environment=row["environment"],
            account=account,
            linked_at=row["linked_at"],
        )

    def access(self) -> LinkedAccess | None:
        """The linked Item's keys and token; None when nothing is linked."""
        row = self._read()
        if row is None or row["item_id"] is None:
            return None
        _, secrets, keys = self._keys(row)
        token = secrets.get("access_token")
        if token is None:
            raise CredentialUnreadableError("plaid")
        return LinkedAccess(
            keys=keys, access_token=token, item_id=row["item_id"], account_id=row["account_id"]
        )

    # -- the keys ---------------------------------------------------------------------

    async def save_keys(self, body: BankKeys) -> BankStatus:
        """Check the keys with a link token, then seal them. Refused while an account is
        linked: its access token belongs to the keys that linked it."""
        if self._kek is None:
            raise KekMissingError()
        row = self._read()
        if row is not None and row["item_id"] is not None:
            raise BankLinkedError()
        keys = PlaidKeys(environment=body.environment, client_id=body.client_id, secret=body.secret)
        await plaid.link_token(keys, user_id=row["id"] if row else "check", webhook=None)
        with self._store.transaction() as conn:
            row = self._row(conn, lock=True)
            if row is not None and row["item_id"] is not None:
                raise BankLinkedError()
            row_id = str(row["id"]) if row else str(uuid.uuid4())
            sealed = self._seal(row_id, {"client_id": body.client_id, "secret": body.secret})
            conn.execute(
                "INSERT INTO bank_links (id, environment, secret_sealed, dek_sealed, kek_id)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET environment ="
                " EXCLUDED.environment, secret_sealed = EXCLUDED.secret_sealed, dek_sealed ="
                " EXCLUDED.dek_sealed, kek_id = EXCLUDED.kek_id, saved_at = clock_timestamp()",
                (row_id, body.environment, sealed.secret_sealed, sealed.dek_sealed, sealed.kek_id),
            )
        return self.status()

    def forget_keys(self) -> None:
        with self._store.transaction() as conn:
            row = self._row(conn, lock=True)
            if row is None:
                raise BankNotConfiguredError()
            if row["item_id"] is not None:
                raise BankLinkedError()
            conn.execute("DELETE FROM bank_links WHERE id = %s", (row["id"],))

    # -- the link ---------------------------------------------------------------------

    async def link_token(self) -> str:
        row, _, keys = self._keys(self._read())
        if row["item_id"] is not None:
            raise BankLinkedError()
        return await plaid.link_token(keys, user_id=str(row["id"]), webhook=None)

    async def link(self, public_token: str, account_id: str) -> BankStatus:
        """Exchange Link's public token and keep the chosen account. An account that is
        not checking or savings, or a link that lost a race, removes its Item at Plaid."""
        row, secrets, keys = self._keys(self._read())
        if row["item_id"] is not None:
            raise BankLinkedError()
        exchanged = await plaid.exchange(keys, public_token)
        try:
            account = await plaid.account(keys, exchanged.access_token, account_id)
        except PlaidError:
            await _remove_quietly(keys, exchanged.access_token)
            raise
        with self._store.transaction() as conn:
            now = self._row(conn, lock=True)
            same = (
                now is not None
                and now["item_id"] is None
                and bytes(now["secret_sealed"]) == bytes(row["secret_sealed"])
            )
            if same:
                sealed = self._seal(
                    str(row["id"]), {**secrets, "access_token": exchanged.access_token}
                )
                conn.execute(
                    "UPDATE bank_links SET secret_sealed = %s, dek_sealed = %s, kek_id = %s,"
                    " item_id = %s, account_id = %s, account_name = %s, account_type = %s,"
                    " account_mask = %s, linked_at = clock_timestamp() WHERE id = %s",
                    (
                        sealed.secret_sealed,
                        sealed.dek_sealed,
                        sealed.kek_id,
                        exchanged.item_id,
                        account.account_id,
                        account.name,
                        account.subtype,
                        account.mask,
                        row["id"],
                    ),
                )
        if not same:
            await _remove_quietly(keys, exchanged.access_token)
            raise BankLinkedError()
        return self.status()

    async def unlink(self) -> None:
        """Remove the Item at Plaid, then drop the token. When Plaid refuses, the token
        stays so the shop can try again; an Item Plaid no longer has is removed."""
        linked = self.access()
        if linked is None:
            raise BankNotLinkedError()
        await plaid.remove_item(linked.keys, linked.access_token)
        with self._store.transaction() as conn:
            row = self._row(conn, lock=True)
            if row is None or row["item_id"] != linked.item_id:
                return
            secrets = {k: v for k, v in self._open(row).items() if k != "access_token"}
            sealed = self._seal(str(row["id"]), secrets)
            conn.execute(
                "UPDATE bank_links SET secret_sealed = %s, dek_sealed = %s, kek_id = %s,"
                " item_id = NULL, account_id = NULL, account_name = NULL, account_type ="
                " NULL, account_mask = NULL, linked_at = NULL WHERE id = %s",
                (sealed.secret_sealed, sealed.dek_sealed, sealed.kek_id, row["id"]),
            )


async def _remove_quietly(keys: PlaidKeys, access_token: str) -> None:
    """Remove an Item ScadBuddy will not keep. A failure is logged by its code only."""
    try:
        await plaid.remove_item(keys, access_token)
    except PlaidError as error:
        logger.warning("could not remove an unkept Plaid Item: %s", error.code)
