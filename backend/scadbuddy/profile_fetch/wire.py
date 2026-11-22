"""What the profile-fetch worker and the CRUD service share (#6625): the queue, the
workflow's names, and the bodies of ``/api/v1/internal/crud/profile-fetch/…``. It imports
no database or app code, so the worker, which holds no database secret, can use it."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, HttpUrl, UrlConstraints

#: The task queue the profile-fetch worker polls, and the API starts fetches on.
DEFAULT_QUEUE: Final = "profile-fetch"
FETCH_WORKFLOW: Final = "ProfileFetch"
EXPORT_ACTIVITY: Final = "profile_fetch_export"
IMPORT_ACTIVITY: Final = "profile_fetch_import"
FAILED_ACTIVITY: Final = "profile_fetch_failed"
#: #7025: the search of 3DFP and the create of a filament on it, on the same queue.
SEARCH_WORKFLOW: Final = "TdfpSearch"
CREATE_WORKFLOW: Final = "TdfpCreate"
SEARCH_ACTIVITY: Final = "tdfp_search"
CREATE_ACTIVITY: Final = "tdfp_create"
#: The CRUD areas the worker calls (`api/crud_areas.py` `CRUD_AREAS["profile-fetch"]`).
AREAS: Final = frozenset({"runs"})
#: The slicer option on the site: ScadBuddy slices only with Bambu Studio.
SLICER: Final = "Bambu Studio"
#: The largest preset file the CRUD service accepts.
MAX_FILE_BYTES: Final = 512 << 10
#: The problem ``code`` of a CRUD route for a run that is not ``running`` (Decision 25), and
#: the type of the worker's error for it, and for a run that is gone (``run_not_found``).
NOT_RUNNING: Final = "not_running"
MAX_FAILURE_REASON: Final = 300
MAX_FAILED_REASON: Final = 500


def fetch_workflow_id(run_id: str) -> str:
    """One workflow per run: the database allows one running run per type, and a new
    run never attaches to an ended run's workflow that still runs."""
    return f"profile-fetch:{run_id}"


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExportTarget(_Wire):
    """One printer model and the nozzle diameters to export for it."""

    printer_model: str
    nozzles: list[str]


class FetchAsk(_Wire):
    """The workflow's input: the run the API opened, the type's 3DFP id and the targets."""

    run_id: str
    type_id: str
    tdfp_id: int = Field(gt=0)
    targets: list[ExportTarget]


class ExportFailure(_Wire):
    printer_model: str
    nozzle: str
    reason: str = Field(max_length=MAX_FAILURE_REASON)


class ExportReport(_Wire):
    """What the export activity returns: files uploaded and the keys that failed."""

    exported: int = Field(ge=0)
    failures: list[ExportFailure]


class MissingKey(_Wire):
    printer_model: str
    nozzle_diameter: str
    reason: str


class ImportOutcome(_Wire):
    """What the import answers, and the workflow returns."""

    status: Literal["imported", "partial", "failed"]
    presets: int = Field(ge=0)
    k_values: int = Field(ge=0)
    missing: list[MissingKey]
    notes: list[str]


class FailedAsk(_Wire):
    """``POST …/runs/{run_id}/failed``: why the workflow gave up."""

    reason: str = Field(max_length=MAX_FAILED_REASON)


# -- #7025: search 3DFP, and add a filament it lacks ----------------------------------------

#: The rows of the site's first page of results ("Rows per page" 25, measured 2026-11-22).
MAX_CANDIDATES: Final = 25
#: The colour rows the add form has: ``#rgbText-0`` to ``#rgbText-3`` (the script on #7025).
MAX_HEXES: Final = 4
#: How much of the add form's duplicate list's text crosses to the worker.
MAX_DUPLICATE_TEXT: Final = 2000
MAX_TEXT: Final = 200
#: The longest product link: the longest create ask stays under the browser service's
#: `remote.MAX_ASK_BYTES` (`test_the_create_ask_fits_the_services_ask_cap`).
MAX_URL: Final = 1000


def _upper(value: str) -> str:
    return value.upper()


Hex = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$"), AfterValidator(_upper)]
Text = Annotated[str, Field(min_length=1, max_length=MAX_TEXT)]
#: A text the site shows; the reader cuts it to `MAX_TEXT`.
SiteText = Annotated[str, Field(max_length=MAX_TEXT)]


class SearchAsk(_Wire):
    """``filament_tdfp_search``: what to find. Only the brand is required."""

    brand: Text
    material: Text | None = None
    material_type: Text | None = None
    colour: Text | None = None


class Candidate(_Wire):
    """One filament on 3DFP: its id (``/filament/details/<id>``), the brand, material and
    type it is filed under, its colour name, its hexes, and the weight its name carries
    (`weight_from_name`). A field the page did not show is None."""

    tdfp_id: int = Field(gt=0)
    brand: SiteText | None = None
    material: SiteText | None = None
    material_type: SiteText | None = None
    name: SiteText
    hexes: list[Hex] = Field(default_factory=list, max_length=MAX_HEXES)
    nominal_weight_g: int | None = Field(default=None, gt=0)


class SearchAnswer(_Wire):
    """The text sent as ``?q=`` and the rows of the first page, ranked (`ranked`)."""

    query: str
    candidates: list[Candidate] = Field(max_length=MAX_CANDIDATES)


class CreateAsk(_Wire):
    """``filament_tdfp_create``: the filament to add on /filament/add. ``mpn`` to ``td`` are
    #7026's well-known identifier keys: sent to the form where it has a field for them,
    stored nowhere in ScadBuddy (R-7025-6). ``not_duplicates`` are 3DFP ids a person judged
    not to be this filament (`duplicates.likely_duplicates` skips them)."""

    brand: Text
    material: Text
    material_type: Text
    colour: Text
    hexes: list[Hex] = Field(min_length=1, max_length=MAX_HEXES)
    nominal_weight_g: int | None = Field(default=None, gt=0, le=100_000)
    mpn: Text | None = None
    gtin: Annotated[str, Field(pattern=r"^[0-9]{8,14}$")] | None = None
    asin: Annotated[str, Field(pattern=r"^[A-Z0-9]{10}$")] | None = None
    manufacturer_url: Annotated[HttpUrl, UrlConstraints(max_length=MAX_URL)] | None = None
    empty_spool_weight_g: int | None = Field(default=None, gt=0, le=10_000)
    td: float | None = Field(default=None, gt=0, le=100)
    not_duplicates: list[int] = Field(default_factory=list, max_length=MAX_CANDIDATES)

    def identifiers(self) -> dict[str, str]:
        """The identifier keys this ask sends, each as the text a form field takes."""
        values: dict[str, object] = {
            "mpn": self.mpn,
            "gtin": self.gtin,
            "asin": self.asin,
            "manufacturer_url": self.manufacturer_url,
            "empty_spool_weight_g": self.empty_spool_weight_g,
            "td": self.td,
        }
        return {key: str(value) for key, value in values.items() if value is not None}


class LikelyDuplicate(_Wire):
    """A filament of the site's duplicate list judged likely to be the one asked for, and
    why; ``tdfp_id`` is None when only the list's text named it."""

    tdfp_id: int | None = Field(default=None, gt=0)
    reason: str = Field(max_length=MAX_TEXT)


class CreateAnswer(_Wire):
    """What the create did. ``created``: Save Filament opened ``/filament/details/<tdfp_id>``.
    ``duplicate``: nothing was saved; ``likely`` lists what of the site's duplicate list
    (``duplicates``, and its text) `duplicates.likely_duplicates` judged likely, and why.
    ``sent`` and ``not_sent`` are the identifier keys the form took and lacked a field for."""

    status: Literal["created", "duplicate"]
    tdfp_id: int | None = Field(default=None, gt=0)
    duplicates: list[Candidate] = Field(default_factory=list, max_length=MAX_CANDIDATES)
    duplicate_text: str = Field(default="", max_length=MAX_DUPLICATE_TEXT)
    likely: list[LikelyDuplicate] = Field(default_factory=list, max_length=MAX_CANDIDATES + 1)
    sent: list[str] = Field(default_factory=list)
    not_sent: list[str] = Field(default_factory=list)


class TdfpOutcome(_Wire):
    """What a search or create workflow returns: its answer, or the `steps.Kind` of the
    failure and why (the worker's own text)."""

    search: SearchAnswer | None = None
    create: CreateAnswer | None = None
    kind: str | None = None
    reason: str | None = None


def search_query(ask: SearchAsk) -> str:
    """The ``?q=`` text: the brand, the type and the colour (R-7025-2). The material is left
    out of the text and ranks the rows instead (`ranked`)."""
    return " ".join(part for part in (ask.brand, ask.material_type, ask.colour) if part)


def _same(left: str | None, right: str | None) -> bool:
    return left is not None and right is not None and left.casefold() == right.casefold()


def ranked(ask: SearchAsk, rows: Sequence[Candidate]) -> list[Candidate]:
    """The rows whose material matches the ask first, then those whose type does, each
    group in the site's order."""

    def score(row: Candidate) -> int:
        return -2 * _same(ask.material, row.material) - _same(ask.material_type, row.material_type)

    return sorted(rows, key=score)


_WEIGHT = re.compile(r"\((\d+(?:[.,]\d+)?)\s*(kg|g)\)\s*$", re.IGNORECASE)


def weight_from_name(name: str) -> int | None:
    """The grams a name's suffix such as "(250g)" or "(1kg)" gives, else None (R-7025-3)."""
    found = _WEIGHT.search(name)
    if found is None:
        return None
    grams = float(found.group(1).replace(",", ".")) * (
        1000 if found.group(2).lower() == "kg" else 1
    )
    return round(grams) if grams > 0 else None
