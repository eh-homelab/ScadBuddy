"""What a template's `pipeline/activities.py` may import (spec 2026-09-27 §5.2): the
references a pipeline passes around, and `emit` for files. The workflow payloads are
these same classes. trimesh is installed on the worker, but only `Part.meshes` needs
it, so it is imported there: the workflow sandbox passes this module through, and a
workflow that only carries a reference must not load trimesh."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from scadbuddy.render.glb import BoundingBox

if TYPE_CHECKING:
    import trimesh

    from scadbuddy.workflows.models import PieceRequest, PieceResult


class Blob(BaseModel):
    """A file in the store: ``path`` inside the blob ``key``."""

    kind: Literal["blob"] = "blob"
    key: str
    path: str
    #: Where the file is on this machine, filled in for a template activity only.
    local: str | None = Field(default=None, exclude=True)

    def read_bytes(self) -> bytes:
        if self.local is None:
            raise RuntimeError("a Blob is readable inside a template activity only")
        return Path(self.local).read_bytes()


class Part(BaseModel):
    """What `ctx.render` returns (§5.2): a reference to a rendered piece, never meshes."""

    kind: Literal["part"] = "part"
    piece_key: str
    file: str
    bbox: BoundingBox
    colours: list[str]
    notes: list[str] = Field(default_factory=list)
    #: Plates the piece laid out for itself (`echo(plates = N)`, base spec §6.4).
    plates: int = 1
    local: str | None = Field(default=None, exclude=True)

    @classmethod
    def of(cls, req: PieceRequest, piece: PieceResult) -> Part:
        result = piece.result
        return cls(
            piece_key=req.piece_key,
            file=req.file,
            bbox=result.bbox_mm,
            colours=list(result.colors),
            notes=list(result.notes),
            plates=max(1, len(result.plates)),
        )

    def meshes(self) -> dict[str, trimesh.Trimesh]:
        """The first plate's closed part per colour, read from the piece's layout."""
        from scadbuddy.render.jobs import LAYOUT_NAME, PlateLayout

        if self.local is None:
            raise RuntimeError("a Part's meshes are readable inside a template activity only")
        layout = PlateLayout.load(Path(self.local) / LAYOUT_NAME)
        return {part.colour: part.mesh for part in layout.plates[0].parts}


def emit(name: str, data: bytes | str) -> Blob:
    """Write a file a template activity returns; ``name`` is a plain file name."""
    out = Path(os.environ["SCADBUDDY_TEMPLATE_OUT"])
    if "/" in name or name.startswith(".") or not name:
        raise ValueError(f"not a plain file name: {name!r}")
    target = out / name
    if isinstance(data, str):
        target.write_text(data, encoding="utf-8")
    else:
        target.write_bytes(data)
    return Blob(key=os.environ["SCADBUDDY_TEMPLATE_OUT_KEY"], path=name)
