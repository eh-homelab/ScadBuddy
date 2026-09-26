"""Model history: the read and restore side of the git repository under ``data/models``.

Every route here is scoped to one model, so the paths it reports are relative to
that model's directory rather than to the repository root.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field

from scadbuddy.api.deps import CatalogueDep, CommitPath, ConfigDep, HistoryDep, PathsDep, SlugPath
from scadbuddy.api.models import require_mine, require_model_exists
from scadbuddy.core.paths import SOURCE_NAME, model_repo_path
from scadbuddy.core.problems import ApiError
from scadbuddy.library.history import (
    COMMIT_ID_PATTERN,
    FileChange,
    GitError,
    ModelHistory,
    Revision,
    RevisionNotFoundError,
    RevisionRange,
)
from scadbuddy.render.jobs import resolve_source
from scadbuddy.render.runner import cached_schema
from scadbuddy.render.schema import CustomizerSchema

router = APIRouter(tags=["versions"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 500

CommitQuery = Annotated[str | None, Query(pattern=COMMIT_ID_PATTERN)]


class VersionFile(BaseModel):
    """``A``/``M``/``D`` plus the path, relative to the model's own directory."""

    status: str
    path: str


class ModelVersion(BaseModel):
    commit: str
    short: str
    author: str
    date: datetime
    message: str
    files: list[VersionFile] = Field(default_factory=list)
    # True for the revision the model is currently at, which is what a render
    # without an explicit version reads.
    current: bool = False


class VersionDiff(BaseModel):
    slug: str
    base: str
    head: str
    files: list[VersionFile] = Field(default_factory=list)
    patch: str


def require_history(history: ModelHistory) -> ModelHistory:
    if not history.available:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "model history is unavailable: no git repository under the models directory",
        )
    return history


async def _require_revision_async(history: ModelHistory, commit: str) -> str:
    """:func:`_require_revision` for an ``async def`` handler: `git rev-parse` is a
    subprocess, and on the event loop it stalls every render poll with it."""
    return await asyncio.to_thread(_require_revision, history, commit)


def _require_range(history: ModelHistory, base: str | None, head: str) -> RevisionRange:
    """:meth:`ModelHistory.revision_range`, with an unknown endpoint as a 404."""
    try:
        return history.revision_range(base, head)
    except RevisionNotFoundError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, str(error)) from None


def _require_revision(history: ModelHistory, commit: str) -> str:
    try:
        return history.resolve(commit)
    except RevisionNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no revision {commit!r}") from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None


def _relative(changes: list[FileChange], slug: str) -> list[VersionFile]:
    prefix = f"{model_repo_path(slug)}/"
    return [
        VersionFile(
            status=change.status,
            path=change.path[len(prefix) :] if change.path.startswith(prefix) else change.path,
        )
        for change in changes
    ]


def _version(revision: Revision, slug: str, *, current: bool) -> ModelVersion:
    return ModelVersion(
        commit=revision.commit,
        short=revision.short,
        author=revision.author,
        date=revision.date,
        message=revision.message,
        files=_relative(revision.files, slug),
        current=current,
    )


@router.get(
    "/models/{slug}/versions",
    response_model=list[ModelVersion],
    summary="A model's revision history",
)
def list_versions(
    slug: SlugPath,
    catalogue: CatalogueDep,
    history: HistoryDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> list[ModelVersion]:
    require_model_exists(catalogue, slug)
    require_history(history)
    try:
        revisions = history.log(model_repo_path(slug), limit=limit)
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
    # Newest first, so the head of the list is the revision the model is at.
    return [
        _version(revision, slug, current=index == 0) for index, revision in enumerate(revisions)
    ]


@router.get(
    "/models/{slug}/versions/{commit}/source",
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}}},
    summary="A revision's OpenSCAD source",
)
def get_version_source(
    slug: SlugPath, commit: CommitPath, catalogue: CatalogueDep, history: HistoryDep
) -> Response:
    require_model_exists(catalogue, slug)
    require_history(history)
    resolved = _require_revision(history, commit)
    try:
        body = history.show(resolved, f"{model_repo_path(slug)}/{SOURCE_NAME}")
    except RevisionNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} has no source at {commit}") from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
    return Response(content=body, media_type="text/plain; charset=utf-8")


@router.get(
    "/models/{slug}/versions/{commit}/schema",
    response_model=CustomizerSchema,
    summary="A revision's customizer schema",
)
async def get_version_schema(
    slug: SlugPath,
    commit: CommitPath,
    catalogue: CatalogueDep,
    history: HistoryDep,
    paths: PathsDep,
    config: ConfigDep,
) -> CustomizerSchema:
    require_model_exists(catalogue, slug)
    require_history(history)
    resolved = await _require_revision_async(history, commit)
    try:
        source = await resolve_source(slug, resolved, paths=paths, history=history)
    except RevisionNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} does not exist at {commit}") from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
    try:
        return await cached_schema(source.scad, source.schema_cache, config=config)
    except FileNotFoundError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "openscad is not available to build the schema"
        ) from None


@router.get(
    "/models/{slug}/versions/{commit}/diff",
    response_model=VersionDiff,
    summary="Diff a revision against another (its parent by default)",
)
def get_version_diff(
    slug: SlugPath,
    commit: CommitPath,
    catalogue: CatalogueDep,
    history: HistoryDep,
    base: CommitQuery = None,
) -> VersionDiff:
    require_model_exists(catalogue, slug)
    require_history(history)
    # Both endpoints are resolved ONCE, here: the patch, the file list and the
    # base echoed back all want the same pair, and the UI asks for a diff on
    # every row click, so re-deriving it per call is latency for nothing. The
    # default base (the revision's parent, or the empty tree at the root) is
    # named rather than left implicit, so the UI can offer "diff against this".
    try:
        revisions = _require_range(history, base, commit)
        patch = history.diff(revisions, model_repo_path(slug))
        files = history.diff_files(revisions, model_repo_path(slug))
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
    return VersionDiff(
        slug=slug,
        base=revisions.base,
        head=revisions.head,
        files=_relative(files, slug),
        patch=patch,
    )


@router.post(
    "/models/{slug}/versions/{commit}/restore",
    response_model=ModelVersion,
    summary="Restore a revision as a new commit",
)
def restore_version(
    slug: SlugPath, commit: CommitPath, catalogue: CatalogueDep, history: HistoryDep
) -> ModelVersion:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    require_history(history)
    resolved = _require_revision(history, commit)
    try:
        created = history.restore(slug, resolved)
    except RevisionNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} does not exist at {commit}") from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
    revisions = history.log(slug, limit=1)
    if not revisions or revisions[0].commit != created:  # pragma: no cover - defensive
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, "the restore left no revision")
    return _version(revisions[0], slug, current=True)
