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

import re
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from scadbuddy.library.libraries import ModelLibrary
from scadbuddy.library.media import MediaKind
from scadbuddy.orders.customer_links import MAX_LINK_CHARS
from scadbuddy.orders.models import ItemKind, OrderStatus, SubmitAddress
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import JobState, PlateInfo
from scadbuddy.render.schema import CustomizerSchema, ParamValue
from scadbuddy.storefront.wire import LISTINGS_MAX_PAGE as LISTINGS_MAX_PAGE
from scadbuddy.storefront.wire import LISTINGS_MAX_QUERY as LISTINGS_MAX_QUERY
from scadbuddy.storefront.wire import LISTINGS_QUERY_PATTERN as LISTINGS_QUERY_PATTERN
from scadbuddy.storefront.wire import (
    MAX_COLOUR_CHOICES,
    MAX_LINK_NOTE,
    MAX_MODEL_FILE_BYTES,
    MAX_MODEL_FILES,
    MAX_PRINT_LINKS,
    MAX_PURCHASE_LINKS,
    MAX_REQUEST_TEXT,
    MAX_UPLOAD_NAME,
)
from scadbuddy.storefront.wire import (
    MAX_REQUEST_FILE_BYTES as MAX_REQUEST_FILE_BYTES,
)
from scadbuddy.storefront.wire import (
    MAX_REQUEST_FILES as MAX_REQUEST_FILES,
)
from scadbuddy.storefront.wire import (
    REQUEST_KINDS as REQUEST_KINDS,
)

#: The most units of a "print my own model" request: a listing item's own cap
#: (`AddListingItem`).
MAX_ITEM_QUANTITY = 100

#: A commit of the models repository, whole: a listing never pins a short id.
LISTING_COMMIT = r"^[0-9a-f]{40}$"
#: The most text a customer may type into a free text parameter.
MAX_TEXT_LENGTH = 200


#: The most a listing's own label for a parameter may hold.
MAX_LABEL_LENGTH = 200
#: A template caption's note of the extruder that prints a colour: the shop's, not the
#: customer's (#6137).
#: ASCII digits only, as the preview's copy (`params.ts` `EXTRUDER_NOTE`) reads them.
EXTRUDER_NOTE: Final = re.compile(r"\s*\(\s*extruder\s*[0-9]+\s*\)", re.IGNORECASE)
#: A support material: the shop's choice, never the customer's (#6152, owner rule
#: 2026-11-13). The page's copy is `frontend/src/storefront/params.ts` `SUPPORT_MATERIAL`,
#: the agent's `agent/src/tools/storefrontDraft.ts` `SUPPORT_MATERIAL`: change all together.
SUPPORT_MATERIAL: Final = re.compile(r"support|pva|bvoh|hips", re.IGNORECASE)
#: A parameter that sets support: the shop may lock it, never offer it (#6152).
SUPPORT_PARAM: Final = re.compile(r"support|interface", re.IGNORECASE)


def is_support(material: str) -> bool:
    """Whether ``material`` is a support material (`SUPPORT_MATERIAL`)."""
    return SUPPORT_MATERIAL.search(material) is not None


def shown_label(name: str, label: str | None, caption: str | None) -> str:
    """What the customer reads for parameter ``name``: the listing's own ``label``, else
    the template's ``caption`` without its extruder note, else the name (#6137). The back
    office's preview applies the same rule (`frontend/src/storefront/params.ts`
    `shownLabel`): change both together."""
    if label:
        return label
    if caption:
        return " ".join(EXTRUDER_NOTE.sub(" ", caption).split()) or caption
    return name


class _Bound(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The listing's own name for the parameter, which the customer reads instead of the
    #: template's caption (#6137).
    label: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_LABEL_LENGTH)
        ]
        | None
    ) = None


class NumberBound(_Bound):
    """A number the customer picks between ``min`` and ``max``, on ``step`` when set."""

    kind: Literal["number"]
    min: float
    max: float
    step: float | None = Field(default=None, gt=0)


class ChoiceBound(_Bound):
    """One of ``choices``. For a ``// color`` parameter, each choice is the colour of one
    of the listing's filaments. An empty list is refused when the listing is saved (422
    ``listing_choice_empty``), with a sentence that names the parameter."""

    kind: Literal["choice"]
    choices: list[ParamValue]


class TextBound(_Bound):
    """Free text of at most ``max_length`` characters."""

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
    #: Every colour of an order is of one material (#6152): each pick, the filament of
    #: each offered colour left at its default, and every slot filament share one; else
    #: 422 ``listing_mixed_material``.
    one_material: bool = False


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


#: How many rows one page of ``GET …/listings`` holds at most, and by default (#6157).
LISTING_PAGE_MAX = 200
LISTING_PAGE_DEFAULT = 50
#: The furthest a page may start: far past any shop, well inside Postgres's bigint.
LISTING_OFFSET_MAX = 1_000_000

type ListingSort = Literal["position", "title", "slug", "updated"]
type ListingStatus = Literal["published", "draft"]


class ListingQuery(BaseModel):
    """``GET …/listings``'s query (#6157): a search, filters, a sort and a page."""

    model_config = ConfigDict(extra="forbid")

    #: Matches the title or the template's slug, case-insensitive, anywhere in it.
    q: str = Field(default="", max_length=200)
    status: ListingStatus | None = None
    #: Templates; a listing of any of them.
    slug: list[str] = Field(default_factory=list, max_length=100)
    #: Price-book materials (``PLA``); a listing whose colours include a filament of any.
    material: list[str] = Field(default_factory=list, max_length=50)
    sort: ListingSort = "position"
    dir: Literal["asc", "desc"] = "asc"
    limit: int = Field(default=LISTING_PAGE_DEFAULT, ge=1, le=LISTING_PAGE_MAX)
    #: Bounded so a huge offset is a 422, never a bigint overflow in Postgres.
    offset: int = Field(default=0, ge=0, le=LISTING_OFFSET_MAX)


class ListingRow(BaseModel):
    """One row of the back office's list: what the table shows, never the bounds or
    the snapshot."""

    id: str
    slug: str
    commit: str
    title: str
    #: The listing's place in the shop's order, from 1.
    place: int
    published: bool
    #: The first media id the listing shows, for the row's picture; None for none.
    media_id: str | None
    published_at: datetime | None
    updated_at: datetime


class ListingPage(BaseModel):
    """A page of ``GET …/listings``: its rows, how many match in all (``total``), and how
    many listings the shop has (``places``: the last place a move can name)."""

    items: list[ListingRow]
    total: int
    places: int
    limit: int
    offset: int


class ListingMove(BaseModel):
    """``POST …/listings/{id}/move``: the place to put the listing at, from 1. A place
    past the end puts it last."""

    model_config = ConfigDict(extra="forbid")

    place: int = Field(ge=1)


# -- the public site's view (Task 6)--------------------------------------------------


class _Shown(BaseModel):
    """What the page shows beside a bound: its label (`shown_label`) and the schema's
    group, and whether it is a ``// color`` parameter, whose choices are the colours of
    the listing's filaments. The schema has no help text to show."""

    label: str
    group: str = ""
    colour: bool = False


class PublicNumber(NumberBound, _Shown):
    label: str


class PublicChoice(ChoiceBound, _Shown):
    label: str
    #: A colour's choices are the listing filaments the price book still has, so none
    #: may be left.
    choices: list[ParamValue]


class PublicText(TextBound, _Shown):
    label: str
    #: The template's default text, which the design uses when the field is left blank
    #: (#6137); null when it has none.
    placeholder: str | None = None


PublicParam = Annotated[PublicNumber | PublicChoice | PublicText, Field(discriminator="kind")]


class PublicFilament(BaseModel):
    """A filament a visitor may pick: never its price."""

    id: str
    colour: str
    colour_name: str | None
    material: str
    #: The finish ("Matte", "Silk", "Translucent") and the maker: what tells two filaments
    #: of one colour name apart (#6152).
    subtype: str | None = None
    brand: str | None = None


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
    #: The listing filaments a visitor may pick: never a support material (#6152).
    colours: list[PublicFilament]
    #: Every pick must share one material (#6152).
    one_material: bool = False
    media: list[PublicMedia]
    updated_at: datetime


class PublicListingCard(BaseModel):
    """One listing in the public list (#6154): what a card shows, and no more. The page
    reads the whole listing (`PublicListing`) only when the visitor opens it."""

    id: str
    title: str
    description: str
    #: The listing's first picture, as a presigned GET, if it has one.
    image: PublicMedia | None
    #: False when a colour the customer must pick has no filament left (`colourGone`).
    available: bool


class PublicListings(BaseModel):
    """One page of the published listings, in the shop's order."""

    listings: list[PublicListingCard]
    #: The next page's ``cursor``; null on the last page.
    next_cursor: str | None
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
    #: The listing filament the visitor picked for each offered colour parameter, by
    #: parameter name (#6152). Its colour must be the parameter's value. The CRUD side
    #: maps picks to the render's slots by colour, so the page never guesses a slot.
    picks: dict[str, Annotated[str, StringConstraints(max_length=200)]] = Field(
        default_factory=dict, max_length=200
    )


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
    #: The edge's path of the preview, once the render is done and its files are in the
    #: API's cache (`renders.py` `shown_render`); null until then, so the page keeps
    #: polling (#6120).
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


SourceKind = Literal["file", "link"]
SourcePurpose = Literal["print", "purchase"]
CheckStatus = Literal["checking", "ready", "refused", "failed"]
SourceStatus = Literal[
    "open", "imported", "manual", "declined", "pending", "recorded", "not_needed"
]


class DraftSource(BaseModel):
    """A file or a link of a request item, as its customer reads it. A link is text only:
    the page shows it escaped, never as an anchor (spec 2026-11-13 §9.3). A file's check
    (``checking``, ``ready``, ``refused`` with ``check_reason``, ``failed``) is the intake
    worker's (§7)."""

    id: str
    kind: SourceKind
    purpose: SourcePurpose
    filename: str | None = None
    size: int | None = None
    url: str | None = None
    note: str = ""
    check_status: CheckStatus | None = None
    check_reason: str | None = None
    status: SourceStatus


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
    #: A request's files and links (#6133): its models, and the things to buy.
    sources: list[DraftSource] = Field(default_factory=list)
    #: Whether the request is a "print my own model" request (#6133), not a design one.
    own_model: bool = False


class UploadSlotAsk(BaseModel):
    """One file the visitor will upload (#6133, spec 2026-11-13 §6.2): its name, its exact
    size in bytes and its type. The store refuses a body of any other size or type."""

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=MAX_UPLOAD_NAME)
    size: int = Field(ge=1, le=MAX_MODEL_FILE_BYTES)
    media_type: Literal["model/stl", "model/3mf"]


class UploadSlotsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[UploadSlotAsk] = Field(min_length=1, max_length=MAX_MODEL_FILES)


class UploadSlot(BaseModel):
    """Where the browser PUTs one file: ``url`` with ``headers``, until ``expires_at``.
    The URL carries a signature: it is the visitor's alone and is never logged."""

    id: str
    filename: str
    url: str
    headers: dict[str, str]
    expires_at: datetime


class UploadSlots(BaseModel):
    uploads: list[UploadSlot]


class LinkAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=MAX_LINK_CHARS)
    purpose: SourcePurpose = "print"
    note: str = Field(default="", max_length=MAX_LINK_NOTE)


class ColourAsk(BaseModel):
    """The colours the visitor asked for: the shop's choice, or price book filaments by
    id."""

    model_config = ConfigDict(extra="forbid")

    shop_choice: bool = True
    filaments: list[str] = Field(default_factory=list, max_length=MAX_COLOUR_CHOICES)


class ModelRequestBody(BaseModel):
    """A "print my own model" request (#6133, spec 2026-11-13 §6.3): the upload slots the
    browser filled, the links, one quantity, the colours and a note."""

    model_config = ConfigDict(extra="forbid")

    uploads: list[str] = Field(default_factory=list, max_length=MAX_MODEL_FILES)
    links: list[LinkAsk] = Field(
        default_factory=list, max_length=MAX_PRINT_LINKS + MAX_PURCHASE_LINKS
    )
    quantity: int = Field(default=1, ge=1, le=MAX_ITEM_QUANTITY)
    colours: ColourAsk = Field(default_factory=ColourAsk)
    note: str = Field(default="", max_length=MAX_REQUEST_TEXT)


class DraftView(BaseModel):
    """The visitor's draft order: no number and no customer until it is submitted."""

    id: str
    currency: str
    minor_exponent: int
    items: list[DraftItem]
    #: The shop's shipping options, priced in ``currency`` (the shop's ``orders_currency``,
    #: which the submit gives the cart), from which the visitor picks one at submit (#5283,
    #: spec §9); one that ``ships`` needs an address.
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
    """A picture, a video or a 3D preview of a design revision, as a presigned GET of an
    hour; ``url`` is null when the picture cannot be shown now (its copy failed, or it is
    too large to copy). ``caption`` is the shop's caption, else "Design N, picture K": never the
    file's own name, which may be the shop's internal one."""

    url: str | None
    #: ``model`` is a render's 3D preview (``model/gltf-binary``) for the 3D viewer.
    kind: Literal["image", "video", "model"]
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
    open; ``paid`` comes only from the provider's settled payment (spec §7). ``failed``
    shows no link: the shop follows it up."""

    id: str
    kind: Literal["deposit", "balance"]
    amount_minor: int
    currency: str
    status: Literal["open", "pending", "paid", "failed"]
    pay_url: str | None
    #: No provider takes this request: the customer pays as the shop's
    #: ``payment_instructions`` say. False for a provider's request, also while its page
    #: is not made yet.
    manual: bool


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
    #: The shop's own words on how to pay (plain text, never markup): set only while an
    #: open request has no provider, and the shop has saved some.
    payment_instructions: str | None
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


class SignInBody(BaseModel):
    """A sign-in: the address and the code mailed to it (`orders/storefront/accounts.py`)."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=320)
    #: Sixteen characters (`XXXX-XXXX-XXXX-XXXX`); spaces, hyphens and lower case are taken too.
    code: str = Field(max_length=32)


class AccountView(BaseModel):
    """The signed-in visitor's account, as the page shows it."""

    email: str


class AccountOrder(BaseModel):
    """One order on "Your orders": no cost, note or event, only what the list shows."""

    id: str
    number: str | None
    #: The orders module's enum, so the page's labels are checked against it (#5283).
    status: OrderStatus
    created_at: datetime
    submitted_at: datetime | None
    items: int


class AccountOrders(BaseModel):
    orders: list[AccountOrder]
