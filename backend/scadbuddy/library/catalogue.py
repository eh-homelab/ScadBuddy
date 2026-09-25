from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from scadbuddy.core.paths import SOURCE_NAME, DataPaths
from scadbuddy.library.history import GitError, ModelHistory, summarise

logger = logging.getLogger(__name__)

THUMBNAIL_NAME = "thumbnail.png"
README_NAME = "README.md"


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
    slug: str
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
            return self.history.commit(message, *slugs)
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
            return self.history.last_commit(slug)
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
        meta_path = self.paths.model_meta(slug)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    def record(self, slug: str) -> ModelRecord:
        return self._record(slug, self.version(slug))

    def _record(self, slug: str, version: str | None) -> ModelRecord:
        self._require(slug)
        raw = self.read_raw_meta(slug)
        meta = ModelMeta.model_validate({"name": slug, **raw})
        return ModelRecord(
            **meta.model_dump(),
            slug=slug,
            has_thumbnail=self.thumbnail_path(slug).is_file(),
            has_readme=self.readme_path(slug).is_file(),
            updated_at=datetime.fromtimestamp(self.paths.model_source(slug).stat().st_mtime, UTC),
            version=version,
        )

    def list_models(self) -> list[ModelRecord]:
        if not self.paths.models.is_dir():
            return []
        slugs = sorted(
            path.name for path in self.paths.models.iterdir() if (path / SOURCE_NAME).is_file()
        )
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
        """
        self._require(slug)
        self.paths.model_source(slug).write_text(source, encoding="utf-8")
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
        tombstones = self.paths.cache / "tombstones"
        tombstones.mkdir(parents=True, exist_ok=True)
        tombstone = tombstones / f"{slug}.{uuid.uuid4().hex}"
        self.paths.model_dir(slug).rename(tombstone)
        # The history is the shared `models/` repository, not the model's own:
        # a delete is one more commit, so the model's revisions stay restorable.
        self._commit(f"Delete {slug}", slug)
        shutil.rmtree(tombstone, ignore_errors=True)
        # Derived, and only reachable through the slug: the schema cache and any
        # exported old revisions.
        self.paths.model_schema_cache(slug).unlink(missing_ok=True)
        shutil.rmtree(self.paths.model_revisions / slug, ignore_errors=True)
        # Outputs are keyed by slug and only listable through it, so they go too.
        # They are NOT in the repository: a 3MF is a build artefact, not source.
        shutil.rmtree(self.paths.outputs / slug, ignore_errors=True)

    def seed(self, seed_dir: Path) -> list[str]:
        """Copy any bundled model whose slug is not in the catalogue yet."""
        if not seed_dir.is_dir():
            return []
        seeded: list[str] = []
        for candidate in sorted(seed_dir.iterdir()):
            if not (candidate / SOURCE_NAME).is_file() or self.exists(candidate.name):
                continue
            shutil.copytree(
                candidate,
                self.paths.model_dir(candidate.name),
                ignore=shutil.ignore_patterns(".*"),
                dirs_exist_ok=True,
            )
            seeded.append(candidate.name)
        if seeded:
            logger.info("seeded models", extra={"slugs": seeded, "from": str(seed_dir)})
            # A re-seed on an image upgrade lands as a commit rather than a silent
            # overwrite -- which is the whole point of #90's seed clause.
            self._commit(f"Seed {summarise(seeded)} from the image", *seeded)
        return seeded
