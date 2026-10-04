"""A duplicate's upstream (#157): whether it has moved, the merge it would make, and
taking, dismissing or detaching it. The work is in :mod:`scadbuddy.library.upstream`."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from scadbuddy.api.deps import AppState, CatalogueDep, OperationsDep, SlugPath
from scadbuddy.api.models import announce_source_change, require_mine
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    IdempotencyKey,
    operation_answer,
    run_operation,
)
from scadbuddy.core.events import ModelEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import ModelNotFoundError, ModelRecord
from scadbuddy.library.history import GitError, GitUnavailableError
from scadbuddy.library.upstream import (
    MergeConflictError,
    MergePlan,
    NoUpstreamError,
    UpstreamStateError,
    UpstreamStatus,
)

router = APIRouter(tags=["models"])


class UpstreamMerge(BaseModel):
    model: ModelRecord = Field(description="The template after the merge")
    taken: list[str] = Field(
        default_factory=list, description="Other files taken from the upstream"
    )
    kept: list[str] = Field(
        default_factory=list,
        description="Other files changed both here and upstream since `base`: ours were kept",
    )


def _answer[T](slug: str, action: Callable[[], T]) -> T:
    """Run ``action``, with every upstream route's failures as problem documents."""
    try:
        return action()
    except ModelNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
    except NoUpstreamError:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"{slug!r} is not a duplicate, so it has no upstream"
        ) from None
    except UpstreamStateError as error:
        raise ApiError(status.HTTP_409_CONFLICT, str(error), state=error.state) from None
    except MergeConflictError as error:
        preview = error.plan.preview
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"the merge into {slug!r} has {error.plan.conflicts} conflict(s); resolve them "
            f"and save with PUT /models/{slug}/source?merge_base={error.plan.revision}",
            merged=preview.merged,
            merge_base=error.plan.revision,
            conflicts=error.plan.conflicts,
            taken=preview.taken,
            kept=preview.kept,
        ) from None
    except GitUnavailableError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "model history is unavailable: no git repository under the models directory",
        ) from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None


@router.get(
    "/models/{slug}/upstream",
    response_model=UpstreamStatus,
    summary="A duplicate's upstream: state and merge preview",
    description=(
        "`state` is `current` (this template includes the upstream's current revision), "
        "`update` (the upstream has moved), `dismissed` (it has moved, to the revision "
        "the user dismissed; still mergeable) or `gone` (the upstream no longer "
        "exists). On `update` and `dismissed`, "
        "`preview` carries `ours`, `base`, `theirs` and the `git merge-file -p --diff3` "
        "result, plus which other files would follow the upstream (`taken`) and which "
        "would stay because both sides changed them (`kept`). 404 for a template that "
        "is not a duplicate."
    ),
)
def get_upstream(slug: SlugPath, catalogue: CatalogueDep) -> UpstreamStatus:
    return _answer(slug, lambda: catalogue.upstream_status(slug))


@router.post(
    "/models/{slug}/upstream/merge",
    response_model=UpstreamMerge,
    summary="Merge the upstream's current revision",
    description=(
        "Three-way merges the upstream's current `model.scad` into this one's, with "
        "`base` as the merge base. Clean: writes it, takes each other file this "
        "template has not changed since `base`, sets `base` to the upstream's revision "
        "and clears `dismissed`, as one commit `Merge <upstream id> into <slug>`. "
        "Conflicted: 409 with the marked-up source as `merged` and the revision to "
        "save the resolution against as `merge_base`; nothing is written. A dismissed "
        "update merges the same way. 409 is one of three cases, told apart by the "
        "problem's fields: a conflict carries `merged` and `merge_base`; no update to "
        "merge carries `state` `current` or `gone`; and a template or upstream that kept "
        "changing across every attempt to write the merge carries `state` `update` or "
        "`dismissed` and is worth retrying."
    ),
    responses={
        **OPERATION_RESPONSES,
        status.HTTP_409_CONFLICT: {
            "description": (
                "The merge conflicts (`merged`, `merge_base`, `conflicts`: resolve it in "
                "the editor), there is no update to merge (`state` is `current` or "
                "`gone`), or the template or its upstream kept changing while the merge "
                "was worked out (`state` is `update` or `dismissed`: retry)"
            )
        },
    },
)
async def merge_upstream(
    slug: SlugPath,
    response: Response,
    ops: OperationsDep,
    catalogue: CatalogueDep,
    idempotency_key: IdempotencyKey = None,
) -> UpstreamMerge | JSONResponse:
    require_mine(slug)

    def refuse_a_conflict() -> None:
        plan = catalogue.merge_plan(slug)
        if plan is not None and plan.conflicts:
            raise MergeConflictError(plan)

    async def before_start() -> None:
        # Here, not in the operation: the conflict's 409 carries `merged`, the whole
        # three-way result, which has no place in a workflow's history (#1054).
        await asyncio.to_thread(_answer, slug, refuse_a_conflict)

    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["model_upstream_merge"],
        subject=slug,
        request={"slug": slug},
        idempotency_key=idempotency_key,
        before_start=before_start,
    )
    return operation_answer(result, UpstreamMerge)


def merge_run(slug: str, state: AppState) -> UpstreamMerge:
    """The ``model_upstream_merge`` operation's run (#1054). A conflict here means the
    upstream or this template moved after the route found the merge clean: the
    retryable 409 of a merge that kept changing, without `merged`."""

    def merge() -> tuple[ModelRecord, MergePlan]:
        try:
            return state.catalogue.merge_upstream(slug)
        except MergeConflictError:
            raise UpstreamStateError(
                f"{slug!r} or its upstream changed while the merge was checked; merge again",
                state="update",
            ) from None

    record, plan = _answer(slug, merge)
    announce_source_change(state.events, slug)
    return UpstreamMerge(model=record, taken=plan.preview.taken, kept=plan.preview.kept)


@router.post(
    "/models/{slug}/upstream/dismiss",
    response_model=ModelRecord,
    summary="Dismiss the upstream's current revision",
    description=(
        "Sets `dismissed` to the upstream's current revision, so it is no longer "
        "offered; a later upstream revision is. One commit, "
        "`Dismiss <upstream id> update in <slug>`. 409 when there is no update."
    ),
    responses=OPERATION_RESPONSES,
)
async def dismiss_upstream(
    slug: SlugPath,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> ModelRecord | JSONResponse:
    require_mine(slug)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["model_upstream_dismiss"],
        subject=slug,
        request={"slug": slug},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, ModelRecord)


def dismiss_run(slug: str, state: AppState) -> ModelRecord:
    """The ``model_upstream_dismiss`` operation's run (#1054)."""
    record = _answer(slug, lambda: state.catalogue.dismiss_upstream(slug))
    emit(state.events, ModelEvent(kind="model.updated", slug=slug))
    return record


@router.post(
    "/models/{slug}/upstream/detach",
    response_model=ModelRecord,
    summary="Detach from an upstream that is gone",
    description=(
        "Clears `upstream` from a duplicate whose upstream no longer exists, leaving an "
        "ordinary template of mine. One commit, `Detach <slug> from <upstream id>`. "
        "409 while the upstream still exists."
    ),
    responses=OPERATION_RESPONSES,
)
async def detach_upstream(
    slug: SlugPath,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> ModelRecord | JSONResponse:
    require_mine(slug)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["model_upstream_detach"],
        subject=slug,
        request={"slug": slug},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, ModelRecord)


def detach_run(slug: str, state: AppState) -> ModelRecord:
    """The ``model_upstream_detach`` operation's run (#1054)."""
    record = _answer(slug, lambda: state.catalogue.detach_upstream(slug))
    emit(state.events, ModelEvent(kind="model.updated", slug=slug))
    return record
