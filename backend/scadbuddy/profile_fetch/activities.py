"""The profile-fetch worker's activities (#6625 Task 6). The worker holds no database secret
and no Bambuddy key: it reaches the CRUD service's ``profile-fetch`` routes with its service
credential (plan Decision 7). It runs no browser: it reaches 3dfilamentprofiles.com through
the browser service, a pod of its own, with `remote.RemoteSite` (#6780).

`ProfileFetchActivities.export` signs in when the browser is new, adds the filament to My
Spools, and exports each target, PUTting each file's bytes as it comes, named by their
sha256 (``PUT …/runs/{id}/files/{sha256}?printer_model=&nozzle=``). A key the dialog does
not offer (``option_missing``), a file over `MAX_FILE_BYTES` (the cap is held
in `site.py` `_read_export`, beside the page's hook, ``file_too_large``) and a file the API
refuses (422) are that key's failure, reported; the other keys go on. Any other failure of
the site fails the attempt: `site_failure` keeps Decision 13's retry classes and puts only the
driver's own fixed text in the error, never the exception it came from, so neither the
email nor the password reaches Temporal's history or the run's ``error``.
`import_` asks the API to import the run, and `failed` posts why the workflow gave up. A
route that answers the run is not running (409) or gone (404) is `NOT_RUNNING`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Final
from urllib.parse import quote, urlencode

from prometheus_client import Counter
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.core.heartbeat import heartbeating
from scadbuddy.crud.client import CrudClient, CrudRefusedError, activity_key
from scadbuddy.crud.wire import PREFIX
from scadbuddy.profile_fetch.steps import Kind, Site, SiteError, SiteHolder
from scadbuddy.profile_fetch.wire import (
    CREATE_ACTIVITY,
    EXPORT_ACTIVITY,
    FAILED_ACTIVITY,
    IMPORT_ACTIVITY,
    MAX_FAILED_REASON,
    MAX_FAILURE_REASON,
    MAX_FILE_BYTES,
    NOT_RUNNING,
    SEARCH_ACTIVITY,
    CreateAnswer,
    CreateAsk,
    ExportFailure,
    ExportReport,
    FailedAsk,
    FetchAsk,
    ImportOutcome,
    SearchAnswer,
    SearchAsk,
)
from scadbuddy.profile_fetch.workflow import IMPORT_REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

__all__ = ["ProfileFetchActivities", "Site", "SiteHolder", "TdfpActivities", "site_failure"]

BASE: Final = f"/api/v1{PREFIX}/profile-fetch/runs"
#: Decision 13: worth another attempt. A refused login, an unknown 3DFP id, a missing
#: option and a file too large are not: they would fail the same way.
RETRYABLE_KINDS: Final[frozenset[Kind]] = frozenset({"browser", "timeout", "bot_check"})
#: The kinds that are one key's failure, reported, not the attempt's (`_export_one`).
KEY_KINDS: Final[frozenset[Kind]] = frozenset({"option_missing", "file_too_large"})
#: A failure after which the browser is closed and the next attempt launches a new one: a
#: crash, and a timeout, which may be a page wedged with its renderer alive (Task 6
#: review, M1). A bot check keeps the browser, and with it the check's cookie.
FRESH_BROWSER_KINDS: Final[frozenset[Kind]] = frozenset({"browser", "timeout"})
TOO_LARGE: Final = "the exported file is larger than 512 KiB"
#: The generic text of a failure the driver did not map: its own text might quote what the
#: browser was given.
BROWSER_FAILED: Final = "the browser failed on 3dfilamentprofiles.com"

EXPORTS = Counter(
    "scadbuddy_profile_fetch_exports",
    "Profile-fetch export keys by outcome: exported, a failure that skips the key"
    " (option_missing, too_large, refused) or the site failure that failed the attempt.",
    ["outcome"],
)
TDFP_STEPS = Counter(
    "scadbuddy_profile_fetch_tdfp_steps",
    "3DFP searches and creates (#7025) by step and outcome: done, or the site failure's kind.",
    ["step", "outcome"],
)


def site_failure(error: BaseException) -> ApplicationError:
    """The activity's error for a failure of the site: a `SiteError` keeps its kind and
    its text (the driver's own, which never holds the login); anything else is a
    retryable ``browser`` failure of a fixed text. Raise it ``from None``."""
    if isinstance(error, SiteError):
        kind, message = error.kind, str(error)
    else:
        kind, message = "browser", BROWSER_FAILED
    return ApplicationError(message, type=kind, non_retryable=kind not in RETRYABLE_KINDS)


def _not_running(refusal: CrudRefusedError) -> bool:
    return refusal.status == 404 or (
        refusal.status == 409 and refusal.problem.get("code") == NOT_RUNNING
    )


def _run_ended(run_id: str) -> ApplicationError:
    return ApplicationError(
        f"profile-fetch run {run_id} is not running", type=NOT_RUNNING, non_retryable=True
    )


class ProfileFetchActivities:
    def __init__(self, crud: CrudClient, sites: SiteHolder) -> None:
        self._crud = crud
        self._sites = sites

    @activity.defn(name=EXPORT_ACTIVITY)
    async def export(self, ask: FetchAsk) -> ExportReport:
        """The export (the module docstring). It heartbeats while it runs
        (`workflow.EXPORT_HEARTBEAT`). Cancelled by Temporal (a cancel, or its
        start-to-close timeout), it closes the browser as a ``timeout`` does, since the
        page may be wedged mid-export, and ends cancelled."""
        try:
            return await heartbeating(self._export(ask))
        except asyncio.CancelledError:
            await self._sites.drop()
            raise

    async def _export(self, ask: FetchAsk) -> ExportReport:
        exported = 0
        failures: list[ExportFailure] = []
        site = await self._on_site(ask, self._sites.get())
        await self._on_site(ask, site.ensure_in_my_spools(ask.tdfp_id))
        for target in ask.targets:
            for nozzle in target.nozzles:
                reason = await self._export_one(site, ask, target.printer_model, nozzle)
                if reason is None:
                    exported += 1
                    EXPORTS.labels(outcome="exported").inc()
                else:
                    failures.append(
                        ExportFailure(
                            printer_model=target.printer_model,
                            nozzle=nozzle,
                            reason=reason[:MAX_FAILURE_REASON],
                        )
                    )
        return ExportReport(exported=exported, failures=failures)

    async def _on_site[T](self, ask: FetchAsk, step: Awaitable[T]) -> T:
        """``step`` on the site; its failure as `site_failure` says, raised outside the
        handler so the error has no cause or context. A `KEY_KINDS` error passes as it is
        (`_export_one` reports it)."""
        try:
            return await step
        except SiteError as error:
            if error.kind in KEY_KINDS:
                raise
            failure, name = site_failure(error), type(error).__name__
        except Exception as error:
            failure, name = site_failure(error), type(error).__name__
        EXPORTS.labels(outcome=failure.type or "browser").inc()
        if failure.type in FRESH_BROWSER_KINDS:
            # A crashed, broken or wedged browser: the next attempt launches a fresh one.
            await self._sites.drop()
        # The type's name only: the error's own text might quote the login.
        logger.warning("export of run %s failed: %s (%s)", ask.run_id, failure.type, name)
        raise failure

    async def _export_one(
        self, site: Site, ask: FetchAsk, printer_model: str, nozzle: str
    ) -> str | None:
        """One key exported and PUT; why it was skipped, or None."""
        try:
            exported = await self._on_site(ask, site.export(ask.tdfp_id, printer_model, nozzle))
        except SiteError as error:
            EXPORTS.labels(
                outcome="too_large" if error.kind == "file_too_large" else error.kind
            ).inc()
            return str(error)
        # The bytes only: the site's download name is never used as a path.
        body = exported.text.encode()
        if len(body) > MAX_FILE_BYTES:
            EXPORTS.labels(outcome="too_large").inc()
            return TOO_LARGE
        sha256 = hashlib.sha256(body).hexdigest()
        query = urlencode({"printer_model": printer_model, "nozzle": nozzle})
        try:
            await self._crud.request(
                "PUT",
                f"{BASE}/{quote(ask.run_id, safe='')}/files/{sha256}?{query}",
                content=body,
                headers={"Content-Type": "application/json"},
            )
        except CrudRefusedError as refusal:
            if _not_running(refusal):
                raise _run_ended(ask.run_id) from None
            if refusal.status not in (413, 422):
                raise
            EXPORTS.labels(outcome="refused").inc()
            detail = str(refusal.problem.get("detail") or refusal.status)
            return f"ScadBuddy refused the exported file: {detail}"
        return None

    @activity.defn(name=IMPORT_ACTIVITY)
    async def import_(self, run_id: str, report: ExportReport) -> ImportOutcome:
        """``POST …/runs/{id}/import`` with the keys that failed, under a key that is the
        same on each retry, waiting up to `IMPORT_REQUEST_TIMEOUT` for the route; the API's
        outcome."""
        try:
            response = await self._crud.request(
                "POST",
                f"{BASE}/{quote(run_id, safe='')}/import",
                json={"failures": [f.model_dump(mode="json") for f in report.failures]},
                idempotency_key=activity_key(1),
                timeout=IMPORT_REQUEST_TIMEOUT.total_seconds(),
            )
        except CrudRefusedError as refusal:
            if _not_running(refusal):
                raise _run_ended(run_id) from None
            raise
        return ImportOutcome.model_validate(response.json())

    @activity.defn(name=FAILED_ACTIVITY)
    async def failed(self, run_id: str, reason: str) -> None:
        """``POST …/runs/{id}/failed`` once the workflow gave up. A run already ended
        needs nothing."""
        try:
            await self._crud.request(
                "POST",
                f"{BASE}/{quote(run_id, safe='')}/failed",
                json=FailedAsk(reason=reason[:MAX_FAILED_REASON]).model_dump(),
            )
        except CrudRefusedError as refusal:
            if not _not_running(refusal):
                raise
            activity.logger.info("profile-fetch run %s had already ended", run_id)


class TdfpActivities:
    """#7025: the search of 3DFP and the create of a filament on it, each one step on the
    browser service (`steps.Site.search`, `steps.Site.create`). They call no CRUD route:
    the workflow returns the answer to the API that started it. A failure of the site is
    `site_failure`'s, raised with no cause; a crash or a timeout closes the browser, as an
    export's does. The create is never retried (`workflow.CREATE_RETRY`): a failure after
    "Save Filament" is ``not_created``, and the filament may exist."""

    def __init__(self, sites: SiteHolder) -> None:
        self._sites = sites

    @activity.defn(name=SEARCH_ACTIVITY)
    async def search(self, ask: SearchAsk) -> SearchAnswer:
        return await self._run("search", lambda site: site.search(ask))

    @activity.defn(name=CREATE_ACTIVITY)
    async def create(self, ask: CreateAsk) -> CreateAnswer:
        return await self._run("create", lambda site: site.create(ask))

    async def _run[T](self, step: str, call: Callable[[Site], Awaitable[T]]) -> T:
        try:
            return await heartbeating(self._on_site(step, call))
        except asyncio.CancelledError:
            await self._sites.drop()
            raise

    async def _on_site[T](self, step: str, call: Callable[[Site], Awaitable[T]]) -> T:
        try:
            answer = await call(await self._sites.get())
        except Exception as error:
            failure, name = site_failure(error), type(error).__name__
        else:
            TDFP_STEPS.labels(step=step, outcome="done").inc()
            return answer
        TDFP_STEPS.labels(step=step, outcome=failure.type or "browser").inc()
        if failure.type in FRESH_BROWSER_KINDS:
            await self._sites.drop()
        # The type's name only: the error's own text might quote the login.
        logger.warning("3DFP %s failed: %s (%s)", step, failure.type, name)
        raise failure
