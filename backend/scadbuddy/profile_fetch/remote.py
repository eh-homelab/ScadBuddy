"""The profile-fetch worker's client of the browser service (#6780).

The worker is a Temporal worker and holds the ``profile-fetch`` CRUD credential. It runs no
browser. The browser service (`browser.py`) is a Deployment of its own: it runs Chromium on
3dfilamentprofiles.com and holds the 3DFP login, and it has no route to Temporal or to the
API. `RemoteSite` is `steps.Site` over the service's RPC, so the activities are the same
code whichever side runs the browser.

The RPC, on ``SCADBUDDY_PROFILE_FETCH_BROWSER_URL`` (default port `BROWSER_PORT`), each call
with ``Authorization: Bearer <token>`` (the token read by `intake_worker.runner.read_token`
from the file `TOKEN_FILE_ENV` names, in both processes):

- ``POST /session``: open the browser and sign in, when the service has no signed-in
  browser; else nothing. ``DELETE /session``: close the browser.
- ``POST /spools`` with `SpoolAsk` as JSON: add the filament to My Spools.
- ``POST /export`` with `ExportAsk` as JSON: export one printer model and nozzle. The
  answer is 200, ``application/octet-stream``, the file's UTF-8 bytes (at most
  `wire.MAX_FILE_BYTES`). The bytes travel as the body: never base64, never inside JSON.

Any other answer than 200 fails the step. A failure of the site is 500 with
``{"error": "site", "kind": <kind>}``. The worker reads every answer as untrusted, since a
renderer exploit may control the service: only a `steps.Kind` it knows crosses, and the
worker writes the failure's text itself, from its own ask; no text of the service's reaches
Temporal's history or the run's ``error``. The body is counted while it is read, and refused past
its cap or with a ``Content-Encoding``. A step that does not answer in
`STEP_REQUEST_TIMEOUT` is ``timeout``; a service that cannot be reached is ``browser``
(both retried, `activities.RETRYABLE_KINDS`).
"""

from __future__ import annotations

import asyncio
import json
from types import TracebackType
from typing import Final, Self, get_args

import httpx
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.profile_fetch.steps import ExportedFile, Kind, SiteError
from scadbuddy.profile_fetch.wire import MAX_FILE_BYTES

#: The browser service's port (``SCADBUDDY_PROFILE_FETCH_BROWSER_PORT``).
BROWSER_PORT: Final = 8091
#: Both processes read the RPC token from the file this variable names.
TOKEN_FILE_ENV: Final = "SCADBUDDY_PROFILE_FETCH_BROWSER_TOKEN_FILE"
SESSION: Final = "/session"
SPOOLS: Final = "/spools"
EXPORT: Final = "/export"
#: The longest one step may take on the service: an export whose page asks for the login
#: again signs in inside the step (`ThreeDfpSite._open`). Under the export activity's
#: start-to-close (`workflow.EXPORT_TIMEOUT`, 20 min), so the attempt ends by its own read.
STEP_REQUEST_TIMEOUT: Final = 600.0
CONNECT_TIMEOUT: Final = 10.0
#: The largest answer of a step that returns no file (an error's JSON included), and the
#: largest ask the service reads.
MAX_ERROR_BYTES: Final = 4096
MAX_ASK_BYTES: Final = 4096
KINDS: Final[frozenset[str]] = frozenset(get_args(Kind))


class SpoolAsk(BaseModel):
    """``POST /spools``: the filament's 3DFP id."""

    model_config = ConfigDict(extra="forbid")

    tdfp_id: int = Field(gt=0)


class ExportAsk(SpoolAsk):
    """``POST /export``: the filament, the printer model and the nozzle (``"0.4"``)."""

    printer_model: str = Field(min_length=1, max_length=200)
    nozzle: str = Field(min_length=1, max_length=16)


class RemoteSite:
    """`steps.Site` over the browser service's RPC (the module docstring). Use it as an async
    context manager; its exit closes the service's browser."""

    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": f"Bearer {self._token}"},
            # `_call`'s `asyncio.timeout` bounds each whole step; this keeps httpx's own
            # connect limit and a read limit no step can outlast (#6829).
            timeout=httpx.Timeout(STEP_REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT),
            transport=self._transport,
            follow_redirects=False,
            # No proxy from the environment: the service is a pod on the pod network.
            trust_env=False,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            await self._call("DELETE", SESSION, None, MAX_ERROR_BYTES, "")
        except SiteError:
            pass
        finally:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    async def login(self) -> None:
        await self._call("POST", SESSION, None, MAX_ERROR_BYTES, "")

    async def ensure_in_my_spools(self, tdfp_id: int) -> None:
        context = f"3DFP filament {tdfp_id}"
        await self._call("POST", SPOOLS, SpoolAsk(tdfp_id=tdfp_id), MAX_ERROR_BYTES, context)

    async def export(self, tdfp_id: int, printer_model: str, nozzle: str) -> ExportedFile:
        context = f"{printer_model} {nozzle} mm"
        ask = ExportAsk(tdfp_id=tdfp_id, printer_model=printer_model, nozzle=nozzle)
        body = await self._call("POST", EXPORT, ask, MAX_FILE_BYTES, context)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise SiteError(
                "browser", f"{context}: the browser service's file is not UTF-8"
            ) from None
        # The site's download name never crosses: the worker uses only the bytes.
        return ExportedFile(name="", text=text)

    async def _call(
        self, method: str, path: str, ask: BaseModel | None, cap: int, context: str
    ) -> bytes:
        """The answer's body of a 200; `SiteError` for any other answer (the module
        docstring), its text the worker's own."""
        if self._client is None:
            raise RuntimeError("RemoteSite is used outside its `async with`")
        content = None if ask is None else ask.model_dump_json().encode()
        headers = {} if content is None else {"Content-Type": "application/json"}
        try:
            # The whole step, not each read: httpx's read timeout restarts on every chunk.
            async with (
                asyncio.timeout(STEP_REQUEST_TIMEOUT),
                self._client.stream(method, path, content=content, headers=headers) as answer,
            ):
                if "content-encoding" in answer.headers:
                    raise _malformed(context, "an encoded answer")
                limit = cap if answer.status_code == 200 else MAX_ERROR_BYTES
                body = await _read_capped(answer, limit)
                status = answer.status_code
        except (httpx.TimeoutException, TimeoutError):
            raise _failure(
                "timeout", context, "the browser service did not answer in time"
            ) from None
        except httpx.HTTPError:
            raise _failure("browser", context, "the browser service could not be reached") from None
        if status == 200:
            if body is None:
                if path == EXPORT:
                    raise _failure("file_too_large", context)
                raise _malformed(context, "an answer past its cap")
            return body
        if status == 401:
            raise _failure("browser", context, "the browser service refused the worker's token")
        if status == 500 and body is not None:
            kind = _site_kind(body)
            if kind is not None:
                raise _failure(kind, context)
        raise _failure("browser", context, f"the browser service answered HTTP {status}")


async def _read_capped(answer: httpx.Response, cap: int) -> bytes | None:
    """The body, or None once it passes ``cap`` (the rest is never read)."""
    chunks: list[bytes] = []
    size = 0
    # No Content-Encoding reaches here (`_call` refuses it), so these are the raw bytes.
    async for chunk in answer.aiter_bytes():
        size += len(chunk)
        if size > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _site_kind(body: bytes) -> Kind | None:
    """The kind of a site failure's answer, only when it is one the worker knows."""
    try:
        problem = json.loads(body)
    except ValueError:
        return None
    if not isinstance(problem, dict) or problem.get("error") != "site":
        return None
    kind = problem.get("kind")
    if isinstance(kind, str) and kind in KINDS:
        return kind  # type: ignore[return-value]  # checked against Kind's values above
    return None


def _failure(kind: Kind, context: str, text: str | None = None) -> SiteError:
    message = text or str(SiteError(kind))
    return SiteError(kind, f"{context}: {message}" if context else message)


def _malformed(context: str, what: str) -> SiteError:
    return _failure("browser", context, f"the browser service sent {what}")
