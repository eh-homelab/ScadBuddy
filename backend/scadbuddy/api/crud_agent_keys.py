"""A key the agent derived from its own KEK, behind a CRUD route (#5299 step 12, D5): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`). With it
the agent mounts no KEK.

Area ``keys``: ``GET …/keys/input-hash``, the approvals' input-hash key, is Go's
(``tools/go/core/internal/secrets``, #7667). This route stays here:

- ``POST …/fetch-grants``: a grant for one fetch off the asset allowlist
  (`library/fetch_grants.py` `mint`), for the agent's ``fetch_asset`` after its approval.
  The key stays here.

It answers 503 ``kek-missing`` when this API holds no KEK. Every CRUD answer is
``no-store``, and no log line here names a key or a grant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Final

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

#: This API holds no KEK.
KEK_MISSING: Final = "kek-missing"
#: The longest host and URL a grant names.
HOST_MAX: Final = 253
URL_MAX: Final = 8192


def _kek(state: Any) -> Kek:
    kek = kek_at(state.settings.secret_key_file)
    if kek is None:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The API has no key-encryption key (SCADBUDDY_SECRET_KEY_FILE).",
            reason=KEK_MISSING,
        )
    return kek


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
