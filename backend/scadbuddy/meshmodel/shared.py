"""What the scene's edits, the renders and the geometry analysis share (#2146), kept
off `edits` so a render, which needs these but not the edit vocabulary, does not load it.
`edits` re-exports each."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

import numpy as np
import trimesh
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.meshmodel.frames import linear
from scadbuddy.meshmodel.scene import Num, Transform, value

#: Two 0.4 mm walls around a socket (spec §5).
WALL = 0.8
MeshOf = Callable[[str], trimesh.Trimesh]


class SceneEditError(ValueError):
    """An edit the scene refuses; the route's 422."""


class JointSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    diameter: float | None = Field(default=None, gt=0, le=100)
    depth: float = Field(default=6.0, gt=0, le=100)
    clearance: float = Field(default=0.2, ge=0, le=2)
    key: Literal["none", "flat", "hex"] = "flat"
    peg_on: Literal["above", "below"] = "above"


def floats(v: Sequence[Num]) -> list[float]:
    return [value(x) for x in v]


def transform_matrix(t: Transform) -> tuple[np.ndarray, np.ndarray]:
    return linear(floats(t.rotate), floats(t.scale)), np.asarray(floats(t.translate))
