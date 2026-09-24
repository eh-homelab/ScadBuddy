from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scadbuddy.core.fontconfig import fonts_dir

#: The renderer's schema cache, beside the model it describes.
MODEL_META_NAME = "model.json"


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
    def fonts(self) -> Path:
        return fonts_dir(self.root)

    def model_dir(self, slug: str) -> Path:
        return self.models / slug

    def model_source(self, slug: str) -> Path:
        return self.model_dir(slug) / "model.scad"

    def model_meta(self, slug: str) -> Path:
        return self.model_dir(slug) / MODEL_META_NAME

    def output_dir(self, slug: str, output_id: str) -> Path:
        return self.outputs / slug / output_id

    def job_file(self, job_id: str) -> Path:
        return self.jobs / f"{job_id}.json"

    def job_work_dir(self, job_id: str) -> Path:
        return self.jobs / f"{job_id}.work"

    def ensure(self) -> None:
        for directory in (self.models, self.outputs, self.jobs, self.fonts):
            directory.mkdir(parents=True, exist_ok=True)
