"""The language-server sockets each client holds (#95), as a component
(`core/components.py`).

``SCADBUDDY_LSP_SESSIONS`` caps the openscad-lsp processes the API runs at once; this
caps one client's share of them, so one page (or a script on the LAN) opening sockets in
a loop cannot hold every server and leave other editors without one. A client is who the
telemetry relay's rate limits count (`telemetry/admission.py` `relay_client`,
`bucket_key`): the peer, or the client a peer in ``SCADBUDDY_TRUSTED_PROXIES`` names,
with an IPv6 client counted by its /64.
"""

from __future__ import annotations

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


class LanguageServerClients:
    def __init__(
        self, trusted_proxies: tuple[Network, ...], per_client: int = LSP_SESSIONS_PER_CLIENT
    ) -> None:
        self._trusted_proxies = trusted_proxies
        self._per_client = per_client
        self._held: dict[str, int] = {}

    def client(self, headers: Headers, peer: str | None) -> str:
        """The key a socket from ``peer`` with ``headers`` counts against."""
        return bucket_key(relay_client(headers, peer, self._trusted_proxies))

    def sessions(self, client: str) -> int:
        return self._held.get(bucket_key(client), 0)

    def full(self, client: str) -> bool:
        return self.sessions(client) >= self._per_client

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
    return LanguageServerClients(core.settings.trusted_proxy_networks)


COMPONENT = Component(LANGUAGE_SERVER_CLIENTS, build=_build)
