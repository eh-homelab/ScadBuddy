"""The storefront listing's shapes (#5282, spec 2026-11-02 §5.3): the back office's body,
the stored row, what publish captures from the template's commit, the public site's
view of a published listing (`PublicListing`), a visitor's render and its draft order
(`DraftView`).

A listing offers one commit of a template. Each parameter a customer may change has a
bound (``ParamBound``) inside the schema's own; every other parameter is fixed. Publish
fixes the parameters the listing does not name at the schema's defaults (plan point 13):
the row's ``fixed`` keeps only the shop's own locks, and the published snapshot's
``fixed`` holds every value a render uses, so a republish at another commit takes that
commit's defaults.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from scadbuddy.library.libraries import ModelLibrary
from scadbuddy.library.media import MediaKind
from scadbuddy.orders.models import ItemKind, SubmitAddress
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import JobState, PlateInfo
from scadbuddy.render.schema import CustomizerSchema, ParamValue
from scadbuddy.storefront.wire import (
    MAX_REQUEST_FILE_BYTES as MAX_REQUEST_FILE_BYTES,
)
from scadbuddy.storefront.wire import (
    MAX_REQUEST_FILES as MAX_REQUEST_FILES,
)
from scadbuddy.storefront.wire import (
    REQUEST_KINDS as REQUEST_KINDS,
)

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
    of the listing's filaments. An empty list is refused when the listing is saved (422
    ``listing_choice_empty``), with a sentence that names the parameter."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["choice"]
    choices: list[ParamValue]


class TextBound(BaseModel):
    """Free text of at most ``max_length`` characters."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["text"]
    max_length: int = Field(ge=1, le=MAX_TEXT_LENGTH)


ParamBound = Annotated[NumberBound | ChoiceBound | TextBound, Field(discriminator="kind")]


class ListingSnapshot(BaseModel):
    """What publish read from the commit, so a visitor's render needs no git (plan
    point 2): the schema, whether the template is a pipeline, its name, its library
    pins, the key of its source snapshot in the content store, and every value a render
    of the listing uses that the customer does not choose."""

    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)

    schema_: CustomizerSchema = Field(alias="schema")
    #: Always false for now: publish refuses a pipeline template.
    pipeline: bool
    template_name: str
    libraries: list[ModelLibrary]
    snapshot_key: str
    #: Every parameter the listing does not offer: the shop's locks, and each other one
    #: at this commit's default (plan point 13). Read this, never the row's ``fixed``,
    #: for what a published listing renders with.
    fixed: dict[str, ParamValue] = Field(default_factory=dict)


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


# -- the public site's view (Task 6) --------------------------------------------------


class _Shown(BaseModel):
    """What the page shows beside a bound: the schema's label (its caption, else its
    name) and group, and whether it is a ``// color`` parameter, whose choices are the
    colours of the listing's filaments. The schema has no help text to show."""

    label: str
    group: str = ""
    colour: bool = False


class PublicNumber(NumberBound, _Shown):
    pass


class PublicChoice(ChoiceBound, _Shown):
    #: A colour's choices are the listing filaments the price book still has, so none
    #: may be left.
    choices: list[ParamValue]


class PublicText(TextBound, _Shown):
    pass


PublicParam = Annotated[PublicNumber | PublicChoice | PublicText, Field(discriminator="kind")]


class PublicFilament(BaseModel):
    """A filament a visitor may pick: never its price."""

    id: str
    colour: str
    colour_name: str | None
    material: str


class PublicMedia(BaseModel):
    """A media file as a presigned GET (`PUBLIC_MEDIA_TTL`)."""

    url: str
    kind: MediaKind
    caption: str


class PublicListing(BaseModel):
    """A published listing as the public site shows it. Every text is the shop's, shown
    as text, never as HTML (spec §5.8)."""

    id: str
    title: str
    description: str
    params: dict[str, PublicParam]
    colours: list[PublicFilament]
    media: list[PublicMedia]
    updated_at: datetime


class PublicListings(BaseModel):
    listings: list[PublicListing]
    #: Whether "Ask for a design" is on (``storefront_requests_enabled``).
    requests_enabled: bool


# -- a visitor's renders (Task 7) ------------------------------------------------------


#: A colour slot of a render: an index into its colours, never negative.
ColourSlotIndex = Annotated[int, Field(ge=0)]


class RenderBody(BaseModel):
    """``POST …/storefront/renders``: the visitor's values for the listing as it read it.
    The CRUD side checks every value against the listing (`inputs.py` `listing_values`),
    never only against the schema."""

    model_config = ConfigDict(extra="forbid")

    listing_id: str = Field(max_length=64)
    #: The listing's ``updated_at`` as the page read it: another is 409 ``listing_changed``.
    listing_updated_at: datetime
    values: dict[str, ParamValue] = Field(default_factory=dict, max_length=200)
    #: The listing filament the visitor chose for each colour slot of the render.
    colours: dict[ColourSlotIndex, str] = Field(default_factory=dict, max_length=64)


class StoreRender(BaseModel):
    """A visitor's render: the fields of the back office's job that ``Preview.tsx`` reads.
    ``error`` is a fixed text, never the render's log: the visitor never sees a path or a
    stack."""

    id: str
    status: JobState
    #: The step the render is in while it runs, as ``job.progress`` names it.
    stage: str | None
    plates: list[PlateInfo]
    bbox_mm: BoundingBox | None
    colors: list[str] | None
    #: The edge's path of the preview, once the render is done.
    preview_url: str | None
    error: str | None


# -- the visitor's draft (Task 8) ------------------------------------------------------


class AddListingItem(RenderBody):
    """``POST …/storefront/draft/items``: the listing at the visitor's values, as a render
    of it takes them, and how many to make. The CRUD side checks every value against the
    listing (`inputs.py` `listing_values`)."""

    model_config = ConfigDict(extra="forbid")

    quantity: int = Field(1, ge=1, le=100, strict=True)


#: What a visitor's file is, as its bytes say: one of `REQUEST_KINDS`, else ``other``.
DraftFileKind = Literal["image", "video", "pdf", "other"]


class DraftAttachment(BaseModel):
    """A file of an item, as a presigned GET (`PUBLIC_MEDIA_TTL`) that downloads it, with
    its sniffed media type."""

    filename: str
    kind: DraftFileKind
    url: str


class DraftItem(BaseModel):
    """An item of the visitor's draft. ``inputs`` are a model item's parameter values
    (``labels`` names each as the page shows it), ``notes`` a request's text; each is None
    on the other kind."""

    id: str
    kind: ItemKind
    title: str
    quantity: int
    inputs: dict[str, ParamValue] | None
    #: What the page shows beside each of ``inputs``: the schema's caption at the item's
    #: commit, else the parameter's name. Empty for an item with no inputs.
    labels: dict[str, str] = Field(default_factory=dict)
    notes: str | None
    attachments: list[DraftAttachment]


class DraftView(BaseModel):
    """The visitor's draft order: no number and no customer until it is submitted."""

    id: str
    currency: str
    minor_exponent: int
    items: list[DraftItem]
    #: The shop's shipping options, in its currency (`orders_currency`), from which the
    #: visitor picks one at submit (#5283, spec §9); one that ``ships`` needs an address.
    shipping_options: list[PublicShippingOption] = Field(default_factory=list)


class PublicShippingOption(BaseModel):
    """A shipping option as the visitor reads it."""

    id: str
    label: str
    amount_minor: int
    ships: bool


class SubmitBody(BaseModel):
    """``POST …/draft/submit`` (#5283, spec §5.5): the cart the page shows (its ``id``)
    and what the visitor typed. No ``account_id``: only the CRUD side's session lookup
    names an account."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(max_length=64)
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    email: str = Field(max_length=254)
    shipping_option_id: str | None = Field(default=None, max_length=40)
    address: SubmitAddress | None = None


class SubmittedOrder(BaseModel):
    """The answer to a submit: the order's id and number, and where its link went. Never
    the link's token, which only the mail carries."""

    id: str
    number: str
    email: str


# -- the customer's order page (#5283, spec §5.6) ------------------------------------------
#
# Every field here is in `storefront/wire.py` `ORDER_VIEW_FIELDS` too: the edge sends on
# nothing else. Never a cost, a margin, an internal note, a shop event or a staff name.


class CustomerPicture(BaseModel):
    """A picture or a video of a design revision, as a presigned GET of an hour; ``url``
    is null when the picture cannot be shown now (its copy failed, or it is too large to
    copy). ``caption`` is the shop's caption, else "Design N, picture K": never the
    file's own name, which may be the shop's internal one."""

    url: str | None
    kind: Literal["image", "video"]
    caption: str


class CustomerColour(BaseModel):
    """One colour slot of a design: what the customer accepts with it (spec §4.3).
    ``name`` is the price book filament's colour name, when it has one."""

    slot: int
    colour: str
    name: str | None


class CustomerRevision(BaseModel):
    """A design the shop sent: its note to the customer, and the customer's answer."""

    id: str
    number: int
    note: str
    sent_at: datetime
    answer: Literal["accepted", "changes_requested", "superseded"] | None
    answer_note: str | None
    #: Whether "Accept" and "Ask for changes" are offered: the item's newest sent design,
    #: not answered yet, on an order that is still open.
    can_answer: bool
    pictures: list[CustomerPicture]
    colours: list[CustomerColour]


class CustomerQuote(BaseModel):
    """The item's price."""

    id: str
    unit_price_minor: int
    quantity: int
    total_minor: int
    accepted: bool
    #: Whether "Accept price" is offered. A ``together`` item's price is accepted with
    #: its design, so only a ``design_first`` item's (once its design is accepted), or an
    #: item with no design, offers it.
    can_accept: bool


class CustomerItem(BaseModel):
    id: str
    title: str
    quantity: int
    #: The item's state in the customer's words (`order_page.py` `ITEM_WORDS`).
    status: str
    revisions: list[CustomerRevision]
    quote: CustomerQuote | None


class CustomerPayment(BaseModel):
    """A payment the shop asked for. ``pay_url`` is the request's pay link while it is
    open; ``paid`` comes only from the provider's settled payment (spec §7)."""

    id: str
    kind: Literal["deposit", "balance"]
    amount_minor: int
    currency: str
    status: Literal["open", "pending", "paid"]
    pay_url: str | None


class CustomerShipment(BaseModel):
    carrier: str | None
    service: str | None
    tracking: str | None
    shipped_at: datetime | None


class CustomerOrderView(BaseModel):
    """The customer's order page (`order_page.py` `view`)."""

    number: str
    #: The order's state in the customer's words (`order_page.py` `ORDER_WORDS`).
    status: str
    currency: str
    minor_exponent: int
    #: Whether "Cancel order" is offered: nothing is approved and nothing paid yet
    #: (spec §5.5).
    can_cancel: bool
    items: list[CustomerItem]
    payments: list[CustomerPayment]
    shipments: list[CustomerShipment]


class RevisionAnswerBody(BaseModel):
    """``POST …/order/revisions/{id}/answer``, past its token."""

    model_config = ConfigDict(extra="ignore")

    answer: Literal["accepted", "changes_requested"]
    note: str = Field(default="", max_length=4000)


class CancelBody(BaseModel):
    """``POST …/order/cancel``, past its token."""

    model_config = ConfigDict(extra="ignore")

    reason: str = Field(default="", max_length=1000)
