"""The library pin writes as operations (#1054, spec 2026-10-01 §4.3, the ``library``
row): each route's effect as its kind's run, moved here unchanged, and the refusals
that need no clone, no lookup and no lock as its check, which records nothing.

The rest arrive as the run's failure, recorded as a failed operation: a pin's 502 and
503 (the clone, the lookup), its 422 for a host name that is not public, its 409 for a
re-pin whose entry changed while it cloned, an unpin's 409 and 404 (the entry is not
the invalid one named, or there is none), and the 409 for a model.json that cannot be
read. A removal's refusals (a render's lease, a model's pin, no checkout) are its
check, and are made again under the gate in its run, where one that a pin or a render
caused in between is recorded as a failure.

They run on the ``library`` worker, which is in the API process and holds the data
volume (phase 3a), so they share the API's checkout gate and install semaphore. Every
kind runs once: a git commit is not deduped.

The gate, the semaphore and the render leases are in-process ``asyncio`` primitives:
they hold only while every process that pins, removes or renders from the volume is
this one. Before the ``library`` worker leaves the API process, or the API runs more
than one replica, the gate and the leases must move to Postgres advisory locks (#872
tracks the leases a separate render worker takes).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from functools import partial, wraps
from typing import TYPE_CHECKING, Any

from fastapi import status

from scadbuddy.api.library_pins import check_pin, resolve_pin
from scadbuddy.api.models import require_model_exists
from scadbuddy.core.events import EventBus, LibraryChanged, LibraryRemoved, ModelEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import (
    InvalidModelMetaError,
    LibraryNotDeclaredError,
    LibraryPinChangedError,
    ModelNotFoundError,
    ModelRecord,
)
from scadbuddy.library.history import GitError
from scadbuddy.library.libraries import (
    CLONE_TIMEOUT,
    COMMIT_PATTERN,
    LibraryCheckoutNotFoundError,
    LibraryDeclarationError,
    LibraryError,
    LibraryNotInstalledError,
    ModelLibrary,
    declared_libraries,
)
from scadbuddy.operations.kinds import KindsBuild, OperationKind, to_thread_to_end

if TYPE_CHECKING:
    from scadbuddy.core.components import Components, Core


#: A pin's run: the clone's own limit, plus waiting its turn (installs, a removal holding
#: the gate) and the commit. Past it the run is cancelled before its next step; a clone
#: or commit already in its thread finishes (holding the gate) and a commit can land.
#: The browser and the agent follow a 202 for 15 minutes (#1063), so this plus the run's
#: ``CANCEL_GRACE`` (`workflows/operation_activities.py`) must stay under that.
PIN_TIMEOUT = timedelta(seconds=CLONE_TIMEOUT) + timedelta(minutes=5)


def _answered[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """A model.json or a ``libraries`` declaration that cannot be read, or a pin whose
    checkout is gone, as the 409 every route answers it with (``api/models.py``,
    ``install_library_handlers``), rather than the operation's unexpected 500."""

    @wraps(fn)
    async def answered(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except InvalidModelMetaError as error:
            raise ApiError(
                status.HTTP_409_CONFLICT, str(error), title="Invalid Model Metadata"
            ) from None
        except LibraryNotInstalledError as error:
            raise ApiError(status.HTTP_409_CONFLICT, str(error.args[0])) from None
        except LibraryDeclarationError as error:
            raise ApiError(
                status.HTTP_409_CONFLICT, str(error.args[0]), title="Invalid Library Declaration"
            ) from None

    return answered


def library_changed(events: EventBus, slug: str, name: str) -> None:
    emit(events, LibraryChanged(slug=slug, name=name))
    emit(events, ModelEvent(kind="model.updated", slug=slug))


def library_kinds(state: Core, components: Components) -> list[OperationKind]:
    """The pin kinds, bound to this process's state (read at each call, so a test's
    replaced store is the one used)."""

    def _record(record: ModelRecord) -> dict[str, Any]:
        dumped: dict[str, Any] = record.model_dump(mode="json")
        return dumped

    async def _declared(slug: str, name: str) -> ModelLibrary:
        declared = await asyncio.to_thread(declared_libraries, state.paths.model_dir(slug))
        current = next((entry for entry in declared if entry.name == name), None)
        if current is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND, f"{slug!r} does not declare a library named {name!r}"
            )
        return current

    async def _pin(
        slug: str,
        name: str,
        *,
        url: str | None,
        ref: str | None,
        replacing: ModelLibrary | None = None,
    ) -> dict[str, Any]:
        """Clone ``name`` and record the pin in ``slug``, with the same checks and status
        codes for a first pin and a re-pin. ``replacing`` is the entry a re-pin read:
        the record is refused, a 409, if it changed while the clone ran."""
        try:
            # Held from the clone to the record, so no removal lands in between.
            async with state.checkouts.pinning():
                pin = await resolve_pin(
                    name, url=url, ref=ref, libraries=state.libraries, installs=state.installs
                )
                record = await to_thread_to_end(
                    partial(state.catalogue.pin_library, slug, pin, replacing=replacing),
                    lambda _: library_changed(state.events, slug, name),
                )
        except LibraryPinChangedError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"{slug!r}'s {name!r} was changed or removed while this re-pin ran; "
                "nothing was recorded",
            ) from None
        except ModelNotFoundError:
            # A concurrent delete of the same slug got there first.
            raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
        except GitError as error:
            raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
        return _record(record)

    async def model_check(request: dict[str, Any]) -> dict[str, Any]:
        require_model_exists(state.catalogue, request["slug"])
        return {}

    async def pin_check(request: dict[str, Any]) -> dict[str, Any]:
        require_model_exists(state.catalogue, request["slug"])
        check_pin(
            request["name"], url=request["url"], ref=request["ref"], libraries=state.libraries
        )
        return {}

    async def pin_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        return await _pin(request["slug"], request["name"], url=request["url"], ref=request["ref"])

    async def repin_check(request: dict[str, Any]) -> dict[str, Any]:
        require_model_exists(state.catalogue, request["slug"])
        current = await _declared(request["slug"], request["name"])
        return {"current": current.model_dump(mode="json")}

    async def repin_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        current = ModelLibrary.model_validate(checked["current"])
        return await _pin(
            request["slug"],
            request["name"],
            url=current.url,
            ref=request["ref"] or current.ref,
            replacing=current,
        )

    async def unpin_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        slug, name, index = request["slug"], request["name"], request["index"]
        try:
            record = await to_thread_to_end(
                partial(state.catalogue.unpin_library, slug, name, index=index),
                lambda _: library_changed(state.events, slug, name),
            )
        except LibraryPinChangedError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"{slug!r}'s entry {index} is no longer an invalid {name!r}; nothing was removed",
            ) from None
        except LibraryNotDeclaredError:
            raise ApiError(
                status.HTTP_404_NOT_FOUND, f"{slug!r} does not declare a library named {name!r}"
            ) from None
        except ModelNotFoundError:
            raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
        except GitError as error:
            raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
        return _record(record)

    async def _refuse_removal(name: str, commit: str | None) -> None:
        """A removal's refusals: a render reading the checkout, a model pinning it, or
        no such checkout."""
        libraries = state.libraries
        what = name if commit is None else f"{name} at {commit[:7]}"
        directory = libraries.paths.libraries / name
        if commit is not None:
            directory /= commit
        jobs = state.checkouts.leased(directory)
        if jobs:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"{what} is being read by render job {', '.join(jobs)}; "
                "try again once it has finished",
                jobs=jobs,
            )
        users = await asyncio.to_thread(state.catalogue.library_users, name, commit)
        if users:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"{what} is still pinned by {', '.join(users)}; remove it from "
                f"{'that model' if len(users) == 1 else 'those models'} first",
                models=users,
            )
        if commit is not None and not re.fullmatch(COMMIT_PATTERN, commit):
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"{commit!r} is not a full commit id"
            )
        try:
            installed = await asyncio.to_thread(libraries.installed, name)
        except LibraryError as error:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
        if not any(commit is None or found == commit for _, found in installed):
            raise ApiError(status.HTTP_404_NOT_FOUND, f"no checkout of {what} is on this volume")

    async def remove_check(request: dict[str, Any]) -> dict[str, Any]:
        await _refuse_removal(request["name"], request["commit"])
        return {}

    async def remove_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        name: str = request["name"]
        commit: str | None = request["commit"]
        libraries, checkouts = state.libraries, state.checkouts
        what = name if commit is None else f"{name} at {commit[:7]}"
        # Alone: no pin can find this checkout and record it while it goes, and no
        # render can take a lease on it. The check's refusals again, now that nothing
        # can get in between.
        async with checkouts.removing():
            await _refuse_removal(name, commit)
            try:
                # No model changes -- a removal is refused while one pins it -- so no
                # `model.updated`: only the checkouts on the volume moved.
                await to_thread_to_end(
                    partial(libraries.remove, name, commit),
                    lambda commits: emit(state.events, LibraryRemoved(name=name, commits=commits)),
                )
            except LibraryCheckoutNotFoundError:
                raise ApiError(
                    status.HTTP_404_NOT_FOUND, f"no checkout of {what} is on this volume"
                ) from None
            except LibraryError as error:
                raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
        return {}

    return [
        OperationKind(
            "library_pin",
            _answered(pin_check),
            _answered(pin_run),
            queue="library",
            run_timeout=PIN_TIMEOUT,
        ),
        OperationKind(
            "library_repin",
            _answered(repin_check),
            _answered(repin_run),
            queue="library",
            run_timeout=PIN_TIMEOUT,
        ),
        OperationKind(
            "library_unpin", _answered(model_check), _answered(unpin_run), queue="library"
        ),
        OperationKind(
            "library_remove", _answered(remove_check), _answered(remove_run), queue="library"
        ),
    ]


OPERATION_KINDS: KindsBuild = library_kinds
