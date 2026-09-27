"""A duplicate's upstream (#157): whether it has moved, the merge it would make, and
taking, dismissing or detaching it. The work is in :mod:`scadbuddy.library.upstream`."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from scadbuddy.api.deps import CatalogueDep, SlugPath
from scadbuddy.api.models import require_mine
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import ModelNotFoundError, ModelRecord
from scadbuddy.library.history import GitError, GitUnavailableError
from scadbuddy.library.upstream import (
    MergeConflictError,
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
        "the user dismissed) or `gone` (the upstream no longer exists). On `update`, "
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
        "save the resolution against as `merge_base`; nothing is written. 409 too when "
        "there is no update to merge."
    ),
)
def merge_upstream(slug: SlugPath, catalogue: CatalogueDep) -> UpstreamMerge:
    require_mine(slug)
    record, plan = _answer(slug, lambda: catalogue.merge_upstream(slug))
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
)
def dismiss_upstream(slug: SlugPath, catalogue: CatalogueDep) -> ModelRecord:
    require_mine(slug)
    return _answer(slug, lambda: catalogue.dismiss_upstream(slug))


@router.post(
    "/models/{slug}/upstream/detach",
    response_model=ModelRecord,
    summary="Detach from an upstream that is gone",
    description=(
        "Clears `upstream` from a duplicate whose upstream no longer exists, leaving an "
        "ordinary template of mine. One commit, `Detach <slug> from <upstream id>`. "
        "409 while the upstream still exists."
    ),
)
def detach_upstream(slug: SlugPath, catalogue: CatalogueDep) -> ModelRecord:
    require_mine(slug)
    return _answer(slug, lambda: catalogue.detach_upstream(slug))
