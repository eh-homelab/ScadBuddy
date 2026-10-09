"""The language-server sockets each client holds (#95), as a component
(`core/components.py`).

``SCADBUDDY_LSP_SESSIONS`` caps the openscad-lsp processes the API runs at once; this
caps one client's share of them, so one page (or a script on the LAN) opening sockets in
a loop cannot hold every server and leave other editors without one. A client is who the
telemetry relay's rate limits count (`telemetry/admission.py` `relay_client`,
`bucket_key`): the peer, or the client a peer in ``SCADBUDDY_TRUSTED_PROXIES`` names,
with an IPv6 client counted by its /64.

The cap is on only with ``SCADBUDDY_TRUSTED_PROXIES`` set. Empty, a deployment behind a
gateway sees every socket from the gateway's address, so a per-client cap would be one
cap for every browser together, shrinking the install to ``LSP_SESSIONS_PER_CLIENT``
sessions. Nothing here can tell a gateway from a lone browser, so it is said once at
start instead, and the global cap is the only one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from starlette.datastructures import Headers

from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.proxies import Network
from scadbuddy.telemetry.admission import bucket_key, relay_client

#: Sessions one client may hold at once. An editor holds one; two leaves room for a
#: second tab, or a reconnect that lands before the old socket's close is seen.
LSP_SESSIONS_PER_CLIENT: Final = 2

logger = logging.getLogger(__name__)


class LanguageServerClients:
    def __init__(
        self, trusted_proxies: tuple[Network, ...], per_client: int = LSP_SESSIONS_PER_CLIENT
    ) -> None:
        self._trusted_proxies = trusted_proxies
        self._per_client = per_client
        self._held: dict[str, int] = {}
        #: Whether a key tells clients apart (the module's docstring).
        self.enabled = bool(trusted_proxies)

    def client(self, headers: Headers, peer: str | None) -> str:
        """The key a socket from ``peer`` with ``headers`` counts against."""
        return bucket_key(relay_client(headers, peer, self._trusted_proxies))

    def sessions(self, client: str) -> int:
        # `bucket_key` of a key `client` already bucketed is the same key.
        return self._held.get(bucket_key(client), 0)

    def full(self, client: str) -> bool:
        return self.enabled and self.sessions(client) >= self._per_client

    @contextmanager
    def slot(self, client: str) -> Iterator[None]:
        """Holds one of ``client``'s sessions. No ``await`` may come between `full` and
        this, so two sockets cannot both pass the check for the last one (the same rule
        as ``api/lsp.py`` `_budget_full`)."""
        key = bucket_key(client)
        self._held[key] = self._held.get(key, 0) + 1
        try:
            yield
        finally:
            left = self._held[key] - 1
            if left:
                self._held[key] = left
            else:
                del self._held[key]


LANGUAGE_SERVER_CLIENTS: Key[LanguageServerClients] = Key("language_server_clients")


def _build(core: Core, components: Components) -> LanguageServerClients:
    # A bootstrap setting, never changed after start: parsed once.
    clients = LanguageServerClients(core.settings.trusted_proxy_networks)
    if not clients.enabled:
        logger.info(
            "SCADBUDDY_TRUSTED_PROXIES is empty, so the per-client language-server cap "
            "(%d of SCADBUDDY_LSP_SESSIONS) is off: behind a gateway every browser "
            "would count as one client",
            LSP_SESSIONS_PER_CLIENT,
        )
    return clients


COMPONENT = Component(LANGUAGE_SERVER_CLIENTS, build=_build)
