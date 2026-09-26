from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scadbuddy.core.fontconfig import fonts_dir

SOURCE_NAME = "model.scad"
#: The model's own metadata. Since #90 it is NOT the schema cache -- see below.
MODEL_META_NAME = "model.json"
# The DERIVED customizer schema. Never `model.json` and never inside `models/`:
# it is written lazily by the first render or schema read, outside any commit,
# so keeping it in the versioned tree would leave the repository permanently
# dirty and fold a cache blob into the next unrelated metadata commit.
SCHEMA_CACHE_NAME = "schema.json"


@dataclass(frozen=True)
class DataPaths:
    root: Path

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def outputs(self) -> Path:
        return self.root / "outputs"

    @property
    def jobs(self) -> Path:
        return self.root / "jobs"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def fonts(self) -> Path:
        return fonts_dir(self.root)

    @property
    def libraries(self) -> Path:
        """Third-party OpenSCAD library checkouts (#93). Not under ``models/``: they
        are pinned by ``models/libraries.lock``, not versioned themselves."""
        return self.root / "libraries"

    def model_dir(self, slug: str) -> Path:
        return self.models / slug

    def model_source(self, slug: str) -> Path:
        return self.model_dir(slug) / SOURCE_NAME

    def model_meta(self, slug: str) -> Path:
        return self.model_dir(slug) / MODEL_META_NAME

    def model_schema_cache(self, slug: str) -> Path:
        """Where the live model's derived schema is cached -- under ``cache/``,
        for the reason on :data:`SCHEMA_CACHE_NAME`."""
        return self.cache / "schema" / f"{slug}.json"

    @property
    def tombstones(self) -> Path:
        """Where a deleted model's directory waits for its ``rmtree`` -- outside
        ``models/``, so the listing and git never see a half-deleted model."""
        return self.cache / "tombstones"

    @property
    def model_revisions(self) -> Path:
        return self.cache / "revisions"

    def model_revision_dir(self, slug: str, commit: str) -> Path:
        """An old revision of a model, exported out of git.

        Outside ``models/`` on purpose: it is derived, it must not be versioned,
        and being an ordinary model directory means the schema cache and the
        renderer work on it unchanged. Commits are immutable, so once populated an
        entry never needs invalidating.
        """
        return self.model_revisions / slug / commit

    def output_dir(self, slug: str, output_id: str) -> Path:
        return self.outputs / slug / output_id

    def job_file(self, job_id: str) -> Path:
        return self.jobs / f"{job_id}.json"

    def job_work_dir(self, job_id: str) -> Path:
        return self.jobs / f"{job_id}.work"

    def ensure(self) -> None:
        for directory in (
            self.models,
            self.outputs,
            self.jobs,
            self.cache,
            self.fonts,
            self.libraries,
        ):
            directory.mkdir(parents=True, exist_ok=True)
