"""Model history: the read and restore side of the git repository under ``data/models``.

Every route here is scoped to one model, so the paths it reports are relative to
that model's directory rather than to the repository root.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field

from scadbuddy.api.deps import CatalogueDep, CommitPath, ConfigDep, HistoryDep, PathsDep, SlugPath
from scadbuddy.api.models import require_model
from scadbuddy.core.paths import SOURCE_NAME
from scadbuddy.core.problems import ApiError
from scadbuddy.library.history import (
    COMMIT_ID_PATTERN,
    EMPTY_TREE,
    FileChange,
    GitError,
    ModelHistory,
    Revision,
    RevisionNotFoundError,
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


def _require_revision(history: ModelHistory, commit: str) -> str:
    try:
        return history.resolve(commit)
    except RevisionNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no revision {commit!r}") from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None


def _relative(changes: list[FileChange], slug: str) -> list[VersionFile]:
    prefix = f"{slug}/"
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
    require_model(catalogue, slug)
    require_history(history)
    try:
        revisions = history.log(slug, limit=limit)
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
    require_model(catalogue, slug)
    require_history(history)
    resolved = _require_revision(history, commit)
    try:
        body = history.show(resolved, f"{slug}/{SOURCE_NAME}")
    except RevisionNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} has no source at {commit}") from None
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
    require_model(catalogue, slug)
    require_history(history)
    resolved = _require_revision(history, commit)
    try:
        source = await resolve_source(slug, resolved, paths=paths, history=history)
    except RevisionNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} does not exist at {commit}") from None
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
    require_model(catalogue, slug)
    require_history(history)
    head = _require_revision(history, commit)
    resolved_base = _require_revision(history, base) if base else None
    try:
        patch = history.diff(resolved_base, head, slug)
        files = history.diff_files(resolved_base, head, slug)
        # An explicit base is echoed back as given; the default one has to be
        # named, so the UI can offer "diff against this" without guessing.
        effective_base = resolved_base or history.parent(head) or EMPTY_TREE
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
    return VersionDiff(
        slug=slug,
        base=effective_base,
        head=head,
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
    require_model(catalogue, slug)
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
