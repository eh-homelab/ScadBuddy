"""What the ``bambuddy`` queue reads about an output (#1060, spec 2026-10-01 §5.5).

Print runs, ``FollowPrint`` and the Bambuddy operation kinds read an output's record,
its stored ``model.3mf`` and two names taken from the model's files. Before #1060 they
read the data volume; the ``scadbuddy-print`` worker mounts none. :class:`OutputReader`
is that read: :class:`LocalOutputs` on the volume (the API, and the in-process worker),
and :class:`RemoteOutputs` through the API's internal routes (``api/internal.py``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.project_file import output_stem
from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE, ApiError
from scadbuddy.library.catalogue import Catalogue, InvalidModelMetaError
from scadbuddy.library.outputs import OutputFiles, OutputMeta, OutputNotFoundError, OutputStore

#: The API's internal routes the remote reader calls (``api/internal.py``).
INTERNAL_OUTPUTS = "/api/v1/internal/outputs"
#: Each call reads one record or one 3MF from the API's volume.
REMOTE_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
#: RFC 9457's members; the rest of a problem document is its extensions.
PROBLEM_MEMBERS = frozenset({"type", "title", "status", "detail", "instance"})


class OutputNaming(BaseModel):
    """What is named from the model's files: the project file's stem (#317) and the
    template's ``print_settings`` as its model.json has them now (#770)."""

    stem: str
    print_settings: dict[str, str] = Field(default_factory=dict)
    #: The model.json's refusal of its print settings, when it has one: a print refuses
    #: on it (409 "Invalid Model Metadata"), a project file only names.
    invalid_meta: str | None = None


class OutputReader(OutputFiles, Protocol):
    async def get(self, output_id: str) -> OutputMeta:
        """The output's record; `OutputNotFoundError` when it is gone."""
        ...

    async def naming(self, meta: OutputMeta) -> OutputNaming: ...


async def require(reader: OutputReader, output_id: str) -> OutputMeta:
    """The output, or the 404 a route answers for it (`require_output`)."""
    try:
        return await reader.get(output_id)
    except OutputNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no output with id {output_id!r}") from None


def invalid_meta(detail: str) -> ApiError:
    """As every route that reads a broken model.json answers it (`api/models.py`)."""
    return ApiError(status.HTTP_409_CONFLICT, detail, title="Invalid Model Metadata")


@dataclass
class LocalOutputs:
    """The reader on the data volume; each read in a thread."""

    outputs: OutputStore
    catalogue: Catalogue

    async def get(self, output_id: str) -> OutputMeta:
        return await asyncio.to_thread(self.outputs.get, output_id)

    async def model_3mf(self, output_id: str) -> bytes | None:
        return await self.outputs.model_3mf(output_id)

    async def naming(self, meta: OutputMeta) -> OutputNaming:
        stem = await output_stem(meta, self.outputs, self.catalogue)
        try:
            settings = await asyncio.to_thread(self.catalogue.print_settings, meta.slug)
        except InvalidModelMetaError as error:
            return OutputNaming(stem=stem, invalid_meta=str(error))
        return OutputNaming(stem=stem, print_settings=settings)


def _problem(response: httpx.Response) -> ApiError:
    """The API's problem document as the `ApiError` it was raised from."""
    body: dict[str, Any] = response.json()
    extensions = {k: v for k, v in body.items() if k not in PROBLEM_MEMBERS}
    return ApiError(
        response.status_code,
        str(body.get("detail") or ""),
        title=body.get("title"),
        type_=body.get("type") or "about:blank",
        **extensions,
    )


def _answer(response: httpx.Response) -> httpx.Response:
    """A success, or the API's problem as `ApiError`. Anything else (a proxy's 502, a
    restart) raises as is, so the activity's retry policy retries it."""
    if response.is_success:
        return response
    if response.headers.get("content-type", "").startswith(PROBLEM_MEDIA_TYPE):
        raise _problem(response)
    response.raise_for_status()
    return response


class RemoteOutputs:
    """The reader through the API's cluster-internal routes (``base_url``,
    ``SCADBUDDY_API_INTERNAL_URL``): the print worker's, which mounts no volume."""

    def __init__(self, base_url: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=REMOTE_TIMEOUT
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, output_id: str) -> OutputMeta:
        response = await self._client.get(f"{INTERNAL_OUTPUTS}/{output_id}")
        if response.status_code == status.HTTP_404_NOT_FOUND:
            raise OutputNotFoundError(output_id)
        return OutputMeta.model_validate_json(_answer(response).content)

    async def model_3mf(self, output_id: str) -> bytes | None:
        response = await self._client.get(f"{INTERNAL_OUTPUTS}/{output_id}/model.3mf")
        if response.status_code == status.HTTP_404_NOT_FOUND:
            return None
        return _answer(response).content

    async def naming(self, meta: OutputMeta) -> OutputNaming:
        response = await self._client.get(f"{INTERNAL_OUTPUTS}/{meta.id}/naming")
        return OutputNaming.model_validate_json(_answer(response).content)
