from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.slugs import InvalidSlugError, slugify
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.jobs import Job, PartInfo
from scadbuddy.render.schema import ParamValue

META_NAME = "meta.json"
PARAMS_NAME = "params.json"
MODEL_NAME = "model.3mf"
PREVIEW_NAME = "preview.glb"
THUMBNAIL_NAME = "thumbnail.png"

OUTPUT_ID_PATTERN = r"^[0-9a-f]{32}$"

#: Which Bambuddy route produced the ids below; see ``bambuddy/dispatch.py``.
PrintRoute = Literal["pipeline", "slice_queue"]


class OutputNotFoundError(KeyError):
    pass


class OutputMeta(BaseModel):
    id: str
    slug: str
    name: str | None = None
    job_id: str
    created_at: datetime
    bbox_mm: BoundingBox
    colors: list[str] = Field(default_factory=list)
    parts: list[PartInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Bambuddy ids, filled in by POST /outputs/{id}/send. Integers, matching
    # Bambuddy's own OpenAPI.
    library_file_id: int | None = None
    pipeline_run_id: int | None = None
    queue_item_id: int | None = None
    #: Which of Bambuddy's two routes the last print took (#87). Without it an output
    #: that has been printed both ways carries a run id *and* a queue item id, and
    #: nothing says which one describes the print now in progress.
    print_route: PrintRoute | None = None
    slice_job_id: int | None = None


class OutputStore:
    """``data/outputs/<slug>/<output-id>/`` — a persisted render plus its parameters."""

    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths

    def _find_dir(self, output_id: str) -> Path:
        for meta_path in self.paths.outputs.glob(f"*/{output_id}/{META_NAME}"):
            return meta_path.parent
        raise OutputNotFoundError(output_id)

    def directory(self, output_id: str) -> Path:
        return self._find_dir(output_id)

    def get(self, output_id: str) -> OutputMeta:
        directory = self._find_dir(output_id)
        return OutputMeta.model_validate_json((directory / META_NAME).read_text(encoding="utf-8"))

    def params(self, output_id: str) -> dict[str, ParamValue]:
        directory = self._find_dir(output_id)
        params_path = directory / PARAMS_NAME
        if not params_path.is_file():
            return {}
        loaded: dict[str, ParamValue] = json.loads(params_path.read_text(encoding="utf-8"))
        return loaded

    def list_for(self, slug: str) -> list[OutputMeta]:
        directory = self.paths.outputs / slug
        if not directory.is_dir():
            return []
        metas = [
            OutputMeta.model_validate_json(path.read_text(encoding="utf-8"))
            for path in directory.glob(f"*/{META_NAME}")
        ]
        return sorted(metas, key=lambda meta: meta.created_at, reverse=True)

    def create(self, job: Job, *, name: str | None = None) -> OutputMeta:
        if job.result is None:
            raise ValueError("the job has no result to persist")
        output_id = uuid.uuid4().hex
        directory = self.paths.output_dir(job.slug, output_id)
        directory.mkdir(parents=True, exist_ok=True)

        shutil.copyfile(self.paths.root / job.result.model_3mf, directory / MODEL_NAME)
        shutil.copyfile(self.paths.root / job.result.preview_glb, directory / PREVIEW_NAME)
        (directory / PARAMS_NAME).write_text(
            json.dumps(job.params, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        meta = OutputMeta(
            id=output_id,
            slug=job.slug,
            name=name or None,
            job_id=job.id,
            created_at=datetime.now(UTC),
            bbox_mm=job.result.bbox_mm,
            colors=list(job.result.colors),
            parts=list(job.result.parts),
            warnings=list(job.result.warnings),
        )
        self._write_meta(directory, meta)
        return meta

    def record_send(
        self,
        output_id: str,
        *,
        library_file_id: int | None = None,
        pipeline_run_id: int | None = None,
        queue_item_id: int | None = None,
        print_route: PrintRoute | None = None,
        slice_job_id: int | None = None,
    ) -> OutputMeta:
        """Persist the Bambuddy ids a send produced, leaving omitted ones alone."""
        directory = self._find_dir(output_id)
        meta = self.get(output_id)
        updated = meta.model_copy(
            update={
                key: value
                for key, value in (
                    ("library_file_id", library_file_id),
                    ("pipeline_run_id", pipeline_run_id),
                    ("queue_item_id", queue_item_id),
                    ("print_route", print_route),
                    ("slice_job_id", slice_job_id),
                )
                if value is not None
            }
        )
        self._write_meta(directory, updated)
        return updated

    def delete(self, output_id: str) -> None:
        shutil.rmtree(self._find_dir(output_id), ignore_errors=True)

    def thumbnail_path(self, output_id: str) -> Path:
        return self._find_dir(output_id) / THUMBNAIL_NAME

    def write_thumbnail(self, output_id: str, png: bytes) -> None:
        self.thumbnail_path(output_id).write_bytes(png)

    def _write_meta(self, directory: Path, meta: OutputMeta) -> None:
        (directory / META_NAME).write_text(
            json.dumps(meta.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )


def download_filename(meta: OutputMeta) -> str:
    """``<slug>-<name>.3mf``, falling back to the output id when it has no name."""
    try:
        suffix = slugify(meta.name) if meta.name else meta.id
    except InvalidSlugError:
        suffix = meta.id
    return f"{meta.slug}-{suffix}.3mf"
