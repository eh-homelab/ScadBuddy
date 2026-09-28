from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import shutil
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import psycopg
from pydantic import BaseModel, Field, ValidationError, field_validator

from scadbuddy.core.config import DEFAULT_DUPLICATE_STAGING_MAX_AGE
from scadbuddy.core.files import write_atomic
from scadbuddy.core.paths import (
    BUILTIN_DIR,
    BUILTIN_PREFIX,
    LEGACY_PRESETS_NAME,
    MODEL_META_NAME,
    SOURCE_NAME,
    DataPaths,
    is_builtin,
    model_path,
)
from scadbuddy.library.history import (
    GitError,
    GitUnavailableError,
    ModelHistory,
    RevisionNotFoundError,
)
from scadbuddy.library.libraries import (
    InvalidLibraryEntry,
    ModelLibrary,
    entry_name,
    invalid_entries,
)
from scadbuddy.library.media import (
    LEGACY_ID,
    MAX_MEDIA_ITEMS,
    MEDIA_DIR,
    MEDIA_UPLOAD_PREFIX,
    VIDEO_EXTENSIONS,
    MediaItem,
    MediaView,
    StagedMedia,
    content_type_of,
    new_media_id,
    readable_media,
)
from scadbuddy.library.media_store import MediaStore
from scadbuddy.library.presets import PresetStore, TemplatePreset, TemplatePresets
from scadbuddy.library.previews import PreviewStore
from scadbuddy.library.slugs import is_slug
from scadbuddy.library.upstream import (
    InvalidMergeBaseError,
    MergeConflictError,
    MergePlan,
    NoUpstreamError,
    Upstream,
    UpstreamState,
    UpstreamStateError,
    UpstreamStatus,
    plan_merge,
    state_of,
)

if TYPE_CHECKING:
    # Type-only: `library.outputs` reaches this module again through
    # `render.provenance`, so a runtime import here would be circular.
    from scadbuddy.library.outputs import OutputStore

logger = logging.getLogger(__name__)

THUMBNAIL_NAME = "thumbnail.png"
README_NAME = "README.md"
SYNC_MESSAGE = "Sync built-in templates from the image"
LINK_MESSAGE = "Link seeded templates to their built-ins"
#: A duplicate's staging folder under ``cache/`` (#156, #212).
DUPLICATE_STAGING_PREFIX = "duplicate-"
#: Seconds before a sweep treats a duplicate's staging as abandoned. A copy takes
#: seconds, so anything this old is a crash, not another replica's live copy on a
#: shared ``/data``. The default; ``SCADBUDDY_DUPLICATE_STAGING_MAX_AGE`` sets it.
DUPLICATE_STAGING_MAX_AGE = DEFAULT_DUPLICATE_STAGING_MAX_AGE

#: How many times a merge is worked out again when the template or its upstream
#: moves between planning and writing it, before it is refused.
MERGE_ATTEMPTS = 3


def _ignore_vanished(function: Any, path: str, error: BaseException) -> None:
    """``rmtree``'s ``onexc``: a file something else already removed is fine."""
    if not isinstance(error, FileNotFoundError):
        raise error


def _remove_tree(path: Path) -> bool:
    """``rmtree`` that logs a real failure rather than raising or hiding it.

    Concurrent deletes and sweeps can race for the same tombstone or orphan, and
    a reused slug may have nothing left to clear, so a path that is already gone,
    or vanishes underneath this one, is not a failure.
    """
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, onexc=_ignore_vanished)
        else:
            path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        if _still_there(path):
            logger.exception("could not remove a path", extra={"path": str(path)})
            return False
    return True


def _still_there(path: Path) -> bool:
    """Whether ``path`` is still present; one that cannot even be checked counts as present."""
    try:
        return path.exists() or path.is_symlink()
    except OSError:
        return True


class _StaleMergeError(Exception):
    """The template or its upstream moved between planning a merge and writing it."""


class ModelNotFoundError(KeyError):
    def __init__(self, slug: str) -> None:
        super().__init__(slug)
        #: The model that is missing, which need not be the one a caller asked
        #: for: a duplicate can lose its upstream or its new copy (#215).
        self.slug = slug


class SidecarNotFoundError(KeyError):
    """The model exists, but the thumbnail or README being removed does not."""


class MediaNotFoundError(KeyError):
    """The model exists, but has no media item (or poster) of that id."""


class MediaOrderError(ValueError):
    """A reorder that is not a permutation of the template's media ids."""


class MediaUnavailableError(RuntimeError):
    """A media write with no database to hold the list (#274): until #401 makes
    ``SCADBUDDY_DATABASE_URL`` required, a deployment may run without one."""


class TooManyMediaError(ValueError):
    """The template already holds :data:`MAX_MEDIA_ITEMS` items."""


class ModelExistsError(ValueError):
    pass


class LibraryNotDeclaredError(KeyError):
    """The model has no library of that name to remove."""


class LibraryPinChangedError(RuntimeError):
    """A re-pin found the model's entry for the library changed, or gone, since it
    was read: another request moved or removed it while the clone ran."""


class InvalidModelMetaError(ValueError):
    """A ``model.json`` on disk that cannot be read as a model's metadata: not JSON,
    or a field of the wrong type -- a hand edit, or a directory placed on the volume
    by hand. It costs its own model only (#179)."""

    def __init__(self, slug: str, reason: str) -> None:
        super().__init__(f"the model.json of {slug!r} is not valid: {reason}")
        self.slug = slug


class ModelMeta(BaseModel):
    """``model.json``: the model's metadata, and nothing derived."""

    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    source: str | None = None
    #: Where the model was imported from (#153), for the link back; None for anything
    #: uploaded, pasted or built in. Not in `ModelPatch`: it records a fact, not a choice.
    origin_url: str | None = None
    #: Set by a duplicate (#156). Not in `ModelPatch` either, so a metadata edit
    #: never clobbers it.
    upstream: Upstream | None = None
    #: The third-party libraries (#93) this model renders with, each pinned for this
    #: model alone: the only ones on its OPENSCADPATH. Not in `ModelPatch`: a pin is
    #: a fetched commit, set by `pin_library`, never typed in.
    libraries: list[ModelLibrary] = Field(default_factory=list)
    #: A built-in's images and videos (#274), in order, as its bundled model.json
    #: ships them. A template of mine keeps its list in `template_media` instead, and
    #: this is never written for one.
    media: list[MediaItem] = Field(default_factory=list)

    @field_validator("media", mode="before")
    @classmethod
    def _readable_media(cls, value: Any) -> list[MediaItem]:
        return readable_media(value)

    @field_validator("libraries", mode="before")
    @classmethod
    def _readable_pins(cls, value: Any) -> Any:
        """Only the entries that are pins. A hand-edited entry that is not one must
        not stop the model listing; its render says what is wrong with it
        (`parse_declaration`), and pinning it again fixes it."""
        if not isinstance(value, list):
            return []
        readable: list[ModelLibrary] = []
        for entry in value:
            with contextlib.suppress(ValidationError):
                readable.append(ModelLibrary.model_validate(entry))
        return readable


#: The model.json fields with a default and no `None` of their own: a `null` for
#: one is the field left out, as a missing one is (#179).
DEFAULTED_META_FIELDS = frozenset({"name", "description", "tags", "libraries", "media"})


def meta_from_raw(raw: dict[str, Any], default_name: str) -> ModelMeta:
    """A model.json's contents as :class:`ModelMeta`, read the same permissive way
    however it arrived -- uploaded, on disk, or from the image.

    A `null` for a defaulted field, or a blank name, falls through to the default
    (the name to ``default_name``). Anything else invalid raises pydantic's
    ``ValidationError`` for the caller to report in its own terms.
    """
    cleaned = {
        key: value
        for key, value in raw.items()
        if not (key in DEFAULTED_META_FIELDS and value is None)
    }
    name = cleaned.get("name")
    if isinstance(name, str) and not name.strip():
        del cleaned["name"]
    return ModelMeta.model_validate({"name": default_name, **cleaned})


class ModelPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    #: The template's own presets (#326), replacing the list whole. Names unique
    #: ignoring case, explicit ids unique; the route writes every key down.
    presets: list[TemplatePreset] | None = None

    @field_validator("presets")
    @classmethod
    def _presets_are_distinct(
        cls, presets: list[TemplatePreset] | None
    ) -> list[TemplatePreset] | None:
        return None if presets is None else TemplatePresets(presets=presets).presets

    @field_validator("name")
    @classmethod
    def _name_is_not_blank(cls, name: str | None) -> str | None:
        """A model is never renamed to nothing. Stored stripped, as the upload
        path stores a name (#179)."""
        if name is None:
            return None
        if not name.strip():
            raise ValueError("the name cannot be blank")
        return name.strip()


#: Where a model's catalogue thumbnail comes from, in order of precedence: ``model``
#: is one set on the model itself, ``output`` the first generated output's plate
#: cover, standing in until one is set (#179), and ``preview`` the plate image of a
#: background render at the default parameters, standing in until either exists.
ThumbnailSource = Literal["model", "output", "preview"]


@dataclass(frozen=True)
class ThumbnailOrigin:
    """Where a model's thumbnail comes from, and which output or preview it is."""

    source: ThumbnailSource | None = None
    output_id: str | None = None
    preview_id: str | None = None


class ModelRecord(ModelMeta):
    # The template's id: a bare slug for mine, `builtin:<slug>` for a built-in.
    slug: str
    origin: Literal["builtin", "mine"]
    #: True when ``GET /models/{slug}/thumbnail`` has an image, from either source.
    has_thumbnail: bool
    has_readme: bool
    thumbnail_source: ThumbnailSource | None = None
    #: The output whose plate image stands in when ``thumbnail_source`` is
    #: ``output``, else None. That fallback moves with no commit (the covering
    #: output is deleted, or an older one gains a cover), so this -- not
    #: ``version`` -- is what tells a client its cached image is stale.
    thumbnail_output_id: str | None = None
    #: Which default-render preview stands in when ``thumbnail_source`` is
    #: ``preview``, else None. It changes when a source edit is re-rendered, again
    #: with no commit of its own, so a client keys its cached image on it too.
    thumbnail_preview_id: str | None = None
    updated_at: datetime
    # The commit this model is currently at, or None when history is unavailable
    # (no git binary). Outputs stamp this as their ``model_version``.
    version: str | None = None
    # Where a duplicate stands against its upstream (#157); None for a template
    # that is not one, or when history is unavailable.
    upstream_state: UpstreamState | None = None
    #: As stored, plus what the disk says of each file. A template with only a
    #: ``thumbnail.png`` lists it as one image, id ``thumbnail``.
    media: list[MediaView] = Field(default_factory=list)  # type: ignore[assignment]
    #: The entries of ``libraries`` in model.json that are not pins, which
    #: ``libraries`` leaves out (#217): what stops the model rendering, and why.
    invalid_libraries: list[InvalidLibraryEntry] = Field(default_factory=list)


class Catalogue:
    """``data/models/<slug>/`` — one directory per model, metadata in a JSON sidecar."""

    def __init__(
        self,
        paths: DataPaths,
        history: ModelHistory | None = None,
        outputs: OutputStore | None = None,
        previews: PreviewStore | None = None,
        duplicate_staging_max_age: float = DUPLICATE_STAGING_MAX_AGE,
        media_store: MediaStore | None = None,
        presets: PresetStore | None = None,
        *,
        serve_previews: bool = True,
        wrapper_prefix: str,
    ) -> None:
        self.paths = paths
        self.history = history
        #: Where a template of mine's media list is; None with no database, when
        #: only a legacy ``thumbnail.png`` is listed and media writes are refused.
        self.media_store = media_store
        #: Where the fallback thumbnail is read from; None turns the fallback off.
        self.outputs = outputs
        #: The default-render previews. Kept clean of gone and reused models even
        #: while `serve_previews` is off, so turning previews back on never serves
        #: an earlier model's image.
        self.previews = previews
        #: Off, the catalogue serves no preview at all -- including ones rendered
        #: while it was on (SCADBUDDY_PREVIEW_RENDERS).
        self.serve_previews = serve_previews
        self.duplicate_staging_max_age = duplicate_staging_max_age
        #: Whose saved presets a template that is gone, or whose slug is reused,
        #: takes with it. None: nothing is saved beside the templates.
        self.presets = presets
        #: A render's colour wrapper file prefix, which a duplicate leaves out.
        self.wrapper_prefix = wrapper_prefix
        #: Called with a model's id after every catalogue change to it, from
        #: whichever thread made the change: how the preview scheduler hears that a
        #: model's source, thumbnail or existence may have changed. Must not raise.
        self.on_change: Callable[[str], None] | None = None

    def notify_change(self, *slugs: str) -> None:
        """Tell :attr:`on_change` about a change made other than through this class
        -- a revision restored straight through the history."""
        if self.on_change is None:
            return
        for slug in slugs:
            # The whole `_builtin/` mirror: the boot-time pass covers every built-in.
            if slug == BUILTIN_DIR:
                continue
            try:
                self.on_change(slug)
            except Exception:
                logger.exception("a catalogue change listener failed", extra={"slug": slug})

    def _commit(self, message: str, *slugs: str) -> str | None:
        """One commit per catalogue action. A failure never fails the action itself:
        the files are already written, and losing the revision is the smaller harm.

        ``OSError`` as well as ``GitError``, because the lock file this takes on
        the way in is ordinary filesystem I/O -- a PVC that has gone read-only or
        full since boot would otherwise 500 a source edit that had already been
        written to disk, telling the client it failed when it did not.
        """
        try:
            if self.history is None or not self.history.available:
                return None
            try:
                return self.history.commit(message, *slugs)
            except (GitError, OSError):
                # NOT `extra={"message": ...}`: `message` is a reserved LogRecord
                # attribute, and logging raises KeyError on the collision -- which
                # would turn this whole tolerate-and-continue branch into the crash
                # it exists to prevent.
                logger.exception("could not record a revision", extra={"revision_message": message})
                return None
        finally:
            # The files are written whether or not the revision was recorded.
            self.notify_change(*slugs)

    def _commit_change(self, message: str, change: Callable[[], None], *slugs: str) -> str | None:
        """Run ``change`` -- a read-modify-write of the template's files -- and commit it,
        both under the history's write lock, so no other catalogue commit's change
        lands between its read and its write and is lost.

        Failures as :meth:`_commit`: once ``change`` has run, a failed commit is
        logged, not raised. One before it (the lock wait) wrote nothing, and raises.
        """
        if self.history is None or not self.history.available:
            change()
            self.notify_change(*slugs)
            return None
        changed = False

        def run() -> None:
            nonlocal changed
            change()
            changed = True

        try:
            return self.history.commit(message, *slugs, prepare=run)
        except (GitError, OSError):
            if not changed:
                raise
            logger.exception("could not record a revision", extra={"revision_message": message})
            return None
        finally:
            if changed:
                self.notify_change(*slugs)

    def version(self, slug: str) -> str | None:
        if self.history is None or not self.history.available:
            return None
        try:
            return self.history.last_commit(model_path(slug))
        except (GitError, OSError):
            logger.exception("could not read the revision", extra={"slug": slug})
            return None

    def versions(self) -> dict[str, str]:
        """Every model's revision in one git call, for listing the catalogue."""
        if self.history is None or not self.history.available:
            return {}
        try:
            return self.history.last_commits()
        except (GitError, OSError):
            logger.exception("could not read the revisions")
            return {}

    def exists(self, slug: str) -> bool:
        return self.paths.model_source(slug).is_file()

    def _require(self, slug: str) -> None:
        if not self.exists(slug):
            raise ModelNotFoundError(slug)

    def thumbnail_path(self, slug: str) -> Path:
        return self.paths.model_dir(slug) / THUMBNAIL_NAME

    def readme_path(self, slug: str) -> Path:
        return self.paths.model_dir(slug) / README_NAME

    def thumbnail_source(self, slug: str, media: list[MediaView] | None = None) -> ThumbnailOrigin:
        """Where the thumbnail comes from: the model's own cover (#274), else the
        first generated output's plate cover, else the default-render preview.
        ``media`` is the template's, when the caller has already listed it."""
        listed = self.list_media(slug) if media is None else media
        if self._cover(slug, listed) is not None:
            return ThumbnailOrigin("model")
        if self.outputs is not None:
            output_id = self.outputs.plate_cover_output(slug)
            if output_id is not None:
                return ThumbnailOrigin("output", output_id=output_id)
        if self.previews is not None and self.serve_previews:
            preview_id = self.previews.preview_id(slug)
            if preview_id is not None:
                return ThumbnailOrigin("preview", preview_id=preview_id)
        return ThumbnailOrigin()

    def has_output_cover(self, slug: str) -> bool:
        return self.outputs is not None and self.outputs.has_plate_cover(slug)

    def _cover(self, slug: str, media: list[MediaView]) -> tuple[Path, str] | None:
        """The template's own cover image and its content type: the first item that
        is an image or a video with a poster. A missing file is passed over, and so
        is a video without a poster -- a card cannot show a frame of it."""
        for item in media:
            if item.missing:
                continue
            if item.kind == "image":
                return self._media_file(slug, item.id, item.file), item.content_type
            if item.poster is not None:
                return self.media_dir(slug) / item.poster, content_type_of(item.poster)
        return None

    def thumbnail(self, slug: str) -> tuple[bytes, str] | None:
        """The catalogue thumbnail and its content type: the template's cover, else
        the first generated output's plate cover, else the default-render preview,
        else None."""
        self._require(slug)
        cover = self._cover(slug, self.list_media(slug))
        if cover is not None:
            with contextlib.suppress(FileNotFoundError):
                return cover[0].read_bytes(), cover[1]
        if self.outputs is not None:
            plate = self.outputs.plate_cover(slug)
            if plate is not None:
                return plate, "image/png"
        if self.previews is None or not self.serve_previews:
            return None
        preview = self.previews.image(slug)
        return (preview, "image/png") if preview is not None else None

    def read_raw_meta(self, slug: str) -> dict[str, Any]:
        meta_path = self.paths.model_meta(slug)
        if not meta_path.is_file():
            return {}
        try:
            loaded: Any = json.loads(meta_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise InvalidModelMetaError(slug, f"not JSON ({error})") from None
        return loaded if isinstance(loaded, dict) else {}

    def _meta(self, slug: str, raw: dict[str, Any]) -> ModelMeta:
        """``raw`` as this model's metadata, or :class:`InvalidModelMetaError`."""
        try:
            return meta_from_raw(raw, slug.removeprefix(BUILTIN_PREFIX))
        except ValidationError as error:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in detail['loc'])}: {detail['msg']}"
                for detail in error.errors()
            )
            raise InvalidModelMetaError(slug, problems) from None
        except RecursionError:
            raise InvalidModelMetaError(slug, "nested too deeply") from None

    def write_raw_meta(self, slug: str, meta: dict[str, Any]) -> None:
        # `schema` is derived and lives under `cache/` (see `SCHEMA_CACHE_NAME`).
        # Dropping it here retires the key from volumes written before that was
        # true, rather than leaving a cache blob in the versioned tree forever.
        meta.pop("schema", None)
        # No `mkdir`: the model directory must already exist (`create` makes it).
        # A write racing a delete then fails instead of recreating a directory
        # holding only `model.json` -- unlisted, and never swept as a tombstone.
        try:
            write_atomic(self.paths.model_meta(slug), (json.dumps(meta, indent=2) + "\n").encode())
        except FileNotFoundError:
            raise ModelNotFoundError(slug) from None

    def record(self, slug: str) -> ModelRecord:
        return self._record(slug, self.version, self._has_history)

    def _record(
        self,
        slug: str,
        version_of: Callable[[str], str | None],
        history: bool,
        media_of: Callable[[str], list[MediaItem]] | None = None,
    ) -> ModelRecord:
        """``version_of`` answers a template's revision -- per call, or from the one
        walk a listing makes -- and is asked for an upstream's as well as this one's.
        ``media_of`` answers a template of mine's media rows from the listing's one
        query; without it they are read for this template alone."""
        self._require(slug)
        raw = self.read_raw_meta(slug)
        if is_builtin(slug):
            # Only `POST /models/import` sets `origin_url`, after fetching it over
            # https, and the catalogue renders it as a link -- a bundled model.json
            # never supplies one (#179). Dropped on read: the mirror must stay
            # byte-identical to the image or it re-syncs on every boot.
            raw.pop("origin_url", None)
            # Nor is a built-in anything's duplicate (#156): only a duplicate of
            # mine records an upstream.
            raw.pop("upstream", None)
        meta = self._meta(slug, raw)
        version = version_of(slug)
        upstream_state: UpstreamState | None = None
        if meta.upstream is not None and history:
            exists = self.exists(meta.upstream.id)
            upstream_state = state_of(
                meta.upstream,
                exists=exists,
                revision=version_of(meta.upstream.id) if exists else None,
            )
        try:
            modified = self.paths.model_source(slug).stat().st_mtime
        except FileNotFoundError:
            # Deleted since `_require`.
            raise ModelNotFoundError(slug) from None
        media = self._views(slug, self._stored_media(slug, meta, media_of))
        thumbnail = self.thumbnail_source(slug, media)
        return ModelRecord(
            **meta.model_dump(exclude={"media"}),
            media=media,
            slug=slug,
            origin="builtin" if is_builtin(slug) else "mine",
            has_thumbnail=thumbnail.source is not None,
            thumbnail_source=thumbnail.source,
            thumbnail_output_id=thumbnail.output_id,
            thumbnail_preview_id=thumbnail.preview_id,
            has_readme=self.readme_path(slug).is_file(),
            updated_at=datetime.fromtimestamp(modified, UTC),
            version=version,
            upstream_state=upstream_state,
            invalid_libraries=invalid_entries(raw.get("libraries")),
        )

    @property
    def _has_history(self) -> bool:
        return self.history is not None and self.history.available

    def slugs(self) -> list[str]:
        """Every template id, mine then the built-ins, without building records."""
        return _templates_in(self.paths.models) + [
            f"{BUILTIN_PREFIX}{slug}" for slug in _templates_in(self.paths.builtins)
        ]

    def library_users(self, name: str, commit: str | None = None) -> list[str]:
        """The models whose live ``model.json`` pins ``name`` (at ``commit``).

        Read leniently and counted conservatively, because the answer decides
        whether a checkout may be deleted: an entry that names the library with no
        readable commit -- a hand edit -- counts at every commit, and a
        ``model.json`` that is not JSON counts when its text mentions the name at
        all.
        """
        users: list[str] = []
        for slug in self.slugs():
            try:
                raw = self.read_raw_meta(slug)
            except InvalidModelMetaError:
                with contextlib.suppress(OSError):
                    if name in self.paths.model_meta(slug).read_text(errors="replace"):
                        users.append(slug)
                continue
            entries = raw.get("libraries")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if entry_name(entry) != name:
                    continue
                pinned = entry.get("commit") if isinstance(entry, dict) else None
                if commit is None or not isinstance(pinned, str) or pinned == commit:
                    users.append(slug)
                    break
        return users

    def list_models(self) -> list[ModelRecord]:
        """Mine, then the built-ins. Only a directory with a ``model.scad`` at its top
        is a template, so the ``_builtin`` mirror itself is never listed as one."""
        slugs = self.slugs()
        # ONE git call for the page, not one per model: see `last_commits`. The same
        # walk answers every duplicate's upstream revision too.
        versions = self.versions()
        history = self._has_history
        # And ONE media query (#274), for every template of mine on the page.
        rows = (
            self.media_store.items_for([slug for slug in slugs if not is_builtin(slug)])
            if self.media_store is not None
            else {}
        )

        def media_of(slug: str) -> list[MediaItem]:
            return rows.get(slug, [])

        records: list[ModelRecord] = []
        for slug in slugs:
            try:
                records.append(self._record(slug, versions.get, history, media_of))
            except InvalidModelMetaError as error:
                # One broken model.json costs its own model, never the whole page;
                # `GET /models/{slug}` says what is wrong with it.
                logger.warning("left a model out of the listing: %s", error, extra={"slug": slug})
        return records

    def create(
        self,
        slug: str,
        source: str,
        meta: ModelMeta,
        *,
        thumbnail: bytes | None = None,
        readme: str | None = None,
    ) -> ModelRecord:
        if self.exists(slug):
            raise ModelExistsError(slug)
        directory = self._claim(slug)
        try:
            self.paths.model_source(slug).write_text(source, encoding="utf-8")
            # A template of mine's media list is rows (#274), never model.json.
            self.write_raw_meta(slug, meta.model_dump(exclude={"media"}))
            self._clear_media_rows(slug)
            if thumbnail is not None:
                self.thumbnail_path(slug).write_bytes(thumbnail)
            if readme is not None:
                self.readme_path(slug).write_text(readme, encoding="utf-8")
        except BaseException:
            # The claim is exclusive, so the directory is this create's alone: a
            # half-written one left behind would hold the slug for good.
            _remove_tree(directory)
            raise
        self._commit(f"Add {slug}", slug)
        return self.record(slug)

    def duplicate(self, upstream_id: str, slug: str, name: str) -> ModelRecord:
        """Copy a template to a new one of mine at ``slug``, recording its upstream.

        The copy is staged under ``cache/`` (same volume) and renamed into place
        with its ``model.json`` already written, so the new template appears whole
        or not at all, and a slug something else claimed meanwhile refuses the
        rename rather than being copied into. Not under ``cache/tombstones/``: a
        concurrent delete sweeps that.
        """
        self._require(upstream_id)
        if self.exists(slug):
            raise ModelExistsError(slug)
        # The upstream's list, copied as rows once the files are in place (#274).
        media = self._stored_media(upstream_id)
        # Not `self.version`, which logs a git failure and answers None: here that
        # would record no base and copy the working tree instead of the revision.
        # A failure reading it fails the duplicate, as a failed export does.
        base: str | None = None
        if self.history is not None and self.history.available:
            base = self.history.last_commit(model_path(upstream_id))
            if base is None:
                # `last_commit` answers None for a failed `git log` as well as for
                # no commit; either way there is no revision to copy or to record.
                raise GitError(f"could not read the current revision of {upstream_id!r}")
        self.paths.cache.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=self.paths.cache, prefix=DUPLICATE_STAGING_PREFIX))
        staged = staging / slug
        target = self.paths.model_dir(slug)
        try:
            if base is not None and self.history is not None:
                # From the revision recorded as `base`, not the working tree: a
                # source edit landing between reading `base` and copying would
                # otherwise give a copy newer than the revision it claims.
                try:
                    self.history.export(model_path(upstream_id), base, staged)
                except RevisionNotFoundError:
                    raise ModelNotFoundError(upstream_id) from None
                # Videos are not in the history (#274): the revision has their
                # entries but not their files, so they come from the working tree.
                self._copy_videos(upstream_id, staged / MEDIA_DIR)
            else:
                try:
                    # Dotfiles are left out: a `.model-*.scad` is a source write in
                    # flight.
                    shutil.copytree(
                        self.paths.model_dir(upstream_id),
                        staged,
                        # Nor a render's colour wrapper, written beside the source
                        # for the length of a render and gitignored for that reason.
                        ignore=shutil.ignore_patterns(
                            ".*", *([f"{self.wrapper_prefix}*"] if self.wrapper_prefix else [])
                        ),
                    )
                except FileNotFoundError:
                    raise ModelNotFoundError(upstream_id) from None
            meta_path = staged / MODEL_META_NAME
            loaded: Any = json.loads(meta_path.read_text("utf-8")) if meta_path.is_file() else {}
            meta: dict[str, Any] = loaded if isinstance(loaded, dict) else {}
            meta.pop("schema", None)
            # A built-in's bundled list becomes rows of the copy's own.
            meta.pop("media", None)
            if is_builtin(upstream_id):
                # A built-in never has an `origin_url` (#179): the record drops one
                # its image's model.json carries, and the copy must not bring it
                # back as a template of mine's link.
                meta.pop("origin_url", None)
            meta["name"] = name
            meta["upstream"] = Upstream(
                id=upstream_id, path=model_path(upstream_id), base=base
            ).model_dump()
            meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
            # Claimed as `create` claims, so the copy is never visible beside a
            # previous occupant's schema cache, outputs or revisions.
            self._claim(slug)
            # Onto the empty directory just claimed, the rename replaces it whole.
            try:
                staged.rename(target)
            except OSError as error:
                # Only an empty claim is still ours to give back: `rmdir` refuses
                # anything something else has written into, and that is left alone.
                try:
                    target.rmdir()
                except OSError as leftover:
                    if leftover.errno in (errno.ENOTEMPTY, errno.EEXIST):
                        raise ModelExistsError(slug) from error
                    raise
                raise
        finally:
            _remove_tree(staging)
        # After the files, as every media write: a row never names a file not there.
        if self.media_store is not None:
            self.media_store.replace(slug, media)
        self._commit(f"Duplicate {upstream_id} as {slug}", slug)
        # Sweep any staging an earlier duplicate crashed out of, once it is old
        # enough not to be another replica's copy in flight: a single replica that crashed and
        # restarted within SCADBUDDY_DUPLICATE_STAGING_MAX_AGE of the crash clears it
        # here rather than never. Best-effort:
        # the duplicate is committed, so a failure here is logged, not reported.
        try:
            self.sweep_duplicate_staging()
        except OSError:
            logger.exception("could not sweep duplicate staging")
        return self.record(slug)

    def _copy_videos(self, upstream_id: str, target: Path) -> None:
        """Copy the upstream's video files into ``target`` (its ``media/``)."""
        source = self.media_dir(upstream_id)
        if not source.is_dir():
            return
        for path in sorted(source.iterdir()):
            if path.suffix.lstrip(".").lower() in VIDEO_EXTENSIONS and path.is_file():
                target.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target / path.name)

    def update(self, slug: str, patch: ModelPatch) -> ModelRecord:
        self._require(slug)

        def change() -> None:
            raw = self.read_raw_meta(slug)
            raw.update(patch.model_dump(exclude_none=True))
            self.write_raw_meta(slug, raw)
            if patch.presets is not None:
                # The list written is the template's presets whole: a legacy file left
                # beside it would add its entries back (they are read below model.json),
                # so `[]` could never clear them. A client edits the merged list it
                # read, so what it keeps of the legacy file is in the list it wrote.
                (self.paths.model_dir(slug) / LEGACY_PRESETS_NAME).unlink(missing_ok=True)

        self._commit_change(f"Update {slug} metadata", change, slug)
        return self.record(slug)

    def pin_library(
        self, slug: str, library: ModelLibrary, *, replacing: ModelLibrary | None = None
    ) -> ModelRecord:
        """Pin ``library`` for this model: in place of any entry of the same name,
        or at the end. One revision of the model; no other model moves.

        With ``replacing``, only in place of that entry: checked in the same
        read-modify-write as the pin (under the history's write lock), and
        :class:`LibraryPinChangedError` when the entry is no longer what the
        caller read -- so a re-pin cannot bring back a library an unpin removed
        while its clone ran, nor overwrite a pin another request just moved.
        """
        self._require(slug)

        def change() -> None:
            raw = self.read_raw_meta(slug)
            current = raw.get("libraries")
            entries: list[Any] = list(current) if isinstance(current, list) else []
            if replacing is not None and not _declares(entries, library.name, replacing):
                raise LibraryPinChangedError(library.name)
            # Where the old entry was, so a re-pin is a one-line diff; any duplicate a
            # hand edit left goes with it.
            index = next(
                (i for i, entry in enumerate(entries) if entry_name(entry) == library.name),
                len(entries),
            )
            entries = [entry for entry in entries if entry_name(entry) != library.name]
            entries.insert(index, library.model_dump())
            raw["libraries"] = entries
            self.write_raw_meta(slug, raw)

        message = f"Pin {library.name} to {library.ref} ({library.commit[:7]}) for {slug}"
        self._commit_change(message, change, slug)
        return self.record(slug)

    def unpin_library(self, slug: str, name: str) -> ModelRecord:
        """Take ``name`` off this model's libraries. :class:`KeyError` when the
        model does not declare it."""
        self._require(slug)

        def change() -> None:
            raw = self.read_raw_meta(slug)
            current = raw.get("libraries")
            entries: list[Any] = list(current) if isinstance(current, list) else []
            kept = [entry for entry in entries if entry_name(entry) != name]
            if len(kept) == len(entries):
                raise LibraryNotDeclaredError(name)
            raw["libraries"] = kept
            self.write_raw_meta(slug, raw)

        self._commit_change(f"Remove library {name} from {slug}", change, slug)
        return self.record(slug)

    def write_thumbnail(self, slug: str, png: bytes) -> ModelRecord:
        """Set or replace the model's own thumbnail, as one revision. A default-render
        preview has nothing left to stand in for, so it goes.

        Once the template has media (#274), the thumbnail is its cover: the PNG
        takes the place of a first item that is an image, or goes in front of a
        first item that is a video. It gets a new id, since an id never changes
        its contents."""
        self._require(slug)
        if not self._stored_media(slug):
            self._write_sidecar(slug, THUMBNAIL_NAME, png)
            self._drop_preview(slug)
            self._commit(f"Set {slug} thumbnail", slug)
            return self.record(slug)
        item_id = new_media_id()
        cover = MediaItem(id=item_id, file=f"{item_id}.png", kind="image")

        def change() -> None:
            edit = self._edit_media(slug)
            replaced = edit.items[0] if edit.items and edit.items[0].kind == "image" else None
            self._media_directory(slug)
            write_atomic(self.media_dir(slug) / cover.file, png)
            edit.items[: 1 if replaced is not None else 0] = [cover]
            self._save_media(slug, edit, added=[cover.file])

        self._commit_change(f"Set {slug} thumbnail", change, slug)
        self._drop_preview(slug)
        return self.record(slug)

    def delete_thumbnail(self, slug: str) -> ModelRecord:
        """Remove the model's own thumbnail, as one revision. The record may still
        report one: the next item's, or the fallback's when the model has been
        generated. With media, that is the first item when it is an image."""
        self._require(slug)
        if not self._stored_media(slug):
            self._remove_sidecar(slug, THUMBNAIL_NAME)
            self._commit(f"Remove {slug} thumbnail", slug)
            return self.record(slug)

        def change() -> None:
            edit = self._edit_media(slug)
            if not edit.items or edit.items[0].kind != "image":
                raise SidecarNotFoundError(THUMBNAIL_NAME)
            del edit.items[0]
            self._save_media(slug, edit)

        self._commit_change(f"Remove {slug} thumbnail", change, slug)
        return self.record(slug)

    # ── media (#274) ──────────────────────────────────────────────────────────

    def media_dir(self, slug: str) -> Path:
        return self.paths.model_dir(slug) / MEDIA_DIR

    def _media_directory(self, slug: str) -> Path:
        """``media/``, made if need be -- but never the model directory itself: a
        write racing a delete fails with the delete's 404."""
        directory = self.media_dir(slug)
        try:
            directory.mkdir(exist_ok=True)
        except FileNotFoundError:
            raise ModelNotFoundError(slug) from None
        return directory

    def _media_file(self, slug: str, item_id: str, file: str) -> Path:
        """Where an item's file is: ``media/<file>``, or the model's own
        ``thumbnail.png`` for the synthesized legacy item."""
        if item_id == LEGACY_ID and file == THUMBNAIL_NAME:
            return self.thumbnail_path(slug)
        return self.media_dir(slug) / file

    def _stored_media(
        self,
        slug: str,
        meta: ModelMeta | None = None,
        media_of: Callable[[str], list[MediaItem]] | None = None,
    ) -> list[MediaItem]:
        """The list as stored: a built-in's bundled model.json ``media``, shipped
        read-only; a template of mine's `template_media` rows, or none without a
        database. ``meta`` is the model's and ``media_of`` a listing's rows, when
        the caller already has them."""
        if is_builtin(slug):
            return (meta or self._meta(slug, self.read_raw_meta(slug))).media
        if self.media_store is None:
            return []
        if media_of is not None:
            return media_of(slug)
        return self.media_store.items(slug)

    def _views(self, slug: str, items: list[MediaItem]) -> list[MediaView]:
        """``items`` with what the disk says of each. With none stored, the model's
        ``thumbnail.png``, if any, is the one item (the legacy thumbnail)."""
        if not items:
            size = _file_size(self.thumbnail_path(slug))
            if size is None:
                return []
            return [
                MediaView(
                    id=LEGACY_ID,
                    file=THUMBNAIL_NAME,
                    kind="image",
                    content_type=content_type_of(THUMBNAIL_NAME),
                    size=size,
                )
            ]
        views: list[MediaView] = []
        for item in items:
            size = _file_size(self._media_file(slug, item.id, item.file))
            poster = item.poster
            if poster is not None and _file_size(self.media_dir(slug) / poster) is None:
                poster = None
            views.append(
                MediaView(
                    **item.model_dump(exclude={"poster"}),
                    poster=poster,
                    missing=size is None,
                    content_type=content_type_of(item.file),
                    size=size,
                )
            )
        return views

    def list_media(self, slug: str) -> list[MediaView]:
        """The template's images and videos, in order; the first is the cover."""
        self._require(slug)
        return self._views(slug, self._stored_media(slug))

    def media_item(self, slug: str, item_id: str) -> tuple[MediaView, Path]:
        """One item and its file, or :class:`MediaNotFoundError` -- for an unknown
        id and for an entry whose file is missing alike."""
        for item in self.list_media(slug):
            if item.id == item_id and not item.missing:
                return item, self._media_file(slug, item.id, item.file)
        raise MediaNotFoundError(item_id)

    def media_poster(self, slug: str, item_id: str) -> Path:
        """The poster of one item, or :class:`MediaNotFoundError`."""
        for item in self.list_media(slug):
            if item.id == item_id and item.poster is not None:
                return self.media_dir(slug) / item.poster
        raise MediaNotFoundError(item_id)

    def _clear_media_rows(self, slug: str) -> None:
        """Rows a deleted model at ``slug`` left behind (its delete removes them
        best-effort), so a new model there does not list its predecessor's media."""
        if self.media_store is not None:
            self.media_store.delete(slug)

    def _require_media_store(self) -> MediaStore:
        if self.media_store is None:
            raise MediaUnavailableError("template media needs SCADBUDDY_DATABASE_URL")
        return self.media_store

    def _edit_media(self, slug: str) -> _MediaEdit:
        """The stored items, to change and pass to :meth:`_save_media`. A legacy
        ``thumbnail.png`` comes back as an ordinary item with an id of its own,
        which the save moves into ``media/`` -- the first write converts it."""
        stored = self._require_media_store().items(slug)
        if stored:
            return _MediaEdit(list(stored), stored, None)
        if not self.thumbnail_path(slug).is_file():
            return _MediaEdit([], [], None)
        item_id = new_media_id()
        legacy = MediaItem(id=item_id, file=f"{item_id}.png", kind="image")
        return _MediaEdit([legacy], [], legacy)

    def _resolve_id(self, edit: _MediaEdit, item_id: str) -> str:
        """``thumbnail`` names the converted legacy item until the first write."""
        if item_id == LEGACY_ID and edit.legacy is not None:
            return edit.legacy.id
        return item_id

    def _save_media(self, slug: str, edit: _MediaEdit, added: Sequence[str] = ()) -> None:
        """Write ``edit.items`` as the template's rows, then remove the files of every
        item it no longer holds. The files come first: ``added`` (already in
        ``media/``) and a converted legacy thumbnail are in place before the rows
        name them, and are taken back out if the rows cannot be written. Under the
        history's write lock, as every change is."""
        store = self._require_media_store()
        before = list(edit.before)
        converted = edit.legacy is not None and any(i.id == edit.legacy.id for i in edit.items)
        if edit.legacy is not None:
            if converted:
                directory = self._media_directory(slug)
                os.replace(self.thumbnail_path(slug), directory / edit.legacy.file)
            else:
                before.append(edit.legacy)
        try:
            store.replace(slug, edit.items)
        except BaseException:
            if converted and edit.legacy is not None:
                with contextlib.suppress(OSError):
                    os.replace(self.media_dir(slug) / edit.legacy.file, self.thumbnail_path(slug))
            for added_name in added:
                (self.media_dir(slug) / added_name).unlink(missing_ok=True)
            raise
        kept = {name for item in edit.items for name in (item.file, item.poster) if name}
        for item in before:
            for name in (item.file, item.poster):
                if name is None or name in kept:
                    continue
                if edit.legacy is not None and item.id == edit.legacy.id:
                    self.thumbnail_path(slug).unlink(missing_ok=True)
                else:
                    (self.media_dir(slug) / name).unlink(missing_ok=True)

    def add_media(
        self,
        slug: str,
        upload: StagedMedia,
        caption: str = "",
        poster: StagedMedia | None = None,
    ) -> ModelRecord:
        """Move a staged upload (and its poster) into ``media/`` as the last item,
        as one revision. :class:`TooManyMediaError` at :data:`MAX_MEDIA_ITEMS`."""
        self._require(slug)
        item_id = new_media_id()
        item = MediaItem(
            id=item_id,
            file=f"{item_id}.{upload.extension}",
            kind=upload.kind,
            caption=caption,
            poster=f"{item_id}-poster.{poster.extension}" if poster is not None else None,
        )

        def change() -> None:
            edit = self._edit_media(slug)
            if len(edit.items) >= MAX_MEDIA_ITEMS:
                raise TooManyMediaError(slug)
            directory = self._media_directory(slug)
            added = [item.file]
            os.replace(upload.path, directory / item.file)
            if poster is not None and item.poster is not None:
                os.replace(poster.path, directory / item.poster)
                added.append(item.poster)
            edit.items.append(item)
            self._save_media(slug, edit, added=added)

        kind = "video" if upload.kind == "video" else "image"
        self._commit_change(f"Add {kind} {item_id} to {slug}", change, slug)
        return self.record(slug)

    def set_caption(self, slug: str, item_id: str, caption: str) -> ModelRecord:
        self._require(slug)

        def change() -> None:
            edit = self._edit_media(slug)
            wanted = self._resolve_id(edit, item_id)
            for index, item in enumerate(edit.items):
                if item.id == wanted:
                    edit.items[index] = item.model_copy(update={"caption": caption})
                    break
            else:
                raise MediaNotFoundError(item_id)
            self._save_media(slug, edit)

        self._commit_change(f"Caption {item_id} in {slug}", change, slug)
        return self.record(slug)

    def reorder(self, slug: str, ids: list[str]) -> ModelRecord:
        """Put the items in the order of ``ids``, which must name each exactly
        once; otherwise :class:`MediaOrderError`."""
        self._require(slug)

        def change() -> None:
            edit = self._edit_media(slug)
            wanted = [self._resolve_id(edit, item_id) for item_id in ids]
            by_id = {item.id: item for item in edit.items}
            if len(wanted) != len(by_id) or set(wanted) != set(by_id):
                raise MediaOrderError("the order must name every media item exactly once")
            edit.items[:] = [by_id[item_id] for item_id in wanted]
            self._save_media(slug, edit)

        self._commit_change(f"Reorder {slug} media", change, slug)
        return self.record(slug)

    def remove_media(self, slug: str, item_id: str) -> ModelRecord:
        """Remove one item and its files, as one revision. An entry whose file is
        already gone is removed all the same."""
        self._require(slug)

        def change() -> None:
            edit = self._edit_media(slug)
            wanted = self._resolve_id(edit, item_id)
            kept = [item for item in edit.items if item.id != wanted]
            if len(kept) == len(edit.items):
                raise MediaNotFoundError(item_id)
            edit.items[:] = kept
            self._save_media(slug, edit)

        self._commit_change(f"Remove {item_id} from {slug}", change, slug)
        return self.record(slug)

    def read_readme(self, slug: str) -> str:
        self._require(slug)
        try:
            return self.readme_path(slug).read_text(encoding="utf-8")
        except FileNotFoundError:
            raise SidecarNotFoundError(README_NAME) from None

    def write_readme(self, slug: str, text: str) -> ModelRecord:
        """Set or replace the model's README, as one revision."""
        self._write_sidecar(slug, README_NAME, text.encode("utf-8"))
        self._commit(f"Set {slug} README", slug)
        return self.record(slug)

    def delete_readme(self, slug: str) -> ModelRecord:
        self._remove_sidecar(slug, README_NAME)
        self._commit(f"Remove {slug} README", slug)
        return self.record(slug)

    def _write_sidecar(self, slug: str, name: str, payload: bytes) -> None:
        """Swap one of the model's files in atomically, as `write_source` does.

        The same reasons apply: a reader (a GET, or git staging a concurrent
        commit) must never see a half-written file, and a write racing a delete
        must fail with the delete's 404 rather than recreate the directory.
        """
        self._require(slug)
        try:
            write_atomic(self.paths.model_dir(slug) / name, payload)
        except FileNotFoundError:
            raise ModelNotFoundError(slug) from None

    def _remove_sidecar(self, slug: str, name: str) -> None:
        self._require(slug)
        try:
            (self.paths.model_dir(slug) / name).unlink()
        except FileNotFoundError:
            # Either the file was never there or a delete took the whole model;
            # which one decides the answer.
            self._require(slug)
            raise SidecarNotFoundError(name) from None

    def write_source(
        self,
        slug: str,
        source: str,
        *,
        message: str | None = None,
        merge_base: str | None = None,
    ) -> ModelRecord:
        """Replace a model's ``.scad`` as one revision.

        The hook the paste/edit path (#92) calls: everything that rewrites model
        source goes through here so it is versioned exactly once. The derived
        schema is dropped rather than left for `cached_schema` to notice: it is
        keyed by the source hash, so a stale one is only ever dead weight.

        The swap never leaves ``model.scad`` half-written. A render for this slug
        may be queued or running, and OpenSCAD opens the file by path;
        `write_text` truncates first, so a reader landing in that window sees a
        torn file and fails for a reason that has nothing to do with its own
        source. `os.replace` is atomic, and a temp file in the same directory
        keeps it on one filesystem so it stays that way.

        ``merge_base`` saves the resolution of a conflicted upstream merge (#157):
        the upstream revision it resolves becomes ``base``, in the same commit. It
        must name a revision of the upstream (:class:`InvalidMergeBaseError`
        otherwise, with nothing written).
        """
        self._require(slug)
        if merge_base is None:
            self._write_edit(slug, source, message or f"Edit {slug} source")
            return self.record(slug)
        history = self._require_history()
        upstream_id = self._upstream(slug).id

        def resolve() -> None:
            upstream = self._upstream(slug)
            base = _merge_base_of(history, upstream, merge_base)
            self._replace_source(slug, source)
            self._advance_base(slug, upstream, base)

        self._commit_change(message or f"Merge {upstream_id} into {slug}", resolve, slug)
        return self.record(slug)

    def _write_edit(self, slug: str, source: str, message: str) -> None:
        """A plain edit: written under the history's write lock, with its commit (#370),
        so it cannot land between another write's check and its write -- a merge's
        ``still_applies``, say -- nor be overwritten by one before it is committed.

        Failures as :meth:`_commit`: a failed commit after the write is logged, not
        raised. When the lock itself cannot be had, the edit is written without it
        and only its revision is lost, as it always was.
        """
        if self.history is None or not self.history.available:
            self._replace_source(slug, source)
            self.notify_change(slug)
            return
        started = written = False

        def write() -> None:
            nonlocal started, written
            started = True
            self._replace_source(slug, source)
            written = True

        try:
            self.history.commit(message, slug, prepare=write)
        except (GitError, OSError):
            if started and not written:
                raise
            if not started:
                self._replace_source(slug, source)
                written = True
            logger.exception("could not record a revision", extra={"revision_message": message})
        finally:
            # Like `_commit_change`: the preview scheduler hears of a source that was written.
            if written:
                self.notify_change(slug)

    def _replace_source(self, slug: str, source: str) -> None:
        """Swap in ``model.scad`` atomically and drop the schema derived from the old one."""
        # A delete can rename the directory away at any point in here; staging
        # and swapping inside it then fail rather than recreate it, and the
        # failure is the same 404 the delete itself would give.
        try:
            write_atomic(self.paths.model_source(slug), source.encode())
        except FileNotFoundError:
            raise ModelNotFoundError(slug) from None
        self.paths.model_schema_cache(slug).unlink(missing_ok=True)

    # ── upstream (#157) ───────────────────────────────────────────────────────

    def _upstream(self, slug: str) -> Upstream:
        upstream = self._meta(slug, self.read_raw_meta(slug)).upstream
        if upstream is None:
            raise NoUpstreamError(slug)
        return upstream

    def _require_history(self) -> ModelHistory:
        if self.history is None or not self.history.available:
            raise GitUnavailableError("model history is unavailable")
        return self.history

    def _upstream_now(self, slug: str) -> tuple[Upstream, str | None, UpstreamState]:
        """The upstream, its current revision (None when gone) and where this stands."""
        history = self._require_history()
        self._require(slug)
        upstream = self._upstream(slug)
        exists = self.exists(upstream.id)
        revision = history.last_commit(model_path(upstream.id)) if exists else None
        return upstream, revision, state_of(upstream, exists=exists, revision=revision)

    def _write_upstream(self, slug: str, upstream: Upstream | None) -> None:
        raw = self.read_raw_meta(slug)
        if upstream is None:
            raw.pop("upstream", None)
        else:
            raw["upstream"] = upstream.model_dump()
        self.write_raw_meta(slug, raw)

    def _advance_base(self, slug: str, upstream: Upstream, revision: str) -> None:
        """This template now includes the upstream at ``revision``, which is where the
        upstream lives now: the next merge reads its base from there."""
        if not revision:
            raise InvalidMergeBaseError("an upstream base cannot be empty")
        self._write_upstream(
            slug, Upstream(id=upstream.id, path=model_path(upstream.id), base=revision)
        )

    def upstream_status(self, slug: str) -> UpstreamStatus:
        upstream, revision, state = self._upstream_now(slug)
        preview = None
        if state in ("update", "dismissed") and revision is not None:
            preview = plan_merge(
                self._require_history(), slug, self.paths.model_dir(slug), upstream, revision
            ).preview
        return UpstreamStatus(state=state, upstream=upstream, revision=revision, preview=preview)

    def merge_upstream(self, slug: str) -> tuple[ModelRecord, MergePlan]:
        """Take the upstream's current revision as one commit, or raise
        :class:`MergeConflictError` having written nothing.

        Worked out outside the history's write lock -- its git reads would stall
        every other catalogue write -- and written under it only if neither this
        template nor its upstream has moved since. If one has, it is worked out
        again, a bounded number of times, and then refused as a 409.
        """
        history = self._require_history()
        self._require(slug)
        upstream_id = self._upstream(slug).id
        directory = self.paths.model_dir(slug)

        for _ in range(MERGE_ATTEMPTS):
            planned = (history.last_commit(model_path(slug)), *self._upstream_now(slug))
            _, upstream, revision, state = planned
            if state not in ("update", "dismissed") or revision is None:
                raise UpstreamStateError(f"{slug!r} has no upstream update to merge", state)
            plan = plan_merge(history, slug, directory, upstream, revision)
            if plan.conflicts:
                raise MergeConflictError(plan)
            merge = partial(self._write_merge, slug, plan, planned)
            try:
                self._commit_change(f"Merge {upstream_id} into {slug}", merge, slug)
            except _StaleMergeError:
                continue
            return self.record(slug), plan
        raise UpstreamStateError(
            f"{slug!r} or its upstream kept changing while the merge was worked out; try again",
            state,
        )

    def _write_merge(
        self,
        slug: str,
        plan: MergePlan,
        planned: tuple[str | None, Upstream, str | None, UpstreamState],
    ) -> None:
        """Write ``plan`` -- under the write lock -- unless the template's revision, its
        upstream or the files the plan read have moved since ``planned`` was read."""
        history = self._require_history()
        directory = self.paths.model_dir(slug)
        now = (history.last_commit(model_path(slug)), *self._upstream_now(slug))
        if now != planned or not plan.still_applies(directory):
            raise _StaleMergeError
        if plan.preview.merged != plan.preview.ours:
            self._replace_source(slug, plan.preview.merged)
        for name, content in plan.files.items():
            target = directory / name
            if content is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(target, content)
        self._advance_base(slug, planned[1], plan.revision)

    def dismiss_upstream(self, slug: str) -> ModelRecord:
        """Don't offer the upstream's current revision again; a later one still is."""
        upstream_id = self._upstream_now(slug)[0].id

        def dismiss() -> None:
            upstream, revision, state = self._upstream_now(slug)
            if state not in ("update", "dismissed") or revision is None:
                raise UpstreamStateError(f"{slug!r} has no upstream update to dismiss", state)
            self._write_upstream(slug, upstream.model_copy(update={"dismissed": revision}))

        self._commit_change(f"Dismiss {upstream_id} update in {slug}", dismiss, slug)
        return self.record(slug)

    def detach_upstream(self, slug: str) -> ModelRecord:
        """Forget an upstream that is gone; the template carries on as a plain one of mine."""
        upstream_id = self._upstream_now(slug)[0].id

        def detach() -> None:
            upstream, _, state = self._upstream_now(slug)
            if state != "gone":
                raise UpstreamStateError(
                    f"{upstream.id!r} still exists, so {slug!r} stays linked", state
                )
            self._write_upstream(slug, None)

        self._commit_change(f"Detach {slug} from {upstream_id}", detach, slug)
        return self.record(slug)

    def duplicates_of(self, model_id: str) -> list[str]:
        """The templates of mine whose upstream is ``model_id``."""
        found: list[str] = []
        for slug in _templates_in(self.paths.models):
            try:
                upstream = self.read_raw_meta(slug).get("upstream")
            except (OSError, ValueError):
                continue
            if isinstance(upstream, dict) and upstream.get("id") == model_id:
                found.append(slug)
        return found

    def delete(self, slug: str) -> None:
        self._require(slug)
        # Renamed out of `models/` first, so the model leaves the catalogue in one
        # step: an `rmtree` that dies halfway could otherwise leave `model.scad`
        # behind and a half-deleted model listed. The tombstone lives under
        # `cache/` (same volume, so the rename is atomic) rather than beside the
        # model, where it would be picked up by the listing and by `git add -A`.
        tombstones = self.paths.tombstones
        tombstones.mkdir(parents=True, exist_ok=True)
        try:
            self.paths.model_dir(slug).rename(tombstones / f"{slug}.{uuid.uuid4().hex}")
        except FileNotFoundError:
            # A concurrent delete of the same slug won the rename.
            raise ModelNotFoundError(slug) from None
        # The history is the shared `models/` repository, not the model's own:
        # a delete is one more commit, so the model's revisions stay restorable.
        self._commit(f"Delete {slug}", slug)
        # The model is deleted once the rename and commit are done. Everything
        # below is best-effort cleanup: each step logs its own failure and the
        # rest still run, so a completed delete never reports an error.
        # This delete's tombstone, and any an earlier one failed to clear.
        try:
            self.sweep_tombstones()
        except OSError:
            logger.exception("could not sweep tombstones", extra={"path": str(tombstones)})
        # Its derived files -- the schema cache, exported old revisions and its
        # outputs (NOT in the repository: a 3MF is a build artefact, not source)
        # -- and any an earlier delete failed to clear or a race wrote since.
        self.sweep_orphans()
        # Whether or not the sweep got to its outputs: the model is gone either way.
        self._forget_cover(slug)
        self._drop_preview(slug)
        # Its media rows (#274). Best-effort as the rest: a create or duplicate at
        # this slug clears any left behind (`_clear_media_rows`).
        if self.media_store is not None:
            try:
                self.media_store.delete(slug)
            except Exception:
                logger.exception("could not remove media rows", extra={"slug": slug})

    def sweep_tombstones(self) -> list[str]:
        """Remove every tombstone left under ``cache/tombstones/``.

        Runs after each delete and once at startup, so a removal that failed
        (a busy PVC, a crash between the rename and the rmtree) is retried
        rather than leaking a deleted model's files for good.
        """
        root = self.paths.tombstones
        if not root.is_dir():
            return []
        removed: list[str] = []
        for tombstone in sorted(root.iterdir()):
            if _remove_tree(tombstone):
                removed.append(tombstone.name)
        return removed

    def sweep_duplicate_staging(self) -> list[str]:
        """Remove the ``cache/duplicate-*`` folders a duplicate killed mid-copy left,
        and the ``cache/media-upload-*`` files an upload killed mid-stream left.

        Runs at boot, after each duplicate and with the periodic upload sweep.
        Another replica sharing ``/data`` may be mid-copy, so only staging older
        than ``duplicate_staging_max_age`` goes (an upload in flight writes its
        file, so its age is since its last chunk).
        One that cannot be read or removed is logged and the rest still go.
        """
        root = self.paths.cache
        if not root.is_dir():
            return []
        cutoff = time.time() - self.duplicate_staging_max_age
        removed: list[str] = []
        entries = [
            *root.glob(f"{DUPLICATE_STAGING_PREFIX}*"),
            *root.glob(f"{MEDIA_UPLOAD_PREFIX}*"),
        ]
        for entry in sorted(entries):
            try:
                if entry.stat().st_mtime > cutoff:
                    continue
            except FileNotFoundError:
                continue
            except OSError:
                logger.exception(
                    "could not read a duplicate's staging", extra={"entry": entry.name}
                )
                continue
            if _remove_tree(entry):
                removed.append(entry.name)
        return removed

    def sweep_orphans(self) -> list[str]:
        """Remove the slug-keyed derived files of every model that is gone.

        A delete's own cleanup can fail (a busy PVC, a crash after the commit),
        and a source PUT or a render racing a delete can write the schema cache
        or an output after it ran. Nothing lists these but the slug, so without
        this they would leak -- or be inherited by a later model of that name.

        A slug counts as live while its directory exists, not only once
        ``model.scad`` does: `create` and the built-in sync make the directory first, and a
        model mid-creation must not lose anything. A live slug is never touched,
        so this is safe beside a running render.

        Each root and each slug is checked on its own: one that cannot be read
        is logged and skipped -- a slug whose liveness is unknown is kept -- and
        the rest are still swept.
        """
        candidates: list[tuple[str, Path]] = []
        keyed_by_file = (self.paths.schema_cache,)
        roots = (self.paths.outputs, self.paths.model_revisions, *keyed_by_file)
        for root in roots:
            try:
                if not root.is_dir():
                    continue
                for entry in root.iterdir():
                    if root in (self.paths.outputs, self.paths.model_revisions):
                        candidates.append((entry.name, entry))
                    elif root in keyed_by_file and entry.suffix == ".json":
                        candidates.append((entry.stem, entry))
            except OSError:
                logger.exception("could not list for orphans", extra={"path": str(root)})
        removed: list[str] = []
        for slug, path in sorted(candidates):
            try:
                if self.paths.model_dir(slug).exists():
                    continue
            except OSError:
                logger.exception("could not check a model for orphans", extra={"slug": slug})
                continue
            if _remove_tree(path):
                removed.append(str(path.relative_to(self.paths.root)))
                if path.parent == self.paths.outputs:
                    self._forget_cover(slug)
        # The saved presets are not derived, but they are keyed and orphaned the same
        # way: a template that is gone takes its presets with it.
        if self.presets is not None:
            try:
                forgotten = self.presets.sweep_orphans(
                    lambda slug: self.paths.model_dir(slug).exists()
                )
            except (OSError, psycopg.Error):
                logger.exception("could not sweep saved presets for orphans")
            else:
                removed.extend(f"saved presets of {slug}" for slug in forgotten)
        return removed

    def sweep_stranded_claims(self) -> list[str]:
        """Move to tombstones the model directories a claim left with nothing in them.

        `_claim` makes a slug's directory before `create` or `duplicate` writes
        into it; a process killed in between leaves one with no ``model.scad``,
        which reads as missing yet refuses a retry as taken. Only one with nothing
        tracked at HEAD either goes, and only once older than ``duplicate_staging_max_age``:
        another replica sharing ``/data`` may be mid-claim. Runs at boot, before
        the tombstone sweep. One that cannot be read or moved is logged and skipped.
        """
        root = self.paths.models
        if not root.is_dir():
            return []
        cutoff = time.time() - self.duplicate_staging_max_age
        moved: list[str] = []
        try:
            entries = sorted(root.iterdir())
        except OSError:
            logger.exception("could not list for stranded claims", extra={"path": str(root)})
            return []
        for directory in entries:
            slug = directory.name
            if slug.startswith(".") or slug == BUILTIN_DIR:
                continue
            try:
                if not directory.is_dir() or (directory / SOURCE_NAME).exists():
                    continue
                if directory.stat().st_mtime > cutoff:
                    continue
                # Tracked at HEAD: a model whose source is missing from disk, not a
                # claim -- restoring it is the history's job, not this sweep's.
                if self.history is not None and self.history.available:
                    head = self.history.head()
                    if head is not None and self.history.files_at(head, slug):
                        continue
                tombstones = self.paths.tombstones
                tombstones.mkdir(parents=True, exist_ok=True)
                directory.rename(tombstones / f"{slug}.{uuid.uuid4().hex}")
            except FileNotFoundError:
                continue
            except (OSError, GitError):
                logger.exception("could not sweep a stranded claim", extra={"slug": slug})
                continue
            moved.append(slug)
        return moved

    def sweep_orphan_previews(self) -> list[str]:
        """Drop the default-render preview of every model that is gone; returns their
        ids. Apart from `sweep_orphans`, which works by path: the previews are
        rows in the database.

        A model counts as live while its directory exists, as for `sweep_orphans`,
        and one whose liveness cannot be checked is kept.
        """
        if self.previews is None:
            return []
        try:
            slugs = self.previews.slugs()
        except psycopg.Error:
            # The database, not one model: nothing to sweep this time, as
            # `sweep_orphans` skips a root it cannot list.
            logger.exception("could not list the previews to sweep")
            return []
        removed: list[str] = []
        for slug in slugs:
            try:
                if self.paths.model_dir(slug).exists():
                    continue
            except OSError:
                logger.exception("could not check a model for orphans", extra={"slug": slug})
                continue
            self.previews.drop(slug)
            removed.append(slug)
        return removed

    def _drop_preview(self, slug: str) -> None:
        """Best effort, like the rest of a delete's or a reused slug's cleanup: one
        left behind ranks below the model's own thumbnail, and a gone model's is
        swept at the next boot."""
        if self.previews is None:
            return
        try:
            self.previews.drop(slug)
        except Exception:
            logger.exception("could not drop a preview", extra={"slug": slug})

    def _claim(self, slug: str) -> Path:
        """Make ``slug``'s directory, or raise ModelExistsError if anything has it.

        The one way a new model takes its slug, and exclusive (no ``exist_ok``):
        of two creates or duplicates racing for a slug, the second is refused
        rather than writing into, or renamed over, the first one's directory.
        What an earlier model of the slug left behind is cleared only once the
        claim is held.
        """
        directory = self.paths.model_dir(slug)
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir()
        except FileExistsError:
            raise ModelExistsError(slug) from None
        self._clear_derived(slug)
        return directory

    def _clear_derived(self, slug: str) -> None:
        """Remove what an earlier model of this slug left behind, before it is reused.

        The orphan sweep usually removes these already. When it failed, they are
        still here, and without this the new model would inherit the old one's
        outputs and cached schema.
        """
        for path in (
            self.paths.model_schema_cache(slug),
            self.paths.model_revisions / slug,
            self.paths.outputs / slug,
        ):
            _remove_tree(path)
        # Under the model's preview lock: a render of the previous model finishing
        # now is either dropped here or discarded by its own "still wanted?" check.
        self._drop_preview(slug)
        self._forget_cover(slug)
        # Not derived, but the previous model's: its saved presets.
        if self.presets is not None:
            try:
                self.presets.forget(slug)
            except psycopg.Error:
                logger.exception("could not forget saved presets", extra={"slug": slug})

    def _forget_cover(self, slug: str) -> None:
        """Drop the output store's resolved fallback cover for ``slug``, whose
        outputs are gone -- so a removed model leaves nothing behind in memory."""
        if self.outputs is not None:
            self.outputs.forget_plate_cover(slug)

    def sync_builtins(self, bundled: Path) -> str | None:
        """Mirror the image's bundled models into ``_builtin/`` as one commit.

        The image is the source of truth for a built-in: one whose files differ
        from the mirror's in any way is replaced whole, and one the image no
        longer has is removed. One that matches is not touched at all -- this
        runs on every boot, and rewriting an unchanged tree is PVC writes and
        mtime churn for nothing. Nothing else writes the mirror, so its history
        is each built-in's version history.

        Runs at boot, before the render queue starts, so nothing reads a
        built-in while it is being replaced.

        Best effort, like the sweeps: it runs in the app lifespan, so anything
        it raised would stop the boot. A built-in that cannot be synced is
        logged and keeps its previous mirror (or none), and the rest still sync.
        """
        try:
            wanted = [slug for slug in _templates_in(bundled) if _routable(slug)]
            mirror = self.paths.builtins
            mirror.mkdir(parents=True, exist_ok=True)
            present = sorted(mirror.iterdir())
        except OSError:
            logger.exception("could not sync built-in templates", extra={"from": str(bundled)})
            return None
        for stale in present:
            if stale.name not in wanted:
                _remove_tree(stale)
        changed: list[str] = []
        for slug in wanted:
            try:
                if _same_tree(bundled / slug, mirror / slug):
                    continue
                self._replace_builtin(bundled / slug, mirror / slug)
            except OSError:
                logger.exception("could not sync a built-in template", extra={"slug": slug})
                continue
            changed.append(slug)
        commit = self._commit(SYNC_MESSAGE, BUILTIN_DIR)
        if commit is not None:
            logger.info(
                "synced built-in templates", extra={"changed": changed, "from": str(bundled)}
            )
        return commit

    def link_seeded(self) -> str | None:
        """Make every seeded template of mine a duplicate of its built-in, as one commit.

        A model the pre-#155 seed copied in has the slug of a built-in and no
        ``upstream``. It gets ``upstream = builtin:<slug>`` with ``base`` = its own
        seed commit and ``path`` = ``<slug>``, where the source lived at that
        commit: the seeded source is the true merge base, so an unedited one
        merges cleanly to the current built-in and an edited one keeps its edits.
        Nothing is renamed, so outputs, ``model_version`` stamps and deep links
        still point where they did. When the built-in has not changed since the
        seed, ``base`` is its current revision instead (see :meth:`_seeded_base`),
        so a freshly linked template reports no update.

        Runs at boot, after :meth:`sync_builtins`. Idempotent: a linked model has
        an ``upstream``, so a second boot finds nothing to do and commits nothing.
        Best effort like the sync: a model that cannot be linked is logged and
        left as it is, and the rest are still linked.
        """
        if self.history is None or not self.history.available:
            return None
        try:
            builtins = set(_templates_in(self.paths.builtins))
            candidates = [slug for slug in _templates_in(self.paths.models) if slug in builtins]
        except OSError:
            logger.exception("could not link seeded templates to their built-ins")
            return None
        seeds: dict[str, tuple[str, str]] = {}
        for slug in candidates:
            try:
                if self.read_raw_meta(slug).get("upstream") is not None:
                    continue
                base = self.history.seed_commit(slug)
            except (GitError, OSError, ValueError):
                logger.exception("could not link a seeded template", extra={"slug": slug})
                continue
            if base is None:
                logger.info(
                    "not linking a template to its built-in: it was not seeded",
                    extra={"slug": slug},
                )
                continue
            seeds[slug] = self._seeded_base(slug, base)
        if not seeds:
            return None
        linked: list[str] = []

        def link() -> None:
            # Re-read under the write lock: the read-modify-write of each
            # `model.json` is atomic with the commit, as every other one is.
            for slug, (base, path) in seeds.items():
                try:
                    raw = self.read_raw_meta(slug)
                    if raw.get("upstream") is not None:
                        continue
                    raw["upstream"] = Upstream(
                        id=f"{BUILTIN_PREFIX}{slug}", path=path, base=base
                    ).model_dump()
                    # Linked, it is a duplicate of the built-in, and holds what a
                    # duplicate does (#179): no `origin_url` its image's model.json
                    # carried, which only `POST /models/import` may set.
                    raw.pop("origin_url", None)
                    self.write_raw_meta(slug, raw)
                except (OSError, ValueError, ModelNotFoundError):
                    logger.exception("could not link a seeded template", extra={"slug": slug})
                    continue
                linked.append(slug)

        commit = self._commit_change(LINK_MESSAGE, link, *seeds)
        if linked:
            logger.info("linked seeded templates to their built-ins", extra={"slugs": linked})
        return commit

    def _seeded_base(self, slug: str, seed: str) -> tuple[str, str]:
        """The ``(base, path)`` a seeded template links with: the built-in's current
        revision when it is still what was seeded -- so an unchanged built-in is no
        update -- and otherwise the seed commit, where the source lived at ``slug``.
        ``model.json`` is each template's own, so it is not compared."""
        assert self.history is not None
        builtin = model_path(f"{BUILTIN_PREFIX}{slug}")
        revision = self.history.last_commit(builtin)
        if revision is not None:
            seeded = self.history.blobs(seed, model_path(slug))
            current = self.history.blobs(revision, builtin)
            seeded.pop(MODEL_META_NAME, None)
            current.pop(MODEL_META_NAME, None)
            if seeded == current:
                return revision, builtin
        return seed, model_path(slug)

    def _replace_builtin(self, source: Path, target: Path) -> None:
        """Copy ``source`` over ``target`` without ever leaving a half-copied mirror.

        The copy is staged under ``cache/tombstones/`` (same volume, so the moves
        are atomic renames) and the old mirror is renamed there before the staged
        copy takes its place. A failure part-way leaves its debris in the
        tombstones, which the boot's sweep clears, never in ``_builtin/``: a
        failed copy keeps the previous mirror.
        """
        tombstones = self.paths.tombstones
        tombstones.mkdir(parents=True, exist_ok=True)
        staged = tombstones / f"{BUILTIN_DIR}-{target.name}.{uuid.uuid4().hex}"
        retired = staged.with_name(f"{staged.name}.old")
        try:
            shutil.copytree(source, staged, ignore=shutil.ignore_patterns(".*"))
            # A new built-in has nothing to retire.
            with contextlib.suppress(FileNotFoundError):
                os.replace(target, retired)
            try:
                os.replace(staged, target)
            except OSError:
                if retired.exists():
                    os.replace(retired, target)
                raise
        finally:
            _remove_tree(staged)
            _remove_tree(retired)


@dataclass
class _MediaEdit:
    """A template's media being changed: the items, the rows as they were, and the
    legacy thumbnail they converted, if any (see :meth:`Catalogue._edit_media`)."""

    items: list[MediaItem]
    before: list[MediaItem]
    legacy: MediaItem | None


def _file_size(path: Path) -> int | None:
    """The size of a regular file, or None when there is none."""
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def _routable(slug: str) -> bool:
    """Can a route reach ``builtin:<slug>``? A bundled directory whose name is no slug
    would be a built-in nothing can address, so it is skipped (#197)."""
    if is_slug(slug):
        return True
    logger.warning("not a usable slug; skipping this built-in template", extra={"slug": slug})
    return False


def _same_tree(source: Path, target: Path) -> bool:
    """Does ``target`` hold what ``source`` does? The copy keeps each file's size and
    mtime, so matching stats settle it without reading a byte; only when they differ
    are the bytes compared (#206)."""
    return _tree(source, _stat) == _tree(target, _stat) or _tree(source) == _tree(target)


def _stat(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _tree(directory: Path, read: Callable[[Path], object] = Path.read_bytes) -> dict[str, object]:
    """``read`` of every file under ``directory`` by relative path, dotfiles left out
    as the copy leaves them out. Empty when there is no such directory."""
    if not directory.is_dir():
        return {}
    return {
        path.relative_to(directory).as_posix(): read(path)
        for path in directory.rglob("*")
        if path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(directory).parts)
    }


def _merge_base_of(history: ModelHistory, upstream: Upstream, commit: str) -> str:
    """``commit`` as a full id, when it is a revision of ``upstream``: one that touched
    where it lives now -- which the conflict 409's ``merge_base`` always does -- or
    where it lived at ``base``."""
    try:
        resolved = history.resolve(commit) if commit else ""
    except RevisionNotFoundError:
        resolved = ""
    places = {model_path(upstream.id), upstream.path}
    if not resolved or not any(history.touched(resolved, place) for place in places):
        raise InvalidMergeBaseError(f"merge_base {commit!r} is not a revision of {upstream.id!r}")
    return resolved


def _declares(entries: list[Any], name: str, expected: ModelLibrary) -> bool:
    """Is ``expected`` still the entry ``entries`` has for ``name``?"""
    found = [entry for entry in entries if entry_name(entry) == name]
    if len(found) != 1:
        return False
    [entry] = found
    try:
        return ModelLibrary.model_validate(entry) == expected
    except ValidationError:
        return False


def _templates_in(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.iterdir() if (path / SOURCE_NAME).is_file())
