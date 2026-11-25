"""The label output choice (#7216, plan `docs/superpowers/plans/2026-11-25-label-outputs.md`
R-7216-6, R-7216-8 and R-7216-9): the stored-only setting `filament_label_output`. A
request's own `format`, `dpi` and `fields` outrank it."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Final, Literal, get_args

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

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
#: Every field, in the order the lines print.
ALL_FIELDS: Final[tuple[LabelField, ...]] = get_args(LabelField)
#: What spool labels printed before #7216.
DEFAULT_FIELDS: Final[tuple[LabelField, ...]] = ("number", "material", "colour", "brand", "qr")


def in_order(fields: Sequence[LabelField]) -> list[LabelField]:
    """``fields`` with no repeat, in ``ALL_FIELDS`` order (R-7216-9). ``Fields``'s
    ``min_length`` refuses an empty list before this runs."""
    if len(set(fields)) != len(fields):
        raise ValueError("a field is named twice")
    return [field for field in ALL_FIELDS if field in fields]


Fields = Annotated[
    list[LabelField],
    Field(min_length=1, max_length=len(ALL_FIELDS)),
    AfterValidator(in_order),
]


class LabelOutput(BaseModel):
    """The labels file's format, the PNG's dpi, and the spool label's fields."""

    model_config = ConfigDict(extra="forbid")

    format: OutputFormat = "pdf"
    dpi: Dpi = 300
    #: The spool label's lines and its QR code (R-7216-9). A request's own `fields` outrank it.
    fields: Fields = Field(default_factory=lambda: list(DEFAULT_FIELDS))
