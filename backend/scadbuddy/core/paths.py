from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scadbuddy.core.fontconfig import fonts_dir

SOURCE_NAME = "model.scad"
META_NAME = "model.json"


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

    def model_dir(self, slug: str) -> Path:
        return self.models / slug

    def model_source(self, slug: str) -> Path:
        return self.model_dir(slug) / SOURCE_NAME

    def model_meta(self, slug: str) -> Path:
        return self.model_dir(slug) / META_NAME

    def model_revision_dir(self, slug: str, commit: str) -> Path:
        """An old revision of a model, exported out of git.

        Outside ``models/`` on purpose: it is derived, it must not be versioned,
        and being an ordinary model directory means the schema cache and the
        renderer work on it unchanged. Commits are immutable, so once populated an
        entry never needs invalidating.
        """
        return self.cache / "revisions" / slug / commit

    def output_dir(self, slug: str, output_id: str) -> Path:
        return self.outputs / slug / output_id

    def job_file(self, job_id: str) -> Path:
        return self.jobs / f"{job_id}.json"

    def job_work_dir(self, job_id: str) -> Path:
        return self.jobs / f"{job_id}.work"

    def ensure(self) -> None:
        for directory in (self.models, self.outputs, self.jobs, self.cache, self.fonts):
            directory.mkdir(parents=True, exist_ok=True)
