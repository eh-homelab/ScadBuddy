"""Label templates (#7157, plan `docs/superpowers/plans/2026-11-23-label-templates-pdf.md`
R-7157-1): SB's own 50 x 25 mm roll label, Bambuddy's six built-in templates with Bambuddy's
sizes (`services/label_renderer.py` at v1.3.1,
https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/label_renderer.py#L568-L579),
and a custom size of one label a page."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TemplateId = Literal[
    "roll_50x25",
    "box_40x30",
    "box_62x29",
    "ams_holder_74x33",
    "ams_holder_75x55",
    "avery_5160",
    "avery_l7160",
    "custom",
]

#: The template a label takes when none is chosen: what SB printed before #7157.
DEFAULT_TEMPLATE: Final = "roll_50x25"

#: The smallest custom label: the QR column (19.8 mm and 2.6 mm of quiet zone on each side)
#: is 25 mm, and the text column needs 15 mm beside it (R-7157-1).
MIN_WIDTH_MM: Final = 40.0
MIN_HEIGHT_MM: Final = 25.0
#: The largest: one label still fits the packed sheet's 190 mm of Letter paper (R-7157-7).
MAX_LENGTH_MM: Final = 190.0

PageSize = Literal["a4", "letter"]
#: A page's width and height in mm.
PAGE_MM: Final[dict[str, tuple[float, float]]] = {"a4": (210.0, 297.0), "letter": (215.9, 279.4)}


class LabelSheet(BaseModel):
    """A sheet of labels: the page, the grid and its margins, in mm."""

    model_config = ConfigDict(frozen=True)

    page: PageSize
    cols: int
    rows: int
    top_mm: float
    left_mm: float
    col_gap_mm: float = 0.0
    row_gap_mm: float = 0.0


class LabelTemplate(BaseModel):
    """One template: a label's size, and the sheet it is laid on (``None``: one label a
    page, or packed on paper by the `sheet` layout)."""

    model_config = ConfigDict(frozen=True)

    id: TemplateId
    name: str
    width_mm: float
    height_mm: float
    sheet: LabelSheet | None = None


TEMPLATES: Final[tuple[LabelTemplate, ...]] = (
    LabelTemplate(id="roll_50x25", name="Roll label 50 x 25 mm", width_mm=50, height_mm=25),
    LabelTemplate(id="box_40x30", name="Box label 40 x 30 mm", width_mm=40, height_mm=30),
    LabelTemplate(id="box_62x29", name="Box label 62 x 29 mm", width_mm=62, height_mm=29),
    LabelTemplate(id="ams_holder_74x33", name="AMS holder 74 x 33 mm", width_mm=74, height_mm=33),
    LabelTemplate(id="ams_holder_75x55", name="AMS holder 75 x 55 mm", width_mm=75, height_mm=55),
    LabelTemplate(
        id="avery_5160",
        name="Avery 5160 (Letter, 30 a sheet)",
        width_mm=66.675,
        height_mm=25.4,
        sheet=LabelSheet(
            page="letter", cols=3, rows=10, top_mm=12.7, left_mm=4.76, col_gap_mm=3.175
        ),
    ),
    LabelTemplate(
        id="avery_l7160",
        name="Avery L7160 (A4, 21 a sheet)",
        width_mm=63.5,
        height_mm=38.1,
        sheet=LabelSheet(page="a4", cols=3, rows=7, top_mm=15.15, left_mm=7.0, col_gap_mm=2.5),
    ),
)

BY_ID: Final = {template.id: template for template in TEMPLATES}


class LabelTemplateChoice(BaseModel):
    """The setting `filament_label_template` and a PDF request's `template`: a template's
    id, and for `custom` its width and height (R-7157-2)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    template: TemplateId = DEFAULT_TEMPLATE
    width_mm: float | None = Field(default=None, ge=MIN_WIDTH_MM, le=MAX_LENGTH_MM)
    height_mm: float | None = Field(default=None, ge=MIN_HEIGHT_MM, le=MAX_LENGTH_MM)

    @model_validator(mode="after")
    def _sizes_only_for_custom(self) -> LabelTemplateChoice:
        sized = self.width_mm is not None or self.height_mm is not None
        if self.template == "custom" and (self.width_mm is None or self.height_mm is None):
            raise ValueError("a custom label needs width_mm and height_mm")
        if self.template != "custom" and sized:
            raise ValueError("width_mm and height_mm are only for a custom label")
        return self

    def resolved(self) -> LabelTemplate:
        """The template this choice names, a custom one built from its sizes."""
        if self.template == "custom":
            assert self.width_mm is not None and self.height_mm is not None
            return LabelTemplate(
                id="custom",
                name=f"Custom {self.width_mm:g} x {self.height_mm:g} mm",
                width_mm=self.width_mm,
                height_mm=self.height_mm,
            )
        return BY_ID[self.template]
