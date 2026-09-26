from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from scadbuddy.core.paths import (
    BUILTIN_DIR,
    SOURCE_NAME,
    DataPaths,
    model_cache_key,
    model_repo_path,
)
from scadbuddy.library.history import GitError, ModelHistory
from scadbuddy.library.slugs import SLUG_PATTERN, bare_slug, builtin_id, is_builtin

logger = logging.getLogger(__name__)

THUMBNAIL_NAME = "thumbnail.png"
README_NAME = "README.md"
SYNC_MESSAGE = "Sync built-in templates from the image"

Origin = Literal["builtin", "mine"]

_SLUG_RE = re.compile(SLUG_PATTERN)


def _templates_in(directory: Path) -> list[str]:
    """The slugs under ``directory`` that are templates: a ``model.scad`` at the top."""
    if not directory.is_dir():
        return []
    return [path.name for path in directory.iterdir() if (path / SOURCE_NAME).is_file()]


def _ignore_vanished(function: Any, path: str, error: BaseException) -> None:
    """``rmtree``'s ``onexc``: a file something else already removed is fine."""
    if not isinstance(error, FileNotFoundError):
        raise error


def _remove_tree(path: Path) -> bool:
    """``rmtree`` that logs a real failure rather than raising or hiding it.

    Concurrent deletes and sweeps can race for the same tombstone, so anything
    vanishing underneath this one counts as removed, not as a failure.
    """
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, onexc=_ignore_vanished)
        else:
            path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        if path.exists() or path.is_symlink():
            logger.exception("could not remove a deleted model's files", extra={"path": str(path)})
            return False
    return True


class ModelNotFoundError(KeyError):
    pass


class ModelExistsError(ValueError):
    pass


class ModelMeta(BaseModel):
    """``model.json``: the model's metadata, and nothing derived."""

    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    source: str | None = None


class ModelPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None


class ModelRecord(ModelMeta):
    #: The template's id: ``<slug>`` for mine, ``builtin:<slug>`` for a built-in.
    slug: str
    origin: Origin
    has_thumbnail: bool
    has_readme: bool
    updated_at: datetime
    # The commit this model is currently at, or None when history is unavailable
    # (no git binary). Outputs stamp this as their ``model_version``.
    version: str | None = None


class Catalogue:
    """``data/models/<slug>/`` — one directory per model, metadata in a JSON sidecar."""

    def __init__(self, paths: DataPaths, history: ModelHistory | None = None) -> None:
        self.paths = paths
        self.history = history

    def _commit(self, message: str, *slugs: str) -> str | None:
        return self._commit_paths(message, *(model_repo_path(slug) for slug in slugs))

    def _commit_paths(self, message: str, *paths: str) -> str | None:
        """One commit per catalogue action. A failure never fails the action itself:
        the files are already written, and losing the revision is the smaller harm.

        ``OSError`` as well as ``GitError``, because the lock file this takes on
        the way in is ordinary filesystem I/O -- a PVC that has gone read-only or
        full since boot would otherwise 500 a source edit that had already been
        written to disk, telling the client it failed when it did not.
        """
        if self.history is None or not self.history.available:
            return None
        try:
            return self.history.commit(message, *paths)
        except (GitError, OSError):
            # NOT `extra={"message": ...}`: `message` is a reserved LogRecord
            # attribute, and logging raises KeyError on the collision -- which
            # would turn this whole tolerate-and-continue branch into the crash
            # it exists to prevent.
            logger.exception("could not record a revision", extra={"revision_message": message})
            return None

    def version(self, slug: str) -> str | None:
        if self.history is None or not self.history.available:
            return None
        try:
            return self.history.last_commit(model_repo_path(slug))
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

    def read_raw_meta(self, slug: str) -> dict[str, Any]:
        meta_path = self.paths.model_meta(slug)
        if not meta_path.is_file():
            return {}
        loaded: Any = json.loads(meta_path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}

    def write_raw_meta(self, slug: str, meta: dict[str, Any]) -> None:
        # `schema` is derived and lives under `cache/` (see `SCHEMA_CACHE_NAME`).
        # Dropping it here retires the key from volumes written before that was
        # true, rather than leaving a cache blob in the versioned tree forever.
        meta.pop("schema", None)
        # No `mkdir`: the model directory must already exist (`create` makes it).
        # A write racing a delete then fails instead of recreating a directory
        # holding only `model.json` -- unlisted, and never swept as a tombstone.
        try:
            self.paths.model_meta(slug).write_text(
                json.dumps(meta, indent=2) + "\n", encoding="utf-8"
            )
        except FileNotFoundError:
            raise ModelNotFoundError(slug) from None

    def record(self, slug: str) -> ModelRecord:
        return self._record(slug, self.version(slug))

    def _record(self, slug: str, version: str | None) -> ModelRecord:
        self._require(slug)
        raw = self.read_raw_meta(slug)
        meta = ModelMeta.model_validate({"name": bare_slug(slug), **raw})
        try:
            modified = self.paths.model_source(slug).stat().st_mtime
        except FileNotFoundError:
            # Deleted since `_require`.
            raise ModelNotFoundError(slug) from None
        return ModelRecord(
            **meta.model_dump(),
            slug=slug,
            origin="builtin" if is_builtin(slug) else "mine",
            has_thumbnail=self.thumbnail_path(slug).is_file(),
            has_readme=self.readme_path(slug).is_file(),
            updated_at=datetime.fromtimestamp(modified, UTC),
            version=version,
        )

    def list_models(self) -> list[ModelRecord]:
        """Mine, then the built-ins. Only a directory with a ``model.scad`` at its
        top is a template, which is what keeps ``_builtin/`` itself out."""
        if not self.paths.models.is_dir():
            return []
        builtins = self.paths.models / BUILTIN_DIR
        slugs = [
            *sorted(_templates_in(self.paths.models)),
            *(builtin_id(slug) for slug in sorted(_templates_in(builtins))),
        ]
        # ONE git call for the page, not one per model: see `last_commits`.
        versions = self.versions()
        return [self._record(slug, versions.get(slug)) for slug in slugs]

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
        directory = self.paths.model_dir(slug)
        directory.mkdir(parents=True, exist_ok=True)
        self.paths.model_source(slug).write_text(source, encoding="utf-8")
        self.write_raw_meta(slug, meta.model_dump())
        if thumbnail is not None:
            self.thumbnail_path(slug).write_bytes(thumbnail)
        if readme is not None:
            self.readme_path(slug).write_text(readme, encoding="utf-8")
        self._commit(f"Add {slug}", slug)
        return self.record(slug)

    def update(self, slug: str, patch: ModelPatch) -> ModelRecord:
        self._require(slug)
        raw = self.read_raw_meta(slug)
        raw.update(patch.model_dump(exclude_none=True))
        self.write_raw_meta(slug, raw)
        self._commit(f"Update {slug} metadata", slug)
        return self.record(slug)

    def write_source(self, slug: str, source: str, *, message: str | None = None) -> ModelRecord:
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
        """
        self._require(slug)
        # A delete can rename the directory away at any point in here; staging
        # and swapping inside it then fail rather than recreate it, and the
        # failure is the same 404 the delete itself would give.
        try:
            handle, staged = tempfile.mkstemp(
                dir=self.paths.model_dir(slug), prefix=".model-", suffix=".scad"
            )
        except FileNotFoundError:
            raise ModelNotFoundError(slug) from None
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as writer:
                writer.write(source)
            os.replace(staged, self.paths.model_source(slug))
        except FileNotFoundError:
            Path(staged).unlink(missing_ok=True)
            raise ModelNotFoundError(slug) from None
        except BaseException:
            Path(staged).unlink(missing_ok=True)
            raise
        self.paths.model_schema_cache(slug).unlink(missing_ok=True)
        self._commit(message or f"Edit {slug} source", slug)
        return self.record(slug)

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
        # Derived, and only reachable through the slug: the schema cache and any
        # exported old revisions.
        _remove_tree(self.paths.model_schema_cache(slug))
        _remove_tree(self.paths.model_revisions / model_cache_key(slug))
        # Outputs are keyed by slug and only listable through it, so they go too.
        # They are NOT in the repository: a 3MF is a build artefact, not source.
        _remove_tree(self.paths.model_outputs(slug))
        # This delete's tombstone, and any an earlier one failed to clear.
        try:
            self.sweep_tombstones()
        except OSError:
            logger.exception("could not sweep tombstones", extra={"path": str(tombstones)})

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

    def sync_builtins(self, image_dir: Path) -> str | None:
        """Mirror the image's bundled models into ``_builtin/``, as one commit.

        Overwriting, not copy-if-absent: the image is the source of truth for a
        built-in, so a newer image's fix reaches an existing install, and one it no
        longer ships leaves the mirror. Nothing else writes ``_builtin/``, so its
        history is each built-in's version history. ``None`` when nothing changed,
        or when there is no repository to record it in.
        """
        root = self.paths.models / BUILTIN_DIR
        bundled = (
            {
                candidate.name: candidate
                for candidate in image_dir.iterdir()
                if _SLUG_RE.match(candidate.name) and (candidate / SOURCE_NAME).is_file()
            }
            if image_dir.is_dir()
            else {}
        )
        if root.is_dir():
            for stale in root.iterdir():
                if stale.name not in bundled:
                    _remove_tree(stale)
        for slug, candidate in sorted(bundled.items()):
            destination = root / slug
            if destination.exists():
                shutil.rmtree(destination)
            # `copy2`, shutil's default: the image's mtimes survive, so a boot that
            # changes nothing does not move every built-in's `updated_at`.
            shutil.copytree(candidate, destination, ignore=shutil.ignore_patterns(".*"))
        if bundled:
            logger.info("synced built-in templates", extra={"slugs": sorted(bundled)})
        return self._commit_paths(SYNC_MESSAGE, BUILTIN_DIR)
