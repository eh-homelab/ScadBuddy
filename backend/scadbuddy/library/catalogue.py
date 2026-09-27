from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from scadbuddy.core.files import write_atomic
from scadbuddy.core.paths import (
    BUILTIN_DIR,
    BUILTIN_PREFIX,
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
from scadbuddy.library.libraries import ModelLibrary, entry_name
from scadbuddy.library.previews import PreviewStore
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
from scadbuddy.render.solids import WRAPPER_PREFIX

if TYPE_CHECKING:
    # Type-only: `library.outputs` reaches this module again through
    # `render.provenance`, so a runtime import here would be circular.
    from scadbuddy.library.outputs import OutputStore

logger = logging.getLogger(__name__)

THUMBNAIL_NAME = "thumbnail.png"
README_NAME = "README.md"
SYNC_MESSAGE = "Sync built-in templates from the image"
LINK_MESSAGE = "Link seeded templates to their built-ins"


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
            logger.exception("could not remove a deleted model's files", extra={"path": str(path)})
            return False
    return True


def _still_there(path: Path) -> bool:
    """Whether ``path`` is still present; one that cannot even be checked counts as present."""
    try:
        return path.exists() or path.is_symlink()
    except OSError:
        return True


class ModelNotFoundError(KeyError):
    pass


class SidecarNotFoundError(KeyError):
    """The model exists, but the thumbnail or README being removed does not."""


class ModelExistsError(ValueError):
    pass


class LibraryNotDeclaredError(KeyError):
    """The model has no library of that name to remove."""


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

    @field_validator("libraries", mode="before")
    @classmethod
    def _readable_pins(cls, value: Any) -> Any:
        """Only the entries that are pins. A bare name from before per-model pins,
        or a hand-edited entry, must not stop the model listing; its render says
        what is wrong with it (`parse_declaration`), and pinning it again fixes it."""
        if not isinstance(value, list):
            return []
        readable: list[ModelLibrary] = []
        for entry in value:
            with contextlib.suppress(ValidationError):
                readable.append(ModelLibrary.model_validate(entry))
        return readable


#: The model.json fields with a default and no `None` of their own: a `null` for
#: one is the field left out, as a missing one is (#179).
DEFAULTED_META_FIELDS = frozenset({"name", "description", "tags", "libraries"})


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


class Catalogue:
    """``data/models/<slug>/`` — one directory per model, metadata in a JSON sidecar."""

    def __init__(
        self,
        paths: DataPaths,
        history: ModelHistory | None = None,
        outputs: OutputStore | None = None,
        previews: PreviewStore | None = None,
    ) -> None:
        self.paths = paths
        self.history = history
        #: Where the fallback thumbnail is read from; None turns the fallback off.
        self.outputs = outputs
        #: Where the default-render preview is read from; None turns it off.
        self.previews = previews
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

    def thumbnail_source(self, slug: str) -> ThumbnailOrigin:
        """Where the thumbnail comes from: the model's own, else the first generated
        output's plate cover, else the default-render preview."""
        if self.thumbnail_path(slug).is_file():
            return ThumbnailOrigin("model")
        if self.outputs is not None:
            output_id = self.outputs.plate_cover_output(slug)
            if output_id is not None:
                return ThumbnailOrigin("output", output_id=output_id)
        if self.previews is not None:
            preview_id = self.previews.preview_id(slug)
            if preview_id is not None:
                return ThumbnailOrigin("preview", preview_id=preview_id)
        return ThumbnailOrigin()

    def has_output_cover(self, slug: str) -> bool:
        return self.outputs is not None and self.outputs.has_plate_cover(slug)

    def thumbnail(self, slug: str) -> bytes | None:
        """The catalogue thumbnail: the model's own, else the first generated
        output's plate cover, else the default-render preview, else None."""
        self._require(slug)
        try:
            return self.thumbnail_path(slug).read_bytes()
        except FileNotFoundError:
            pass
        if self.outputs is not None:
            cover = self.outputs.plate_cover(slug)
            if cover is not None:
                return cover
        if self.previews is None:
            return None
        return self.previews.image(slug)

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
        self, slug: str, version_of: Callable[[str], str | None], history: bool
    ) -> ModelRecord:
        """``version_of`` answers a template's revision -- per call, or from the one
        walk a listing makes -- and is asked for an upstream's as well as this one's."""
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
        thumbnail = self.thumbnail_source(slug)
        return ModelRecord(
            **meta.model_dump(),
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
        )

    @property
    def _has_history(self) -> bool:
        return self.history is not None and self.history.available

    def list_models(self) -> list[ModelRecord]:
        """Mine, then the built-ins. Only a directory with a ``model.scad`` at its top
        is a template, so the ``_builtin`` mirror itself is never listed as one."""
        slugs = _templates_in(self.paths.models) + [
            f"{BUILTIN_PREFIX}{slug}" for slug in _templates_in(self.paths.builtins)
        ]
        # ONE git call for the page, not one per model: see `last_commits`. The same
        # walk answers every duplicate's upstream revision too.
        versions = self.versions()
        history = self._has_history
        records: list[ModelRecord] = []
        for slug in slugs:
            try:
                records.append(self._record(slug, versions.get, history))
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
            self.write_raw_meta(slug, meta.model_dump())
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
        staging = Path(tempfile.mkdtemp(dir=self.paths.cache, prefix="duplicate-"))
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
            else:
                try:
                    # Dotfiles are left out: a `.model-*.scad` is a source write in
                    # flight.
                    shutil.copytree(
                        self.paths.model_dir(upstream_id),
                        staged,
                        # Nor a render's colour wrapper, written beside the source
                        # for the length of a render and gitignored for that reason.
                        ignore=shutil.ignore_patterns(".*", f"{WRAPPER_PREFIX}*"),
                    )
                except FileNotFoundError:
                    raise ModelNotFoundError(upstream_id) from None
            meta_path = staged / MODEL_META_NAME
            loaded: Any = json.loads(meta_path.read_text("utf-8")) if meta_path.is_file() else {}
            meta: dict[str, Any] = loaded if isinstance(loaded, dict) else {}
            meta.pop("schema", None)
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
        self._commit(f"Duplicate {upstream_id} as {slug}", slug)
        return self.record(slug)

    def update(self, slug: str, patch: ModelPatch) -> ModelRecord:
        self._require(slug)

        def change() -> None:
            raw = self.read_raw_meta(slug)
            raw.update(patch.model_dump(exclude_none=True))
            self.write_raw_meta(slug, raw)

        self._commit_change(f"Update {slug} metadata", change, slug)
        return self.record(slug)

    def pin_library(self, slug: str, library: ModelLibrary) -> ModelRecord:
        """Pin ``library`` for this model: in place of any entry of the same name,
        or at the end. One revision of the model; no other model moves."""
        self._require(slug)

        def change() -> None:
            raw = self.read_raw_meta(slug)
            current = raw.get("libraries")
            entries: list[Any] = list(current) if isinstance(current, list) else []
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
        preview has nothing left to stand in for, so it goes."""
        self._write_sidecar(slug, THUMBNAIL_NAME, png)
        if self.previews is not None:
            self.previews.drop(slug)
        self._commit(f"Set {slug} thumbnail", slug)
        return self.record(slug)

    def delete_thumbnail(self, slug: str) -> ModelRecord:
        """Remove the model's own thumbnail, as one revision. The record may still
        report one: the fallback takes over when the model has been generated."""
        self._remove_sidecar(slug, THUMBNAIL_NAME)
        self._commit(f"Remove {slug} thumbnail", slug)
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
            self._replace_source(slug, source)
            self._commit(message or f"Edit {slug} source", slug)
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
        if state == "update" and revision is not None:
            preview = plan_merge(
                self._require_history(), slug, self.paths.model_dir(slug), upstream, revision
            ).preview
        return UpstreamStatus(state=state, upstream=upstream, revision=revision, preview=preview)

    def merge_upstream(self, slug: str) -> tuple[ModelRecord, MergePlan]:
        """Take the upstream's current revision as one commit, or raise
        :class:`MergeConflictError` having written nothing.

        Worked out and written under the history's write lock, so no other
        catalogue commit lands between reading this template and committing it.
        """
        history = self._require_history()
        self._require(slug)
        upstream_id = self._upstream(slug).id
        plans: list[MergePlan] = []

        def merge() -> None:
            upstream, revision, state = self._upstream_now(slug)
            if state not in ("update", "dismissed") or revision is None:
                raise UpstreamStateError(f"{slug!r} has no upstream update to merge", state)
            plan = plan_merge(history, slug, self.paths.model_dir(slug), upstream, revision)
            if plan.conflicts:
                raise MergeConflictError(plan)
            if plan.preview.merged != plan.preview.ours:
                self._replace_source(slug, plan.preview.merged)
            directory = self.paths.model_dir(slug)
            for name, content in plan.files.items():
                target = directory / name
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    write_atomic(target, content)
            self._advance_base(slug, upstream, revision)
            plans.append(plan)

        self._commit_change(f"Merge {upstream_id} into {slug}", merge, slug)
        return self.record(slug), plans[0]

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
        # The saved presets are not derived, but they are keyed and orphaned the same
        # way: a template that is gone takes its presets with it.
        keyed_by_file = (self.paths.schema_cache, self.paths.presets)
        roots = (
            self.paths.outputs,
            self.paths.model_revisions,
            *keyed_by_file,
            self.paths.previews,
        )
        for root in roots:
            try:
                if not root.is_dir():
                    continue
                for entry in root.iterdir():
                    if root in (self.paths.outputs, self.paths.model_revisions):
                        candidates.append((entry.name, entry))
                    elif (root in keyed_by_file and entry.suffix == ".json") or (
                        root == self.paths.previews
                        and entry.suffix in (".png", ".json")
                        # A dotfile is an atomic write's temp file, still in flight.
                        and not entry.name.startswith(".")
                    ):
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
        return removed

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
            # Not derived, but the previous model's: its saved presets.
            self.paths.model_presets(slug),
            self.paths.model_preview(slug),
            self.paths.model_preview_record(slug),
        ):
            _remove_tree(path)
        self._forget_cover(slug)

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
            wanted = _templates_in(bundled)
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
                if _tree(bundled / slug) == _tree(mirror / slug):
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


def _tree(directory: Path) -> dict[str, bytes]:
    """Every file under ``directory`` by relative path, dotfiles left out as the copy
    leaves them out. Empty when there is no such directory."""
    if not directory.is_dir():
        return {}
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
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


def _templates_in(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.iterdir() if (path / SOURCE_NAME).is_file())
