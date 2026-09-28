"""What the routes need from the render path, whichever one the deployment runs: the
legacy `RenderQueue` or `RenderService` on Temporal (``SCADBUDDY_TEMPORAL_ADDRESS``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from scadbuddy.render.job_models import Job
from scadbuddy.render.schema import ParamValue


class JobReader(Protocol):
    def read(self, job_id: str) -> Job: ...
    def list_jobs(self) -> list[Job]: ...
    def latest_finished(self, slug: str) -> Job | None: ...
    def has_unfinished(self, slug: str) -> bool: ...


class RenderBackend(Protocol):
    @property
    def store(self) -> JobReader: ...

    async def submit(
        self,
        slug: str,
        params: Mapping[str, ParamValue],
        *,
        model_version: str | None = None,
        supersedes: str | None = None,
    ) -> Job: ...

    def retry_after(self) -> int: ...
    def refresh_metrics(self) -> None: ...
    async def aclose(self) -> None: ...
