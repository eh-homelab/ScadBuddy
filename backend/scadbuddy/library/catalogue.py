from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from scadbuddy.core.paths import DataPaths

logger = logging.getLogger(__name__)

THUMBNAIL_NAME = "thumbnail.png"
README_NAME = "README.md"
SOURCE_NAME = "model.scad"


class ModelNotFoundError(KeyError):
    pass


class ModelExistsError(ValueError):
    pass


class ModelMeta(BaseModel):
    """The editable half of ``model.json``. The renderer owns the ``schema`` key."""

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


class Catalogue:
    """``data/models/<slug>/`` — one directory per model, metadata in a JSON sidecar."""

    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths

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
        meta_path = self.paths.model_meta(slug)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    def record(self, slug: str) -> ModelRecord:
        self._require(slug)
        raw = self.read_raw_meta(slug)
        meta = ModelMeta.model_validate({"name": slug, **raw})
        return ModelRecord(
            **meta.model_dump(),
            slug=slug,
            has_thumbnail=self.thumbnail_path(slug).is_file(),
            has_readme=self.readme_path(slug).is_file(),
            updated_at=datetime.fromtimestamp(self.paths.model_source(slug).stat().st_mtime, UTC),
        )

    def list_models(self) -> list[ModelRecord]:
        if not self.paths.models.is_dir():
            return []
        slugs = sorted(
            path.name for path in self.paths.models.iterdir() if (path / SOURCE_NAME).is_file()
        )
        return [self.record(slug) for slug in slugs]

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
        return self.record(slug)

    def update(self, slug: str, patch: ModelPatch) -> ModelRecord:
        self._require(slug)
        raw = self.read_raw_meta(slug)
        raw.update(patch.model_dump(exclude_none=True))
        self.write_raw_meta(slug, raw)
        return self.record(slug)

    def delete(self, slug: str) -> None:
        self._require(slug)
        shutil.rmtree(self.paths.model_dir(slug), ignore_errors=True)
        # Outputs are keyed by slug and only listable through it, so they go too.
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
        return seeded
