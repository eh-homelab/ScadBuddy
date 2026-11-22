"""The two keys the agent derived from its own KEK, behind CRUD routes (#5299 step 12, D5):
under ``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`).
With them the agent mounts no KEK.

Area ``keys``:

- ``GET …/keys/input-hash``: the HMAC key of the approvals' input hashes (agent
  `approvals/service.ts` `inputHash`), HKDF-SHA256 of the KEK with the label the agent used
  (:data:`INPUT_HASH_KEY_INFO`), so a hash written before this route still matches. Every
  agent process must hash with one key: a call prepared on one replica is confirmed on
  another. Never the KEK.
- ``POST …/fetch-grants``: a grant for one fetch off the asset allowlist
  (`library/fetch_grants.py` `mint`), for the agent's ``fetch_asset`` after its approval.
  The key stays here.

Both answer 503 ``kek-missing`` when this API holds no KEK. Every CRUD answer is
``no-store``, and no log line here names a key or a grant.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scadbuddy.agent_data.rotation import kek_at
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.core.secrets import Kek
from scadbuddy.crud.guard import crud_router
from scadbuddy.library.asset_fetch import ascii_host, parse_fetch_url
from scadbuddy.library.fetch_grants import fetch_grant_key, mint
from scadbuddy.library.url_import import ImportRefusedError

router = crud_router("agent")

#: The agent's HKDF label of the input-hash key (`approvals/service.ts`, before #5299 step 12).
INPUT_HASH_KEY_INFO: Final = b"scadbuddy:ai_approvals:input_hash:v1"
#: This API holds no KEK (as `api/crud_agent_secrets.py`).
KEK_MISSING: Final = "kek-missing"
#: The longest host and URL a grant names.
HOST_MAX: Final = 253
URL_MAX: Final = 8192


def input_hash_key(kek: Kek) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=INPUT_HASH_KEY_INFO).derive(
        kek.key
    )


def _kek(state: Any) -> Kek:
    kek = kek_at(state.settings.secret_key_file)
    if kek is None:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The API has no key-encryption key (SCADBUDDY_SECRET_KEY_FILE).",
            reason=KEK_MISSING,
        )
    return kek


class InputHashKey(BaseModel):
    #: The 32-byte key, base64.
    key: str


@router.get("/keys/input-hash", name="crud_agent_input_hash_key")
async def release_input_hash_key(state: StateDep) -> InputHashKey:
    return InputHashKey(key=base64.b64encode(input_hash_key(_kek(state))).decode("ascii"))


class GrantAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: Annotated[str, Field(min_length=1, max_length=HOST_MAX)]
    url: Annotated[str, Field(min_length=1, max_length=URL_MAX)]

    @model_validator(mode="after")
    def _host_is_the_urls(self) -> GrantAsk:
        """The grant names the host the asset route reads from ``url`` (its own parse,
        `assets.py` `_spend_grant`): a grant it would refuse is an agent bug, shown here."""
        try:
            host = ascii_host(parse_fetch_url(self.url))
        except ImportRefusedError as error:
            raise ValueError(str(error)) from None
        if host != self.host:
            raise ValueError(f"host {self.host!r} is not the URL's host {host!r}")
        return self


class Grant(BaseModel):
    grant: str


@router.post("/fetch-grants", name="crud_agent_fetch_grant")
async def mint_fetch_grant(body: GrantAsk, state: StateDep) -> Grant:
    key = fetch_grant_key(_kek(state))
    return Grant(grant=mint(key, body.host, body.url, now=datetime.now(UTC)))
