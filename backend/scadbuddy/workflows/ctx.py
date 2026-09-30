"""The one object a pipeline gets (spec 2026-09-27 §5.2). Everything it does leaves
the sandbox as an activity or a child workflow; nothing here does I/O."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from scadbuddy.render.job_models import BomEntry, Job, OutputRecord, PipelineOutput, StepInfo
    from scadbuddy.render.schema import ParamValue
    from scadbuddy.template import Blob, Part
    from scadbuddy.workflows.models import (
        Failure,
        Layout,
        LayoutPlate,
        LoadedPipeline,
        OutputRef,
        OutputRequest,
        PackItem,
        PackRequest,
        PieceRequest,
        PlateSize,
        piece_key,
    )
    from scadbuddy.workflows.packing import explicit_plate

if TYPE_CHECKING:
    from scadbuddy.workflows.pipelines import TemplatePipeline


class PieceFailedError(Exception):
    """A `ctx.render` whose piece failed: the job fails with the piece's log unless
    the pipeline catches it."""

    def __init__(self, file: str, failure: Failure) -> None:
        super().__init__(f"{file}: {failure.error}")
        self.file = file
        self.failure = failure


class Ctx:
    def __init__(
        self, host: TemplatePipeline, job: Job, loaded: LoadedPipeline, inputs: Mapping[str, Any]
    ) -> None:
        self._host = host
        self._job = job
        self._loaded = loaded
        self._inputs_v = int(inputs.get("v", 0))
        self.inputs_version = loaded.inputs_version
        self.plate: PlateSize = loaded.plate
        self.outputs: list[PipelineOutput] = []
        #: Every piece rendered and every blob an output wrote: the job refs them all,
        #: so a packed output's Parts stay in the store with it (§8.4).
        self.blob_keys: list[str] = []
        self.log_tail: list[str] = []
        self.steps: list[StepInfo] = [StepInfo(name="render", state="running", done=0, total=None)]

    async def render(self, file: str, /, **params: ParamValue) -> Part:
        job = self._job
        # Without a revision the source is live and may change before the next job,
        # so the piece is this job's own: its blob directory and workflow (#642).
        scope = f"job:{job.id}" if job.model_version is None else None
        version = job.model_version if job.model_version is not None else scope
        req = PieceRequest(
            slug=job.slug,
            revision=job.model_version,
            scope=scope,
            file=file,
            params=dict(params),
            piece_key=piece_key(job.slug, version, file, params),
        )
        outcome = await self._host.piece(req)
        if outcome.result is None:
            raise PieceFailedError(file, outcome.failure or Failure(error="the piece failed"))
        if req.piece_key not in self.blob_keys:
            self.blob_keys.append(req.piece_key)
        self.log_tail = list(outcome.result.log_tail)
        return Part.of(req, outcome.result)

    async def pack(
        self,
        items: Sequence[Part | tuple[Part, int]],
        *,
        goal: str = "fewest_plates",
        filament_plan: object | None = None,
    ) -> Layout:
        if filament_plan is not None:
            raise ValueError("filament_plan arrives with Arrange (phase 5)")
        packed = [
            PackItem(part=i[0], count=i[1]) if isinstance(i, tuple) else PackItem(part=i)
            for i in items
        ]
        layout: Layout = await self._host.activity_call(
            "pack", PackRequest(items=packed, plate=self.plate, goal=goal), result_type=Layout
        )
        return layout

    def plate_of(
        self, items: Sequence[Part], *, at: Sequence[tuple[float, float, float]] | None = None
    ) -> LayoutPlate:
        if at is None:
            if len(items) != 1:
                raise ValueError(
                    "plate_of needs at=[(x, y, rot), …] for more than one part;"
                    " use pack for automatic placement"
                )
            at = [(0.0, 0.0, 0.0)]
        return explicit_plate(items, at, plate=self.plate)

    async def output(
        self,
        *,
        plates: Layout | Sequence[LayoutPlate],
        name: str | None = None,
        bom: Sequence[BomEntry | Mapping[str, Any]] | None = None,
        files: Mapping[str, str | bytes | Blob | Mapping[str, Any]] | None = None,
    ) -> OutputRef:
        layout = plates if isinstance(plates, Layout) else Layout(plates=list(plates))
        used = (
            [layout.own]
            if layout.own
            else list(dict.fromkeys(p.piece_key for pl in layout.plates for p in pl.items))
        )
        parts = [self._host.part_of(key) for key in used]
        index = len(self.outputs)
        loaded = self._loaded
        req = OutputRequest(
            job_id=self._job.id,
            index=index,
            slug=self._job.slug,
            layout=layout,
            parts=parts,
            name=name,
            bom=[b if isinstance(b, BomEntry) else BomEntry.model_validate(b) for b in bom or []],
            files={k: _file(k, v) for k, v in (files or {}).items()},
            record=OutputRecord(
                revision=self._job.model_version,
                ui_api=loaded.ui_api,
                pipeline_api=loaded.api,
                pipeline_version=loaded.version,
                inputs_v=self._inputs_v,
                plate_key=self.plate.key,
                parts=used,
            ),
        )
        written: PipelineOutput = await self._host.activity_call(
            "write_output", req, result_type=PipelineOutput
        )
        self.outputs.append(written)
        self.blob_keys.extend(k for k in written.blob_keys if k not in self.blob_keys)
        return OutputRef(index=index, name=name)

    def progress(self, message: str, *, done: int | None = None, total: int | None = None) -> None:
        self.steps = [StepInfo(name=message, state="running", done=done, total=total)]
        self._host.project_later(steps=self.steps)

    async def activity(
        self, name: str, *args: Any, timeout: float | None = None, **kwargs: Any
    ) -> Any:
        """A function of the template's `pipeline/activities.py` (§5.2)."""
        raise NotImplementedError("ctx.activity arrives with template activities (phase 4 Task 5)")


def _file(name: str, value: str | bytes | Blob | Mapping[str, Any]) -> str | Blob:
    if isinstance(value, Blob | str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(
                f"{name}: binary files come from a template activity as a Blob"
                " (scadbuddy.template.emit)"
            ) from None
    return Blob.model_validate(value)
