"""The shop's bank account, linked through Plaid (#4961). It sits beside the payments seam,
not in it: Plaid opens no payment request. One ``bank_links`` row holds the Plaid keys
and, once linked, the access token, sealed with ``core/secrets.py`` under AAD
``bank_links:<id>``; in the clear it holds Plaid's ids and the account's name, type and
mask only. Plaid is called outside every transaction, as ``Payments.link`` calls a
provider. No exception, log line, ``repr`` or answer here carries a key or the token."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Sequence
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
    " account_name, account_type, account_mask, linked_at, sync_cursor, synced_at, sync_error"
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


class BankKeysChangedError(Exception):
    def __init__(self) -> None:
        super().__init__(
            "the Plaid keys were replaced or forgotten while the account was being linked;"
            " link it again"
        )


class BankChoiceError(ValueError):
    """Link gave no account, or several: Plaid's Account Select lets the shop pick more
    than one when the Dashboard allows it."""

    def __init__(self) -> None:
        super().__init__("Choose one checking or savings account in Plaid.")


class AccessTokenMissingError(Exception):
    """A linked row whose sealed secrets hold no access token: the keys open, but the
    Item cannot be called."""

    def __init__(self, item_id: str) -> None:
        super().__init__("the linked bank account has no stored access token")
        self.item_id = item_id


class BankNotLinkedError(LookupError):
    def __init__(self) -> None:
        super().__init__("no bank account is linked")


class BankKeys(BaseModel):
    """The shop's Plaid API keys. The answer never repeats them."""

    model_config = ConfigDict(extra="forbid")

    environment: Environment
    client_id: Short = Field(repr=False)
    secret: Short = Field(repr=False)


#: The most account ids a link body takes; one is kept, any other count is refused.
MAX_CHOSEN = 32


class BankLinkCreate(BaseModel):
    """What Link's ``onSuccess`` gives the page: the public token and the ids of
    ``metadata.accounts`` (https://plaid.com/docs/link/web/#onsuccess). The page sends the
    token whatever the count, so an Item made with no account or several is exchanged and
    removed rather than left at Plaid."""

    model_config = ConfigDict(extra="forbid")

    public_token: Short = Field(repr=False)
    account_ids: list[Short] = Field(max_length=MAX_CHOSEN)


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
    #: When the deposit sync last read Plaid (#4961), and Plaid's ``error_code`` when the
    #: last read failed (a fixed code, never Plaid's text).
    synced_at: datetime | None = None
    sync_error: str | None = None


class LinkToken(BaseModel):
    link_token: str


#: What the shop is told when Plaid refused the keys, so the Item could not be removed.
REMOVE_AT_PLAID = (
    "Unlinked here, but Plaid refused the stored keys, so the Item is still at Plaid."
    " Remove it in the Plaid Dashboard."
)
#: What the shop is told when the link held no access token: the keys stay.
TOKEN_MISSING = (
    "Unlinked here, but the link had no stored access token, so the Item may still be at"
    " Plaid. Remove it in the Plaid Dashboard."
)
#: What the shop is told when the keys would not open with this key file: Plaid was never
#: asked, and the keys went with the row.
KEYS_UNREADABLE = (
    "Unlinked here, but the stored Plaid keys could not be opened with this key file, so"
    " the Item is still at Plaid. Remove it in the Plaid Dashboard, then save the keys"
    " again."
)


class BankUnlinked(BaseModel):
    #: False when Plaid refused the keys (the link is dropped here, the keys stay) or
    #: they are sealed under another key file (the row is deleted with its keys).
    removed_at_plaid: bool
    #: What the shop must still do, when anything.
    note: str | None


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

    @property
    def store(self) -> OrderStore:
        return self._store

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
            synced_at=row["synced_at"],
            sync_error=row["sync_error"],
        )

    def access(self) -> LinkedAccess | None:
        """The linked Item's keys and token; None when nothing is linked."""
        row = self._read()
        if row is None or row["item_id"] is None:
            return None
        _, secrets, keys = self._keys(row)
        token = secrets.get("access_token")
        if token is None:
            raise AccessTokenMissingError(row["item_id"])
        return LinkedAccess(
            keys=keys, access_token=token, item_id=row["item_id"], account_id=row["account_id"]
        )

    # -- the keys ---------------------------------------------------------------------

    async def save_keys(self, body: BankKeys) -> BankStatus:
        """Check the keys with a link token, then seal them. Refused while an account is
        linked: its access token belongs to the keys that linked it."""
        if self._kek is None:
            raise KekMissingError()
        row = await asyncio.to_thread(self._read)
        if row is not None and row["item_id"] is not None:
            raise BankLinkedError()
        keys = PlaidKeys(environment=body.environment, client_id=body.client_id, secret=body.secret)
        await plaid.link_token(keys, user_id=row["id"] if row else "check", webhook=None)
        await asyncio.to_thread(self._store_keys, body)
        return await asyncio.to_thread(self.status)

    def _store_keys(self, body: BankKeys) -> None:
        secrets = {"client_id": body.client_id, "secret": body.secret}
        with self._store.transaction() as conn:
            row = self._row(conn, lock=True)
            if row is None:
                row_id = str(uuid.uuid4())
                sealed = self._seal(row_id, secrets)
                inserted = conn.execute(
                    "INSERT INTO bank_links (id, environment, secret_sealed, dek_sealed, kek_id)"
                    " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (singleton) DO NOTHING RETURNING id",
                    (
                        row_id,
                        body.environment,
                        sealed.secret_sealed,
                        sealed.dek_sealed,
                        sealed.kek_id,
                    ),
                ).fetchone()
                if inserted is not None:
                    return
                # A save at the same time inserted the one row first: update it instead,
                # sealed under its own id.
                row = self._row(conn, lock=True)
                assert row is not None
            if row["item_id"] is not None:
                raise BankLinkedError()
            sealed = self._seal(str(row["id"]), secrets)
            conn.execute(
                "UPDATE bank_links SET environment = %s, secret_sealed = %s, dek_sealed = %s,"
                " kek_id = %s, saved_at = clock_timestamp() WHERE id = %s",
                (
                    body.environment,
                    sealed.secret_sealed,
                    sealed.dek_sealed,
                    sealed.kek_id,
                    row["id"],
                ),
            )

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
        row, _, keys = self._keys(await asyncio.to_thread(self._read))
        if row["item_id"] is not None:
            raise BankLinkedError()
        return await plaid.link_token(keys, user_id=str(row["id"]), webhook=None)

    async def link(self, public_token: str, account_ids: Sequence[str]) -> BankStatus:
        """Exchange Link's public token and keep the one chosen account. No account or
        several (`BankChoiceError`), an account that is not checking or savings, or a link
        that lost a race removes its Item at Plaid: the token is exchanged first, so no
        Item Link made is left behind."""
        row, secrets, keys = self._keys(await asyncio.to_thread(self._read))
        if row["item_id"] is not None:
            raise BankLinkedError()
        exchanged = await plaid.exchange(keys, public_token)
        if len(account_ids) != 1:
            await _remove_quietly(keys, exchanged.access_token)
            raise BankChoiceError()
        try:
            account = await plaid.account(keys, exchanged.access_token, account_ids[0])
        except PlaidError:
            await _remove_quietly(keys, exchanged.access_token)
            raise
        try:
            await asyncio.to_thread(self._keep_link, row, secrets, exchanged, account)
        except Exception:
            # The token was never stored (another link, new keys, or a failed write): no
            # Item may be left at Plaid without it.
            await _remove_quietly(keys, exchanged.access_token)
            raise
        return await asyncio.to_thread(self.status)

    def _keep_link(
        self,
        row: dict[str, Any],
        secrets: dict[str, str],
        exchanged: plaid.Exchanged,
        account: plaid.PlaidAccount,
    ) -> None:
        """Store the link. `BankLinkedError` when another link came first,
        `BankKeysChangedError` when the keys were replaced or forgotten meanwhile."""
        with self._store.transaction() as conn:
            now = self._row(conn, lock=True)
            if now is not None and now["item_id"] is not None:
                raise BankLinkedError()
            if now is None or bytes(now["secret_sealed"]) != bytes(row["secret_sealed"]):
                raise BankKeysChangedError()
            sealed = self._seal(str(row["id"]), {**secrets, "access_token": exchanged.access_token})
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

    async def unlink(self) -> BankUnlinked:
        """Remove the Item at Plaid, then drop the token. When Plaid does not answer, the
        token stays so the shop can try again; an Item Plaid no longer has is removed.
        When Plaid refuses the keys themselves (a rotated secret), only the link is
        dropped here and the keys stay (``REMOVE_AT_PLAID``). When the keys are sealed
        under another key file, the row is deleted with its keys (``KEYS_UNREADABLE``):
        the shop removes the Item in the Plaid Dashboard, then saves the keys again. Both
        unlink without the secret, as ``PaymentCredentials.disconnect`` does. No key file
        at all (`KekMissingError`) may be temporary, so it refuses and drops nothing."""
        if self._kek is None:
            raise KekMissingError()
        try:
            linked = await asyncio.to_thread(self.access)
        except CredentialUnreadableError:
            await asyncio.to_thread(self._drop_unreadable)
            return BankUnlinked(removed_at_plaid=False, note=KEYS_UNREADABLE)
        except AccessTokenMissingError as missing:
            # The keys open, so they stay; only the link that cannot be called goes.
            await asyncio.to_thread(self._drop_link, missing.item_id)
            return BankUnlinked(removed_at_plaid=False, note=TOKEN_MISSING)
        if linked is None:
            raise BankNotLinkedError()
        try:
            await plaid.remove_item(linked.keys, linked.access_token)
        except PlaidError as error:
            if error.code not in plaid.KEYS_REFUSED:
                raise
            await asyncio.to_thread(self._drop_link, linked.item_id)
            return BankUnlinked(removed_at_plaid=False, note=REMOVE_AT_PLAID)
        await asyncio.to_thread(self._drop_link, linked.item_id)
        return BankUnlinked(removed_at_plaid=True, note=None)

    def _drop_unreadable(self) -> None:
        """Keys that will not open cannot be sealed again without the token: the row goes."""
        with self._store.transaction() as conn:
            row = self._row(conn, lock=True)
            if row is None or row["item_id"] is None:
                raise BankNotLinkedError()
            logger.warning("unlinked the bank account here: its stored keys will not open")
            conn.execute("DELETE FROM bank_links WHERE id = %s", (row["id"],))
            conn.execute("DELETE FROM bank_deposits WHERE item_id = %s", (row["item_id"],))

    def _drop_link(self, item_id: str) -> None:
        """Forget ``item_id``'s token, account, sync cursor and deposits; the keys stay."""
        with self._store.transaction() as conn:
            row = self._row(conn, lock=True)
            if row is None or row["item_id"] != item_id:
                return
            secrets = {k: v for k, v in self._open(row).items() if k != "access_token"}
            sealed = self._seal(str(row["id"]), secrets)
            conn.execute(
                "UPDATE bank_links SET secret_sealed = %s, dek_sealed = %s, kek_id = %s,"
                " item_id = NULL, account_id = NULL, account_name = NULL, account_type ="
                " NULL, account_mask = NULL, linked_at = NULL, sync_cursor = NULL,"
                " synced_at = NULL, sync_error = NULL WHERE id = %s",
                (sealed.secret_sealed, sealed.dek_sealed, sealed.kek_id, row["id"]),
            )
            # The Item's deposits and their suggestions go with it.
            conn.execute("DELETE FROM bank_deposits WHERE item_id = %s", (item_id,))


async def _remove_quietly(keys: PlaidKeys, access_token: str) -> None:
    """Remove an Item ScadBuddy will not keep. A failure is logged by its code only."""
    try:
        await plaid.remove_item(keys, access_token)
    except PlaidError as error:
        logger.warning("could not remove an unkept Plaid Item: %s", error.code)
