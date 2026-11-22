"""``python -m scadbuddy.profile_fetch.browser``: the profile-fetch browser service (#6780), a
Deployment of its own.

It runs the headed Chromium on 3dfilamentprofiles.com (`site.ThreeDfpSite`), so every page it
loads is untrusted web content. It is no Temporal worker and no CRUD client: it holds no
proof, no service credential, no database secret and no Temporal or API address, and refuses
to start with any of them (`settings.check_browser_environment`) or with a mounted
ServiceAccount token or secret under `SECRETS_DIR` (`intake_worker.sandbox.mounted_secrets`;
the RPC token's Secret is mounted elsewhere). It holds the 3DFP login, which it alone needs,
since its browser types it into the site's login form, and the RPC token, which authorizes
calls to this service and nothing else. Its NetworkPolicy lets it reach DNS and public TCP
443 only, and lets only the worker's pods reach it. So a renderer that escapes Chromium's own
sandbox reaches the 3DFP account and this service, never Temporal or the CRUD service.

The worker (`worker.py`, `remote.RemoteSite`) drives it over HTTP on
``SCADBUDDY_PROFILE_FETCH_BROWSER_PORT`` (default `remote.BROWSER_PORT`); the RPC is in
`remote`'s docstring. ``GET /healthz`` answers 200 while it serves, with no token. Every other
call must carry the token (``Authorization: Bearer <token>``, compared with
`hmac.compare_digest`): without it the answer is 401 before any byte of the body is read. One
step runs at a time (a lock), on one signed-in browser (`steps.SiteHolder`). When the worker
goes away during a step (its activity was cancelled or timed out), the service cancels the
step and closes the browser, since the page may be wedged mid-export.

At start it serves, then signs in. A login refused on three tries (`LOGIN_RETRY_DELAYS`
apart), or a browser that does not launch (Chromium's sandbox cannot start, plan
Decision 6), ends the process with 1: every fetch would fail the same way. A bot check, a
timeout or a browser failure after the launch is logged, and the first call signs in again.
It sets ``RLIMIT_CORE`` to 0 before any browser launches, and makes itself non-dumpable
(`hardening.forbid_dumping`).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import resource
import signal
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ValidationError

from scadbuddy.core.logging import configure_logging
from scadbuddy.intake_worker.hardening import forbid_dumping
from scadbuddy.intake_worker.sandbox import (
    HEAD_TIMEOUT,
    MAX_HEAD_BYTES,
    _BadHeadError,
    _parse_head,
    _send,
    mounted_secrets,
)
from scadbuddy.profile_fetch.remote import (
    EXPORT,
    MAX_ASK_BYTES,
    SESSION,
    SPOOLS,
    ExportAsk,
    SpoolAsk,
)
from scadbuddy.profile_fetch.settings import (
    BrowserSettings,
    ProfileFetchRefusal,
    check_browser_environment,
    forget_login,
)
from scadbuddy.profile_fetch.site import SITE, ThreeDfpSite
from scadbuddy.profile_fetch.steps import Site, SiteError, SiteHolder
from scadbuddy.profile_fetch.wire import MAX_FILE_BYTES

logger = logging.getLogger(__name__)

#: Where Kubernetes mounts a ServiceAccount token and where secret volumes go by convention:
#: any file here refuses the start (the pod sets ``automountServiceAccountToken: false``).
SECRETS_DIR: Path = Path("/var/run/secrets")
#: How long an ask's body may take to arrive once its head did.
BODY_TIMEOUT: Final = 10.0
BROWSER_DID_NOT_START: Final = "the browser did not start"
#: The pauses between the start's sign-in tries after a refused login (#6941): three tries.
LOGIN_RETRY_DELAYS: Final = (10.0, 30.0)

Answer = tuple[int, dict[str, str], bytes]


def forbid_core_dumps() -> None:
    """RLIMIT_CORE 0, soft and hard, for this process and every child it starts (Chromium
    and its renderers inherit it), so a node's ``core_pattern`` never keeps a crashed
    renderer's memory, where the page and the login lived (build Decision 75;
    [getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html), RLIMIT_CORE)."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class StartRefusedError(Exception):
    """The service cannot start: the 3DFP login was refused, or the browser did not
    launch. Its text is fixed and holds no secret."""


async def sign_in_at_start(
    sites: SiteHolder, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
) -> None:
    """Sign in at start (the module docstring). A refused login is tried again after
    each of `LOGIN_RETRY_DELAYS` before it refuses the start: on prod the site refused three
    starts in a row and signed in the fourth, with the same login (#6941)."""
    refused: str | None = None
    for delay in (*LOGIN_RETRY_DELAYS, None):
        refused = None
        try:
            await sites.get()
        except SiteError as error:
            if error.kind == "login_failed":
                refused = "3DFP login failed"
            else:
                # The driver's own text, which never holds the login.
                logger.warning(
                    "3DFP sign-in at start failed (%s): %s; the first call signs in again",
                    error.kind,
                    error,
                )
        except Exception as error:
            # Logged by its class only: a launch error's text can quote the environment.
            logger.error("the browser did not start (%s)", type(error).__name__)
            refused = BROWSER_DID_NOT_START
        if refused != "3DFP login failed" or delay is None:
            break
        logger.warning("3DFP login failed at start; trying again in %.0f s", delay)
        await sleep(delay)
    if refused is not None:
        # Raised outside the handler: the text is fixed, and no chain rides along.
        raise StartRefusedError(refused)


def _json(status: int, body: dict[str, str]) -> Answer:
    return status, {"Content-Type": "application/json"}, json.dumps(body).encode()


def _site_failure(kind: str) -> Answer:
    return _json(500, {"error": "site", "kind": kind})


class BrowserService:
    """The RPC's server (the module docstring) over one `SiteHolder`."""

    def __init__(self, sites: SiteHolder, token: str) -> None:
        self._sites = sites
        self._authorization = f"Bearer {token}".encode("ascii")
        #: One step at a time on the one browser; the start's sign-in holds it too.
        self.lock = asyncio.Lock()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._handle(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as error:
            logger.error("the browser service could not answer a call (%s)", type(error).__name__)
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        except asyncio.LimitOverrunError:
            await _send(writer, *_json(431, {"error": "head-too-large"}))
            return
        except (asyncio.IncompleteReadError, TimeoutError):
            return
        try:
            method, path, headers = _parse_head(head)
        except _BadHeadError:
            await _send(writer, *_json(400, {"error": "bad-head"}))
            return
        if path == "/healthz":
            if method != "GET":
                await _send(writer, *_json(405, {"error": "method"}))
                return
            await _send(writer, 200, {"Content-Type": "text/plain"}, b"ok")
            return
        routes: dict[str, dict[str, Callable[[bytes], Awaitable[Answer]]]] = {
            SESSION: {"POST": self._open, "DELETE": self._close},
            SPOOLS: {"POST": self._spools},
            EXPORT: {"POST": self._export},
        }
        if path not in routes:
            await _send(writer, *_json(404, {"error": "not-found"}))
            return
        step = routes[path].get(method)
        if step is None:
            await _send(writer, *_json(405, {"error": "method"}))
            return
        if not self._authorized(headers):
            # Before any byte of the body is read.
            status, kind, body = _json(401, {"error": "unauthorized"})
            await _send(writer, status, {**kind, "WWW-Authenticate": "Bearer"}, body)
            return
        lengths = headers.get("content-length", [])
        if "transfer-encoding" in headers or len(lengths) > 1:
            await _send(writer, *_json(400, {"error": "bad-length"}))
            return
        length = lengths[0] if lengths else "0"
        if not (length.isascii() and length.isdigit()):
            await _send(writer, *_json(400, {"error": "bad-length"}))
            return
        if int(length) > MAX_ASK_BYTES:
            await _send(writer, *_json(413, {"error": "too-large"}))
            return
        try:
            ask = await asyncio.wait_for(reader.readexactly(int(length)), BODY_TIMEOUT)
        except TimeoutError:
            return
        answer = await self._run(reader, step, ask)
        if answer is not None:
            await _send(writer, *answer)

    def _authorized(self, headers: dict[str, list[str]]) -> bool:
        values = headers.get("authorization", [])
        presented = values[0].encode("latin-1") if len(values) == 1 else b""
        return hmac.compare_digest(presented, self._authorization)

    async def _run(
        self,
        reader: asyncio.StreamReader,
        step: Callable[[bytes], Awaitable[Answer]],
        ask: bytes,
    ) -> Answer | None:
        """The step's answer, one step at a time; None when the worker went away during
        it: the step is cancelled and the browser closed."""
        async with self.lock:
            running = asyncio.ensure_future(step(ask))
            # The ask is read whole; the next read ends only when the worker goes away.
            watcher = asyncio.ensure_future(reader.read(1))
            try:
                await asyncio.wait({running, watcher}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                watcher.cancel()
            if running.done():
                return running.result()
            running.cancel()
            with suppress(BaseException):
                await running
            await self._sites.drop()
            logger.info("the worker went away during a step; the browser is closed")
            return None

    async def _on_site(self, step: Callable[[Site], Awaitable[Answer]]) -> Answer:
        """``step`` on the signed-in browser; a failure as its kind only."""
        try:
            return await step(await self._sites.get())
        except SiteError as error:
            # The driver's own text, which never holds the login.
            logger.warning("a step on the site failed (%s): %s", error.kind, error)
            return _site_failure(error.kind)
        except Exception as error:
            # The class only: a Playwright error's text can quote a filled value.
            logger.warning("a step on the site failed (browser): %s", type(error).__name__)
            return _site_failure("browser")

    async def _open(self, ask: bytes) -> Answer:
        async def signed_in(site: Site) -> Answer:
            return _json(200, {})

        return await self._on_site(signed_in)

    async def _close(self, ask: bytes) -> Answer:
        await self._sites.drop()
        return _json(200, {})

    async def _spools(self, ask: bytes) -> Answer:
        spool = _parse(SpoolAsk, ask)
        if spool is None:
            return _json(400, {"error": "bad-ask"})

        async def add(site: Site) -> Answer:
            await site.ensure_in_my_spools(spool.tdfp_id)
            return _json(200, {})

        return await self._on_site(add)

    async def _export(self, ask: bytes) -> Answer:
        target = _parse(ExportAsk, ask)
        if target is None:
            return _json(400, {"error": "bad-ask"})

        async def export(site: Site) -> Answer:
            exported = await site.export(target.tdfp_id, target.printer_model, target.nozzle)
            body = exported.text.encode("utf-8")
            if len(body) > MAX_FILE_BYTES:
                raise SiteError("file_too_large")
            return 200, {"Content-Type": "application/octet-stream"}, body

        return await self._on_site(export)


def _parse[M: BaseModel](model: type[M], ask: bytes) -> M | None:
    try:
        return model.model_validate_json(ask)
    except ValidationError:
        return None


async def serve(
    settings: BrowserSettings,
    *,
    stop: asyncio.Event,
    open_site: Callable[[], Site] | None = None,
    host: str = "0.0.0.0",
    port: int | None = None,
) -> None:
    """Serves until ``stop``; `StartRefusedError` when the start's sign-in refuses it."""
    # Before any browser launches: no core file of Chromium may outlive a crash.
    forbid_core_dumps()

    def browser() -> Site:
        return ThreeDfpSite(SITE, settings.tdfp_email, settings.tdfp_password, settings.profile_dir)

    sites = SiteHolder(open_site or browser)
    service = BrowserService(sites, settings.browser_token)
    server = await asyncio.start_server(
        service.handle,
        host,
        settings.profile_fetch_browser_port if port is None else port,
        limit=MAX_HEAD_BYTES,
    )
    logger.info("profile-fetch browser service serving on %s", server.sockets[0].getsockname()[1])
    try:
        async with service.lock:
            await sign_in_at_start(sites)
        await stop.wait()
    finally:
        server.close()
        await sites.aclose()


async def _main(settings: BrowserSettings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    await serve(settings, stop=stop)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        check_browser_environment(os.environ)
    except ProfileFetchRefusal as refusal:
        forget_login(os.environ)
        configure_logging()
        for reason in refusal.reasons:
            logger.error(reason)
        return 2
    secrets = mounted_secrets(SECRETS_DIR)
    if secrets:
        forget_login(os.environ)
        configure_logging()
        logger.error(
            "the browser service refuses to start: a mounted secret is present (%s); it holds"
            " no ServiceAccount token (automountServiceAccountToken: false)",
            ", ".join(secrets[:5]),
        )
        return 2
    # Before the login is read: no process of this user may ptrace this one or read its
    # memory, where the 3DFP login and the RPC token live (`hardening.py`).
    forbid_dumping()
    try:
        settings = BrowserSettings()  # type: ignore[call-arg]  # read from the environment
    except ValidationError as invalid:
        forget_login(os.environ)
        configure_logging()
        # Each refused setting by its name and why, never the value: pydantic's own text
        # quotes the input, which could be the login.
        for error in invalid.errors(include_input=False, include_url=False):
            name = ".".join(str(part) for part in error["loc"]) or "settings"
            logger.error("setting %s: %s", name, error["msg"])
        return 2
    # The settings hold the login now: no child process may inherit it.
    forget_login(os.environ)
    configure_logging(settings.log_level)
    try:
        asyncio.run(_main(settings))
    except StartRefusedError as refused:
        logger.error("%s", refused)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
