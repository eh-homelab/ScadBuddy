"""A client of MediaMTX's control API (``/v3/config/paths/*``). The relay gives it each
printer's source URL, access code included, here and nowhere else; the recorder gives it the
relay's URL with the relay read password."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

PAGE = 1000


class MediaMtxError(Exception):
    """MediaMTX refused or missed a path change. It names the path and the status only."""


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

    async def ready(self) -> dict[str, bool]:
        """Each running path's ``ready`` flag, from the runtime list ``/v3/paths/list``
        (fields ``name`` and ``ready``, measured on MediaMTX v1.21.4)."""
        found: dict[str, bool] = {}
        page = 0
        while True:
            response = await self._http.get(
                "/v3/paths/list", params={"page": page, "itemsPerPage": PAGE}
            )
            response.raise_for_status()
            body = response.json()
            for item in body.get("items") or []:
                found[item["name"]] = item.get("ready") is True
            page += 1
            if page >= body.get("pageCount", 1):
                return found

    async def answers(self) -> bool:
        """Whether the control API answers a one-item runtime list with a 2xx."""
        try:
            response = await self._http.get("/v3/paths/list", params={"itemsPerPage": 1})
        except httpx.HTTPError:
            return False
        return response.is_success

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
