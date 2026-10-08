"""The routes the print worker reads an output through (#1060, spec 2026-10-01 §5.5).

The ``scadbuddy-print`` worker mounts no data volume, so its `RemoteOutputs` asks the
API's cluster-internal service for what `LocalOutputs` reads here: the record, the
stored ``model.3mf`` (not the download, which is laid out for the default printer), and
the names taken from the model's files. Not a client API: hidden from the schema.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from scadbuddy.api.deps import CatalogueDep, OutputIdPath, OutputsDep
from scadbuddy.api.outputs import THREE_MF_MEDIA_TYPE
from scadbuddy.bambuddy.output_reader import (
    INTERNAL_OUTPUTS,
    LocalOutputs,
    OutputNaming,
    require,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta

router = APIRouter(prefix=INTERNAL_OUTPUTS.removeprefix("/api/v1"), include_in_schema=False)


def _reader(outputs: OutputsDep, catalogue: CatalogueDep) -> LocalOutputs:
    return LocalOutputs(outputs, catalogue)


ReaderDep = Annotated[LocalOutputs, Depends(_reader)]


@router.get("/{output_id}", response_model=OutputMeta)
async def output_record(output_id: OutputIdPath, outputs: ReaderDep) -> OutputMeta:
    return await require(outputs, output_id)


@router.get("/{output_id}/model.3mf", response_class=Response)
async def output_model(output_id: OutputIdPath, outputs: ReaderDep) -> Response:
    payload = await outputs.model_3mf(output_id)
    if payload is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r} has no 3MF")
    return Response(payload, media_type=THREE_MF_MEDIA_TYPE)


@router.get("/{output_id}/naming", response_model=OutputNaming)
async def output_naming(output_id: OutputIdPath, outputs: ReaderDep) -> OutputNaming:
    return await outputs.naming(await require(outputs, output_id))
