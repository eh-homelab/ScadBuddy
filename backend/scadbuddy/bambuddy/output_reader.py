"""What the ``bambuddy`` queue reads about an output (#1060, spec 2026-10-01 §5.5).

Print runs, ``FollowPrint`` and the Bambuddy operation kinds read an output's record,
its stored ``model.3mf`` and two names taken from the model's files. Before #1060 they
read the data volume; the ``scadbuddy-print`` worker mounts none. :class:`OutputReader`
is that read: :class:`LocalOutputs` on the volume (the API, and the in-process worker),
and :class:`RemoteOutputs` through the API's internal routes (``api/internal.py``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import THREE_MF_MEDIA_TYPE
from scadbuddy.bambuddy.part_settings import PartSettings, PartSettingsMap, by_colour
from scadbuddy.bambuddy.project_file import cached_defaults, output_stem
from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE, ApiError
from scadbuddy.crud.client import CrudClient, CrudRefusedError
from scadbuddy.crud.wire import PREFIX
from scadbuddy.library.catalogue import Catalogue, InvalidModelMetaError
from scadbuddy.library.outputs import OutputFiles, OutputMeta, OutputNotFoundError, OutputStore
from scadbuddy.slicing.models import TransferUrl
from scadbuddy.slicing.transfer import TRANSFER_REFUSED
from scadbuddy.store.content import ContentStore, template_title

logger = logging.getLogger(__name__)

#: The API's internal routes the remote reader calls (``api/internal.py``).
INTERNAL_OUTPUTS = "/api/v1/internal/outputs"
#: Each call reads one record or one 3MF from the API's volume.
REMOTE_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
#: RFC 9457's members; the rest of a problem document is its extensions.
PROBLEM_MEMBERS = frozenset({"type", "title", "status", "detail", "instance"})
JSON_MEDIA_TYPE = "application/json"
#: The waits between attempts while the API does not answer at all (45 s in all): a
#: restart under Recreate takes tens of seconds, and one attempt of ``print_check``
#: (``ACCEPT_TIMEOUT``, 60 s) still fits around it.
RESTART_DELAYS = (1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0)


class OutputNaming(BaseModel):
    """What is named from the model's files: the project file's stem (#317) and the
    template's ``print_settings`` as its model.json has them now (#770)."""

    stem: str
    print_settings: dict[str, str] = Field(default_factory=dict)
    #: The template's ``part_print_settings`` as its model.json has them now (#5626),
    #: as this output's slots (``part_settings.by_colour``); ``None`` for none.
    part_settings: PartSettingsMap | None = None
    #: The model.json's refusal of its print settings, when it has one: a print, the
    #: dialog's background slice and a quote refuse on it (409 "Invalid Model
    #: Metadata", #5649), a project file only names.
    invalid_meta: str | None = None
    #: The template's display name (`template_title`): the folder a native run files its
    #: copy under (#3818 tier A, plan 2026-10-18 D6), as the output's filing does.
    title: str | None = None


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
        title = await asyncio.to_thread(
            template_title, self.catalogue.paths.model_source(meta.slug).parent, meta.slug
        )
        settings, declared, refusal = await self._settings(meta)
        if refusal is not None:
            return OutputNaming(stem=stem, invalid_meta=refusal, title=title)
        parts = await asyncio.to_thread(self._part_settings, meta, declared) if declared else None
        return OutputNaming(stem=stem, print_settings=settings, part_settings=parts, title=title)

    async def print_settings(self, meta: OutputMeta) -> tuple[dict[str, str], str | None]:
        """`naming`'s ``print_settings`` and ``invalid_meta`` only (#5662): the dialog's
        check reads no stem, title or part slots."""
        settings, _, refusal = await self._settings(meta)
        return settings, refusal

    async def part_settings(self, meta: OutputMeta) -> PartSettingsMap | None:
        """`naming`'s ``part_settings`` only (#5627): the dialog's check reads them only
        when it asks for the slots' defaults. ``None`` when the model.json refuses them."""
        _, declared, refusal = await self._settings(meta)
        if refusal is not None or not declared:
            return None
        return await asyncio.to_thread(self._part_settings, meta, declared)

    async def _settings(
        self, meta: OutputMeta
    ) -> tuple[dict[str, str], dict[str, PartSettings], str | None]:
        """The template's print settings, its declared part settings and the model.json's
        refusal of either; with a refusal, both are empty."""
        try:
            settings = await asyncio.to_thread(self.catalogue.print_settings, meta.slug)
            declared = await asyncio.to_thread(self.catalogue.part_print_settings, meta.slug)
        except InvalidModelMetaError as error:
            return {}, {}, str(error)
        return settings, declared, None

    def _part_settings(
        self, meta: OutputMeta, declared: dict[str, PartSettings]
    ) -> PartSettingsMap | None:
        """``declared`` as this output's slots (#5626): each colour parameter's value is
        the output's, else the template's default from its cached schema (an output
        from before #4774 left its defaults out). ``None`` for an arranged output: its
        parts come from other outputs or library files, it has no params, and a default
        colour's slot may be another file's part."""
        if self.outputs.arranged_from(meta.id):
            return None
        defaults = cached_defaults(self.outputs.paths, meta.slug)
        values: dict[str, object] = {k: v for k, v in defaults.items() if v is not None}
        values.update(self.outputs.params(meta.id))
        return by_colour(declared, values, meta.colors)


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


class UnexpectedAnswerError(httpx.HTTPError):
    """A success that is not what the internal route answers: the SPA's ``index.html``
    fallback (``static.py``) of a URL that missed the API, say."""


def _media_type(response: httpx.Response) -> str:
    return str(response.headers.get("content-type", ""))


def _is_problem(response: httpx.Response) -> bool:
    return _media_type(response).startswith(PROBLEM_MEDIA_TYPE)


def _answer(response: httpx.Response, media_type: str) -> httpx.Response:
    """A success of ``media_type``, or the API's problem as `ApiError`. Anything else (a
    proxy's 502, a restart, a page that is not the API's) raises as is, so the activity's
    retry policy retries it."""
    if response.is_success:
        if not _media_type(response).startswith(media_type):
            raise UnexpectedAnswerError(
                f"{response.request.url} answered {_media_type(response)!r}, not {media_type!r}"
            )
        return response
    if _is_problem(response):
        raise _problem(response)
    response.raise_for_status()
    return response


def _gone(response: httpx.Response) -> bool:
    """The API's own 404 for the output; any other 404 (a misrouted URL) is an error."""
    return response.status_code == status.HTTP_404_NOT_FOUND and _is_problem(response)


class RemoteOutputs:
    """The reader through the API's cluster-internal routes (``base_url``,
    ``SCADBUDDY_API_INTERNAL_URL``): the print worker's, which mounts no volume."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sleep = sleep
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=REMOTE_TIMEOUT
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str) -> httpx.Response:
        """One GET, retried on a transport error (the API restarting: the README rolls it
        with Recreate) for about `RESTART_DELAYS`; past it the error is the activity's."""
        for delay in RESTART_DELAYS:
            try:
                return await self._client.get(path)
            except httpx.TransportError:
                await self._sleep(delay)
        return await self._client.get(path)

    async def get(self, output_id: str) -> OutputMeta:
        response = await self._get(f"{INTERNAL_OUTPUTS}/{output_id}")
        if _gone(response):
            raise OutputNotFoundError(output_id)
        return OutputMeta.model_validate_json(_answer(response, JSON_MEDIA_TYPE).content)

    async def model_3mf(self, output_id: str) -> bytes | None:
        response = await self._get(f"{INTERNAL_OUTPUTS}/{output_id}/model.3mf")
        if _gone(response):
            return None
        return _answer(response, THREE_MF_MEDIA_TYPE).content

    async def naming(self, meta: OutputMeta) -> OutputNaming:
        response = await self._get(f"{INTERNAL_OUTPUTS}/{meta.id}/naming")
        return OutputNaming.model_validate_json(_answer(response, JSON_MEDIA_TYPE).content)


#: A sliced 3MF streamed from the API: as long as an upload may take.
BLOB_TIMEOUT = httpx.Timeout(600.0, connect=10.0)
#: The print worker's route that mints a read URL (`api/crud_print.py` `transfer_url`).
TRANSFER_URLS = f"/api/v1{PREFIX}/print/transfer-urls"


class BlobMissingAnswerError(LookupError):
    """The API has no blob under the key (its own 404), or no live sliced file to mint a
    read URL of (its 403 ``transfer-refused``)."""


class SlicedBlobs(Protocol):
    """A native slice's 3MF by its blob key (plan 2026-10-16 Task 9): what a LAN print
    sends to the printer."""

    def stream(self, key: str) -> AsyncGenerator[bytes]: ...


class RemoteBlobs:
    """`SlicedBlobs` through the API's internal blob route (`api/internal_blobs.py`), as
    `RemoteOutputs` reads outputs: the print worker holds no store and no key to sign
    with. Each read first asks the API for a URL of its one key (``POST
    …/print/transfer-urls``, #5298 Task 32), which the API mints only for a key its
    content index holds live as a ``slice`` blob. The URL's header carries a bearer
    token, never logged; the URL is fetched with ``client``, which sends no service
    credential."""

    def __init__(self, crud: CrudClient, *, client: httpx.AsyncClient | None = None) -> None:
        self._crud = crud
        self._client = client or httpx.AsyncClient(timeout=BLOB_TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _url(self, key: str) -> TransferUrl:
        """The API's read URL of ``key``. A 403 ``transfer-refused`` (the API holds no live
        sliced file under the key: the content sweep took it) is `BlobMissingAnswerError`,
        which the LAN upload reports as a file no longer stored, the key logged at DEBUG
        only. Any other refusal, such as the API's 503 ``not_configured``, is an
        `ApiError`. The LAN upload retries neither."""
        try:
            response = await self._crud.request("POST", TRANSFER_URLS, json={"key": key})
        except CrudRefusedError as error:
            if error.problem.get("reason") == TRANSFER_REFUSED:
                logger.debug("the API refused a read URL of %s", key)
                raise BlobMissingAnswerError(key) from None
            raise ApiError(
                error.status, str(error.problem.get("detail") or "transfer URL refused")
            ) from None
        return TransferUrl.model_validate_json(response.content)

    async def stream(self, key: str) -> AsyncGenerator[bytes]:
        url = await self._url(key)
        async with self._client.stream("GET", url.url, headers=url.headers) as response:
            if not response.is_success:
                await response.aread()
                if _gone(response):
                    raise BlobMissingAnswerError(key)
                if _is_problem(response):
                    raise _problem(response)
                response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk


@dataclass
class ContentBlobs:
    """`SlicedBlobs` on the API's own content store: the in-process print worker's."""

    content: ContentStore

    async def stream(self, key: str) -> AsyncGenerator[bytes]:
        stat = await self.content.stat(key)
        if stat is None:
            raise BlobMissingAnswerError(key)
        async for chunk in self.content.get(stat.ref):
            yield chunk
