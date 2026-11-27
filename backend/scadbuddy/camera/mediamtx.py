"""A client of MediaMTX's control API (``/v3/config/paths/*``). The relay gives it each
printer's source URL, access code included, here and nowhere else; the recorders and the live
edge give it the relay's URL with the relay read password (`relay_source`)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from pydantic import SecretStr

PAGE = 1000


class MediaMtxError(Exception):
    """MediaMTX refused or missed a path change. It names the path and the status only."""


def relay_source(relay_url: str, user: str, password: SecretStr, printer_id: str) -> str:
    """The relay's RTSP URL of one printer's path, with the relay read user and password in
    it: ``rtsp://<user>:<pass>@<relay host>/<printer_id>``. Only MediaMTX gets it."""
    parts = urlsplit(relay_url)
    return urlunsplit(
        (
            parts.scheme,
            f"{quote(user, safe='')}:{quote(password.get_secret_value(), safe='')}@{parts.netloc}",
            f"{parts.path.rstrip('/')}/{quote(printer_id, safe='')}",
            "",
            "",
        )
    )


def why(error: httpx.HTTPError) -> str:
    """An httpx error as its type and status: never its request, whose body holds a secret."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"{type(error).__name__} {error.response.status_code}"
    return type(error).__name__


class MediaMtx:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:9997",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=10.0, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def paths(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        page = 0
        while True:
            response = await self._http.get(
                "/v3/config/paths/list", params={"page": page, "itemsPerPage": PAGE}
            )
            response.raise_for_status()
            body = response.json()
            items = body.get("items") or []
            found.update({item["name"]: item for item in items})
            page += 1
            if page >= body.get("pageCount", 1):
                return found

    async def _runtime(self) -> list[dict[str, Any]]:
        """The runtime list ``/v3/paths/list``, every page."""
        found: list[dict[str, Any]] = []
        page = 0
        while True:
            response = await self._http.get(
                "/v3/paths/list", params={"page": page, "itemsPerPage": PAGE}
            )
            response.raise_for_status()
            body = response.json()
            found.extend(body.get("items") or [])
            page += 1
            if page >= body.get("pageCount", 1):
                return found

    async def ready(self) -> dict[str, bool]:
        """Each running path's ``ready`` flag, from the runtime list ``/v3/paths/list``
        (fields ``name`` and ``ready``, measured on MediaMTX v1.21.4)."""
        return {item["name"]: item.get("ready") is True for item in await self._runtime()}

    async def received(self) -> dict[str, int]:
        """Each ready path's ``bytesReceived`` from its source, from the runtime list (the
        field is there on prod's MediaMTX, measured 2026-11-27 UTC). A path that is not ready,
        or lists no count, is left out."""
        return {
            item["name"]: item["bytesReceived"]
            for item in await self._runtime()
            if item.get("ready") is True and isinstance(item.get("bytesReceived"), int)
        }

    async def put_path(self, name: str, conf: dict[str, Any]) -> None:
        """Add the path, or replace it when it exists."""
        quoted = quote(name, safe="")
        response = await self._http.post(f"/v3/config/paths/replace/{quoted}", json=conf)
        if response.status_code == 404:
            response = await self._http.post(f"/v3/config/paths/add/{quoted}", json=conf)
        response.raise_for_status()

    async def delete_path(self, name: str) -> None:
        response = await self._http.delete(f"/v3/config/paths/delete/{quote(name, safe='')}")
        if response.status_code != 404:
            response.raise_for_status()
