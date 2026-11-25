"""The label output choice (#7216, plan `docs/superpowers/plans/2026-11-25-label-outputs.md`
R-7216-6, R-7216-8 and R-7216-9): the stored-only setting `filament_label_output`. A
request's own `format` and `dpi` outrank it."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

OutputFormat = Literal["pdf", "png"]
#: The PNG resolutions, as Bambuddy's (`routes/labels.py` at v1.3.1,
#: https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/api/routes/labels.py).
Dpi = Literal[203, 300, 600]
#: The fields a spool label can print, in the order they print (R-7216-9).
LabelField = Literal[
    "number",
    "material",
    "colour",
    "hex",
    "brand",
    "location",
    "temps",
    "weight",
    "note",
    "added",
    "qr",
]
#: What spool labels printed before #7216.
DEFAULT_FIELDS: Final[tuple[LabelField, ...]] = ("number", "material", "colour", "brand", "qr")


class LabelOutput(BaseModel):
    """The labels file's format, the PNG's dpi, and the spool label's fields."""

    model_config = ConfigDict(extra="forbid")

    format: OutputFormat = "pdf"
    dpi: Dpi = 300
    #: Task 3 of the plan makes the fields act; until then the labels print the default set.
    fields: list[LabelField] = Field(default_factory=lambda: list(DEFAULT_FIELDS), min_length=1)
