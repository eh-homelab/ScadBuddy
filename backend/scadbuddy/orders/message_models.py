"""The shapes of an order's customer messages (#5660, spec
2026-11-08-order-notify-customer-design.md §4.2 and §7.1): the binding of one messaging
plugin tool (the stored setting ``orders_customer_messaging``) and one message's record.
Pydantic only, so ``library/settings_store.py`` can import it."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scadbuddy.orders.models import Actor

NotifyTemplate = Literal["quoted", "accepted", "ready", "shipped"]
MessageStatus = Literal["sending", "sent", "failed", "unknown", "skipped"]
#: Why a send did not end `sent`: a fixed category, never the plugin's own text.
MessageReason = Literal["plugin_error", "transport", "no_answer", "unavailable"]
#: A message still `sending` this long after its claim is read as `unknown` (§7.2).
STALE_SENDING_MINUTES = 15

#: `remote:<plugin>` or `package:<package>/<server>`: a remote MCP server only (§4.3).
SOURCE = re.compile(
    r"^(remote:[A-Za-z0-9_.-]{1,100}|package:[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})$"
)
ARG = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
#: A `fixed_args` key with one of these words looks like a secret: secrets go in the
#: plugin's header or OAuth. Words are split at `_` and at a lower-to-upper case change,
#: so `smtp_password` and `apiKey` are refused and `author` and `keyword` pass.
SECRET_WORDS = frozenset(
    {
        "pass",
        "passwd",
        "password",
        "passphrase",
        "pwd",
        "token",
        "secret",
        "key",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "credentials",
        # Lowercase compounds a split at `_` or a case change cannot find (#5858 review).
        "apitoken",
        "accesstoken",
        "authtoken",
        "refreshtoken",
        "accesskey",
        "secretkey",
        "privatekey",
        "smtppass",
        "smtppassword",
    }
)
_WORD_BREAK = re.compile(r"_+|(?<=[a-z0-9])(?=[A-Z])")


def looks_secret(key: str) -> bool:
    return any(word.lower() in SECRET_WORDS for word in _WORD_BREAK.split(key))


class BindingArgs(BaseModel):
    """Which argument of the plugin's tool takes each field of a message."""

    model_config = ConfigDict(extra="forbid")

    to: str = Field(pattern=ARG)
    to_shape: Literal["list", "string"] = "list"
    subject: str | None = Field(default=None, pattern=ARG)
    text: str = Field(pattern=ARG)
    idempotency_key: str | None = Field(default=None, pattern=ARG)


class MessagingBinding(BaseModel):
    """One tool of one installed plugin bound to "customer messages" (§4.2). No secret."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(pattern=SOURCE.pattern)
    tool: str = Field(min_length=1, max_length=128)
    contact_kind: str = Field(min_length=1, max_length=40)
    args: BindingArgs
    fixed_args: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=20)

    @field_validator("fixed_args")
    @classmethod
    def _no_secrets(
        cls, value: dict[str, str | int | float | bool]
    ) -> dict[str, str | int | float | bool]:
        for key, entry in value.items():
            if not re.match(ARG, key):
                raise ValueError(f"fixed_args key {key!r} is not an argument name")
            if looks_secret(key):
                raise ValueError(
                    f"fixed_args key {key!r} looks like a secret: put it in the plugin's "
                    "header or OAuth sign-in"
                )
            if isinstance(entry, str) and len(entry) > 500:
                raise ValueError(f"fixed_args {key!r} is longer than 500 characters")
        return value

    @model_validator(mode="after")
    def _no_overlap(self) -> MessagingBinding:
        args = self.args
        named = [
            name
            for name in (args.to, args.subject, args.text, args.idempotency_key)
            if name is not None
        ]
        if len(set(named)) < len(named):
            raise ValueError("args maps two fields to one argument")
        clash = sorted(set(named) & set(self.fixed_args))
        if clash:
            raise ValueError(f"fixed_args names a mapped argument: {', '.join(clash)}")
        return self


class Recipient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=320)


class MessageClaim(BaseModel):
    """What the agent is about to send: the claim that comes before the plugin's call."""

    model_config = ConfigDict(extra="forbid")

    template: NotifyTemplate
    source: str = Field(pattern=SOURCE.pattern)
    tool: str = Field(min_length=1, max_length=128)
    recipients: list[Recipient] = Field(min_length=1, max_length=20)
    subject: str | None = Field(default=None, max_length=300)
    text: str = Field(min_length=1, max_length=10_000)
    #: An earlier message of the order this one sends again (a person's "Send again").
    resend_of: str | None = Field(default=None, max_length=100)


class MessageOutcome(BaseModel):
    """How the plugin's call ended."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["sent", "failed", "unknown"]
    reason: MessageReason | None = None
    provider_message_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _reason_fits(self) -> MessageOutcome:
        if self.status == "sent" and self.reason is not None:
            raise ValueError("a sent message has no reason")
        if self.status != "sent" and self.reason is None:
            raise ValueError(f"a {self.status} message needs a reason")
        return self


class OrderMessage(BaseModel):
    """One message's record. ``status`` reads `unknown` for a `sending` row past
    ``STALE_SENDING_MINUTES``."""

    id: str
    order_id: str
    template: NotifyTemplate
    source: str
    tool: str
    recipients: list[Recipient]
    subject: str | None
    text: str
    status: MessageStatus
    reason: str | None
    provider_message_id: str | None
    episode: int | None
    resend_of: str | None
    by: Actor
    created_at: datetime
    finished_at: datetime | None
