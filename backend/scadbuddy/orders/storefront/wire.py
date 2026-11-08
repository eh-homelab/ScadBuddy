"""The storefront listing's shapes (#5282, spec 2026-11-02 §5.3): the back office's body,
the stored row, and what publish captures from the template's commit.

A listing offers one commit of a template. Each parameter a customer may change has a
bound (``ParamBound``) inside the schema's own; every other parameter is fixed. Publish
fixes the parameters the listing does not name at the schema's defaults (plan point 13).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.library.libraries import ModelLibrary
from scadbuddy.library.media import MediaKind
from scadbuddy.render.schema import CustomizerSchema, ParamValue

#: A commit of the models repository, whole: a listing never pins a short id.
LISTING_COMMIT = r"^[0-9a-f]{40}$"
#: The most text a customer may type into a free text parameter.
MAX_TEXT_LENGTH = 200


class NumberBound(BaseModel):
    """A number the customer picks between ``min`` and ``max``, on ``step`` when set."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["number"]
    min: float
    max: float
    step: float | None = Field(default=None, gt=0)


class ChoiceBound(BaseModel):
    """One of ``choices``. For a ``// color`` parameter, each choice is the colour of one
    of the listing's filaments."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["choice"]
    choices: list[ParamValue] = Field(min_length=1)


class TextBound(BaseModel):
    """Free text of at most ``max_length`` characters."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["text"]
    max_length: int = Field(ge=1, le=MAX_TEXT_LENGTH)


ParamBound = Annotated[NumberBound | ChoiceBound | TextBound, Field(discriminator="kind")]


class ListingSnapshot(BaseModel):
    """What publish read from the commit, so a visitor's render needs no git (plan
    point 2): the schema, whether the template is a pipeline, its name, its library
    pins, and the key of its source snapshot in the content store."""

    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)

    schema_: CustomizerSchema = Field(alias="schema")
    pipeline: bool
    template_name: str
    libraries: list[ModelLibrary]
    snapshot_key: str


class ListingMedia(BaseModel):
    """One media file publish copied into the content store."""

    blob_key: str
    kind: MediaKind
    media_type: str
    caption: str


class ListingInput(BaseModel):
    """The back office's listing, as it creates or replaces one."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=200)
    commit: str = Field(pattern=LISTING_COMMIT)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    params: dict[str, ParamBound] = Field(default_factory=dict)
    fixed: dict[str, ParamValue] = Field(default_factory=dict)
    #: Price-book filament ids (plan point 12).
    colours: list[str] = Field(default_factory=list)
    #: Ids of the template's media (``template_media``), in the order to show them.
    media_ids: list[str] = Field(default_factory=list)


class Listing(ListingInput):
    """A stored listing: every column of ``storefront_listings``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    position: int
    published: bool
    snapshot: ListingSnapshot | None
    media: list[ListingMedia]
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ListingUpdate(BaseModel):
    """``PUT …/listings/{id}``: the listing whole, and the ``updated_at`` the editor read."""

    model_config = ConfigDict(extra="forbid")

    listing: ListingInput
    updated_at: datetime


class ListingOrder(BaseModel):
    """``PUT …/listings/order``: every listing's id, in the order to show them."""

    model_config = ConfigDict(extra="forbid")

    ids: list[str]
