"""`/api/v1/filament` purchasing (#6266, epic #6260): suppliers, their products, price
points, the price sheet and a type's price history, over `filament/suppliers.py`, and
invoice drafts, over `filament/invoices.py`, and the shopping list, over
`filament/shopping.py`. Money is a `Decimal` and goes over the wire as a string."""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from scadbuddy.api import media as media_api
from scadbuddy.api.attachment_serving import INERT, NOSNIFF
from scadbuddy.api.deps import PathsDep, StateDep
from scadbuddy.api.filament import actor, nudge_after_write
from scadbuddy.auth.component import CallerDep
from scadbuddy.core.problems import ApiError
from scadbuddy.filament import purchasing_models as pm
from scadbuddy.filament.colour_codes import ColourCode, ColourTableMissingError, colour_codes
from scadbuddy.filament.component import FilamentDep
from scadbuddy.filament.invoice_money import InvoiceMathError, MixedLineTaxError
from scadbuddy.filament.invoices import (
    DuplicateInvoiceError,
    ImportRunChangedError,
    InvoiceConfirmedError,
    InvoiceCurrencyError,
    InvoiceLineError,
    InvoiceNoLinesError,
    InvoiceNotReconciledError,
    InvoiceRefError,
    InvoiceSupplierArchivedError,
)
from scadbuddy.filament.profile_fetch import start_profile_fetches
from scadbuddy.filament.shopping import ShoppingRefError
from scadbuddy.filament.suppliers import (
    PriceFromInvoiceError,
    ProductExistsError,
    ProductRefError,
    SupplierExistsError,
)
from scadbuddy.orders import attachments

router = APIRouter(prefix="/filament", tags=["filament"], dependencies=[Depends(nudge_after_write)])


def _no_supplier(supplier_id: UUID) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no supplier {supplier_id}")


def _no_product(product_id: UUID) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no supplier product {product_id}")


def _no_invoice(invoice_id: UUID) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no invoice {invoice_id}")


def _no_item(item_id: UUID) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no shopping list item {item_id}")


def _unprocessable(exc: ValueError) -> ApiError:
    return ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


def _conflict(exc: ValueError) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, str(exc))


@router.get(
    "/suppliers",
    response_model=list[pm.Supplier],
    summary="List suppliers",
    description=(
        "The suppliers by name, each with its `product_count` (unarchived products). "
        "Archived suppliers only with `include_archived`."
    ),
)
async def list_suppliers(
    filament: FilamentDep, include_archived: bool = False
) -> list[pm.Supplier]:
    return await asyncio.to_thread(filament.suppliers.list, include_archived=include_archived)


@router.post(
    "/suppliers",
    response_model=pm.Supplier,
    status_code=status.HTTP_201_CREATED,
    summary="Add a supplier",
    description=(
        "A shop a person buys filament from: a `name`, and optionally a `website` (`http` or "
        "`https` only), a `customer_number` and a `note`. 409 when an unarchived supplier "
        "has the name, 422 for a bad field or a website that is not an http(s) URL."
    ),
)
async def create_supplier(filament: FilamentDep, body: pm.SupplierCreate) -> pm.Supplier:
    try:
        return await asyncio.to_thread(filament.suppliers.create, body)
    except SupplierExistsError as exc:
        raise _conflict(exc) from None


@router.get(
    "/suppliers/stats",
    response_model=list[pm.SupplierStats],
    summary="Supplier stats",
    description=(
        "Per supplier with a confirmed invoice, archived suppliers too (`archived`): "
        "`spool_count` (every spool its invoices made, archived ones included), "
        "`remaining_g` (grams left of the unarchived ones only), "
        "`consumed_g` (grams they used, by the ledger's rule: a print's actual replaces its "
        "estimate) and `spend` (the invoices' "
        "`allocated_cost`, a decimal string). Most consumed first, then by name."
    ),
)
async def supplier_stats(filament: FilamentDep) -> list[pm.SupplierStats]:
    return await asyncio.to_thread(filament.suppliers.stats)


@router.get(
    "/suppliers/{supplier_id}",
    response_model=pm.Supplier,
    summary="Read a supplier",
    description="One supplier (archived too). 404 for none.",
)
async def get_supplier(filament: FilamentDep, supplier_id: UUID) -> pm.Supplier:
    found = await asyncio.to_thread(filament.suppliers.get, supplier_id)
    if found is None:
        raise _no_supplier(supplier_id)
    return found


@router.patch(
    "/suppliers/{supplier_id}",
    response_model=pm.Supplier,
    summary="Change a supplier",
    description=(
        "Only the fields sent change; `null` clears a nullable one. `archived: true` takes "
        "the supplier off the lists and frees its name, `false` puts it back. 404 for no "
        "such supplier, 409 when another unarchived supplier has the name, 422 for a bad field."
    ),
)
async def patch_supplier(
    filament: FilamentDep, supplier_id: UUID, body: pm.SupplierPatch
) -> pm.Supplier:
    try:
        changed = await asyncio.to_thread(filament.suppliers.update, supplier_id, body)
    except SupplierExistsError as exc:
        raise _conflict(exc) from None
    if changed is None:
        raise _no_supplier(supplier_id)
    return changed


@router.get(
    "/suppliers/{supplier_id}/products",
    response_model=list[pm.SupplierProduct],
    summary="List a supplier's products",
    description=(
        "The supplier's products by SKU, each with its `newest_price`. Archived products "
        "only with `include_archived`. 404 for no such supplier."
    ),
)
async def list_products(
    filament: FilamentDep, supplier_id: UUID, include_archived: bool = False
) -> list[pm.SupplierProduct]:
    found = await asyncio.to_thread(
        filament.suppliers.products, supplier_id, include_archived=include_archived
    )
    if found is None:
        raise _no_supplier(supplier_id)
    return found


@router.post(
    "/suppliers/{supplier_id}/products",
    response_model=pm.SupplierProduct,
    status_code=status.HTTP_201_CREATED,
    summary="Add a supplier product",
    description=(
        "One SKU of the supplier for one spool type (`type_id`); `pack_size` is how many "
        "spools one SKU holds. 404 for no such supplier, 409 when the supplier has an "
        "unarchived product with the SKU, 422 for a bad field, a type that does not exist "
        "or a product URL that is not an http(s) URL."
    ),
)
async def create_product(
    filament: FilamentDep, supplier_id: UUID, body: pm.ProductCreate
) -> pm.SupplierProduct:
    try:
        made = await asyncio.to_thread(filament.suppliers.add_product, supplier_id, body)
    except ProductExistsError as exc:
        raise _conflict(exc) from None
    except ProductRefError as exc:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    if made is None:
        raise _no_supplier(supplier_id)
    return made


@router.get(
    "/suppliers/{supplier_id}/price-sheet",
    response_model=list[pm.PriceSheetRow],
    summary="Read a supplier's price sheet",
    description=(
        "One row per unarchived product, by type code: the product, its type, its newest "
        "and previous price points, and `change_pct`, the change of the price per kg from "
        "the previous point to the newest, in percent to one decimal (`null` without two points). "
        "404 for no such supplier."
    ),
)
async def price_sheet(filament: FilamentDep, supplier_id: UUID) -> list[pm.PriceSheetRow]:
    sheet = await asyncio.to_thread(filament.suppliers.price_sheet, supplier_id)
    if sheet is None:
        raise _no_supplier(supplier_id)
    return sheet


@router.patch(
    "/products/{product_id}",
    response_model=pm.SupplierProduct,
    summary="Change a supplier product",
    description=(
        "Only the fields sent change; `null` clears a nullable one. `archived: true` takes "
        "the product off the lists and frees its SKU, `false` puts it back. 404 for no such "
        "product, 409 when the supplier has another unarchived product with the SKU, 422 "
        "for a bad field or a type that does not exist."
    ),
)
async def patch_product(
    filament: FilamentDep, product_id: UUID, body: pm.ProductPatch
) -> pm.SupplierProduct:
    try:
        changed = await asyncio.to_thread(filament.suppliers.update_product, product_id, body)
    except ProductExistsError as exc:
        raise _conflict(exc) from None
    except ProductRefError as exc:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    if changed is None:
        raise _no_product(product_id)
    return changed


@router.get(
    "/products/{product_id}/prices",
    response_model=list[pm.PricePoint],
    summary="List a product's price points",
    description=(
        "Newest `on_date` first. `price` is per pack with tax; `price_per_kg` is derived "
        "from the pack size and the type's label weight. 404 for no such product."
    ),
)
async def list_prices(filament: FilamentDep, product_id: UUID) -> list[pm.PricePoint]:
    if await asyncio.to_thread(filament.suppliers.product, product_id) is None:
        raise _no_product(product_id)
    return await asyncio.to_thread(filament.suppliers.prices, product_id)


@router.post(
    "/products/{product_id}/prices",
    response_model=pm.PricePoint,
    status_code=status.HTTP_201_CREATED,
    summary="Add a manual price point",
    description=(
        "A price per pack, with tax, as a decimal string. `currency` defaults to the shop's "
        "(`orders_currency`) and `on_date` to today (UTC). The point's source is `manual`. "
        "An archived product still takes a price, and a currency other than the shop's is "
        "kept as sent (Decision 6). 404 for no such product, 422 for a bad field."
    ),
)
async def add_price(
    filament: FilamentDep, product_id: UUID, body: pm.PricePointCreate
) -> pm.PricePoint:
    made = await asyncio.to_thread(filament.suppliers.add_price, product_id, body)
    if made is None:
        raise _no_product(product_id)
    return made


@router.delete(
    "/prices/{price_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a manual price point",
    description=(
        "Only a point of source `manual`. 404 for no such point, 409 for one an invoice made "
        "(it goes with its invoice)."
    ),
)
async def delete_price(filament: FilamentDep, price_id: UUID) -> Response:
    try:
        gone = await asyncio.to_thread(filament.suppliers.delete_price, price_id)
    except PriceFromInvoiceError as exc:
        raise _conflict(exc) from None
    if not gone:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no price point {price_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/types/{type_id}/prices",
    response_model=list[pm.TypePricePoint],
    summary="Read a spool type's price history",
    description=(
        "Every price point of the type's supplier products, newest first, with the supplier "
        "and SKU. Points of archived products and suppliers stay, so the history stays true; "
        "`product_archived` and `supplier_archived` mark them. 404 for no such type."
    ),
)
async def type_prices(filament: FilamentDep, type_id: UUID) -> list[pm.TypePricePoint]:
    if await asyncio.to_thread(filament.types.get, type_id) is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no spool type {type_id}")
    return await asyncio.to_thread(filament.suppliers.type_prices, type_id)


# -- invoices ---------------------------------------------------------------------------

_INVOICE_ERRORS = (InvoiceRefError, MixedLineTaxError, InvoiceMathError)


def _invoice_error(exc: ValueError) -> ApiError:
    """The problem for what an invoice write refuses: a duplicate number carries the first
    invoice's id as `existing_invoice_id`; a confirmed one is 409; the rest 422."""
    if isinstance(exc, DuplicateInvoiceError):
        return ApiError(
            status.HTTP_409_CONFLICT, str(exc), existing_invoice_id=str(exc.existing_id)
        )
    if isinstance(exc, InvoiceConfirmedError | ImportRunChangedError):
        return _conflict(exc)
    return _unprocessable(exc)


@router.get(
    "/invoices",
    response_model=list[pm.Invoice],
    summary="List invoices",
    description=(
        "Newest `invoice_date` first. Filter by `supplier_id` and `status` (`draft` or "
        "`confirmed`). Each invoice carries `difference` (its total less its lines and "
        "shipping), `reconciles`, `line_count`, `spool_count` (the spools its lines made or "
        "filled) and its `file` pointer."
    ),
)
async def list_invoices(
    filament: FilamentDep, supplier_id: UUID | None = None, status: pm.InvoiceStatus | None = None
) -> list[pm.Invoice]:
    return await asyncio.to_thread(filament.invoices.list, supplier_id=supplier_id, status=status)


@router.post(
    "/invoices",
    response_model=pm.InvoiceDetail,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": pm.InvoiceConflict}},
    summary="Add an invoice draft",
    description=(
        "A draft from the header and optionally its `lines`. Tax and shipping are spread over "
        "the lines on every write: all lines give a `line_tax`, or none does and the invoice "
        "`tax` is spread by subtotal. A repeated `import_run_id` answers 200 with the first "
        "invoice and applies nothing. 409 when the supplier has an invoice with the number "
        "(the body's `existing_invoice_id` is that invoice), 422 for a missing or archived "
        "supplier, a missing type or product, a mixed line tax, a line whose spool list is "
        "longer than its quantity or whose total is out of range, or a bad field."
    ),
)
async def create_invoice(
    filament: FilamentDep, caller: CallerDep, body: pm.InvoiceCreate, response: Response
) -> pm.InvoiceDetail:
    try:
        made, new = await asyncio.to_thread(filament.invoices.create, body, actor=actor(caller))
    except (DuplicateInvoiceError, *_INVOICE_ERRORS) as exc:
        raise _invoice_error(exc) from None
    if not new:
        response.status_code = status.HTTP_200_OK
    return made


@router.get(
    "/invoices/{invoice_id}",
    response_model=pm.InvoiceDetail,
    summary="Read an invoice",
    description=(
        "The invoice with its lines, their costs and the spools made from them. 404 for none."
    ),
)
async def get_invoice(filament: FilamentDep, invoice_id: UUID) -> pm.InvoiceDetail:
    found = await asyncio.to_thread(filament.invoices.get, invoice_id)
    if found is None:
        raise _no_invoice(invoice_id)
    return found


@router.get(
    "/invoices/{invoice_id}/candidates",
    response_model=list[pm.LineCandidates],
    summary="List the spools each invoice line could update",
    description=(
        "Per line (by `position`), the spools (archived ones too, marked `archived`) whose "
        "effective material and exact RGBA match the line's type, or its proposed type when "
        "it has no type yet: `uncosted` (no cost per kg and no invoice line yet) and "
        "`costed`. A `costed` spool with no "
        "`invoice_line_id` may be filled, and the line's cost replaces its own; one with an "
        "`invoice_line_id` is refused at confirm. Archived (used-up) spools are listed too, "
        "with `archived` true: a spent spool was still bought. A line with neither type has "
        "none. 404 for no such invoice."
    ),
)
async def invoice_candidates(filament: FilamentDep, invoice_id: UUID) -> list[pm.LineCandidates]:
    found = await asyncio.to_thread(filament.invoices.candidates, invoice_id)
    if found is None:
        raise _no_invoice(invoice_id)
    return found


@router.get(
    "/bambu-colour-codes",
    response_model=ColourCode,
    summary="Look up a Bambu colour code",
    description=(
        "Bambu Studio's colour table, by the `code` digits of a Bambu spool's SKU: its "
        "`name`, its `rgba` list (`#RRGGBBAA`, one per colour; a translucent colour keeps "
        "its alpha, e.g. `80`), `fila_type` and `fila_id`. 404 for a code the table does "
        "not have, 503 when the image has no table (the body names the path)."
    ),
)
async def bambu_colour_code(state: StateDep, code: str) -> ColourCode:
    table = colour_codes(state.settings.slicer_profiles_dir)
    try:
        found = await asyncio.to_thread(table.lookup, code)
    except ColourTableMissingError as exc:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    if found is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no Bambu colour code {code}")
    return found


@router.patch(
    "/invoices/{invoice_id}",
    response_model=pm.InvoiceDetail,
    responses={409: {"model": pm.InvoiceConflict}},
    summary="Change an invoice",
    description=(
        "Only the fields sent change; the lines' tax and cost are worked out again. A "
        "confirmed invoice accepts only `note` (409 otherwise). `import_run_id` is the "
        "assistant session that imports the invoice; `if_import_run_id` makes the write "
        "apply only while the invoice's `import_run_id` is that value (`null` for none), "
        "else 409. A draft's `supplier_id` moves "
        "it to another unarchived supplier (its lines drop their supplier product). 404 for "
        "no such invoice, 409 for a number the supplier has (with `existing_invoice_id`), 422 "
        "for a bad field, a missing or archived supplier or a tax that cannot be spread."
    ),
)
async def patch_invoice(
    filament: FilamentDep, invoice_id: UUID, body: pm.InvoicePatch
) -> pm.InvoiceDetail:
    try:
        changed = await asyncio.to_thread(filament.invoices.update, invoice_id, body)
    except (
        DuplicateInvoiceError,
        InvoiceConfirmedError,
        ImportRunChangedError,
        *_INVOICE_ERRORS,
    ) as exc:
        raise _invoice_error(exc) from None
    if changed is None:
        raise _no_invoice(invoice_id)
    return changed


@router.put(
    "/invoices/{invoice_id}/lines",
    response_model=pm.InvoiceDetail,
    summary="Replace a draft's lines",
    description=(
        "The whole list of lines, in order. Tax and shipping are worked out again. 404 for no "
        "such invoice, 409 for a confirmed one, 422 for a mixed line tax, a missing type or "
        "product, or a bad field."
    ),
)
async def put_invoice_lines(
    filament: FilamentDep, invoice_id: UUID, body: list[pm.InvoiceLineIn]
) -> pm.InvoiceDetail:
    try:
        changed = await asyncio.to_thread(filament.invoices.replace_lines, invoice_id, body)
    except (InvoiceConfirmedError, *_INVOICE_ERRORS) as exc:
        raise _invoice_error(exc) from None
    if changed is None:
        raise _no_invoice(invoice_id)
    return changed


@router.delete(
    "/invoices/{invoice_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an invoice draft",
    description=(
        "A draft only; its file's hold is released. 404 for no such invoice, 409 for a "
        "confirmed one (archive its spools instead)."
    ),
)
async def delete_invoice(filament: FilamentDep, invoice_id: UUID) -> Response:
    try:
        gone = await asyncio.to_thread(filament.invoices.delete, invoice_id)
    except InvoiceConfirmedError as exc:
        raise _conflict(exc) from None
    if not gone:
        raise _no_invoice(invoice_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/invoices/{invoice_id}/confirm",
    response_model=pm.InvoiceDetail,
    summary="Confirm an invoice",
    description=(
        "Turns a draft into spools in one transaction: new spool types for lines that propose "
        "one, supplier products by SKU, each line's `quantity` spools (or fewer new ones when "
        "the line fills the spools in `update_spool_ids`) with a `cost_per_kg` from the line's "
        "allocated cost, and one price point per line. A filled spool that has its own cost and "
        "no invoice line takes the line's cost instead, with one `edited` event by the caller "
        "that records the old and new `cost_per_kg`. An archived spool takes its cost and "
        "invoice line only, and stays archived. A spool on another invoice line or of another "
        "type is refused. Confirming a confirmed invoice answers "
        "200 with it and makes nothing more. 404 for no such invoice. 409, with nothing "
        "written, for an invoice with no lines, one of an archived supplier, one not in the "
        "shop's currency, one whose total is more than 0.05 from its lines plus shipping, or a "
        "line that cannot be confirmed (the message names the line). After the commit it "
        "starts a 3DFP preset fetch for each new type that has a `tdfp_id` (`POST "
        "/filament/types/{type_id}/profile-fetch`); a fetch that cannot start never "
        "fails the confirm."
    ),
)
async def confirm_invoice(
    state: StateDep, filament: FilamentDep, caller: CallerDep, invoice_id: UUID
) -> pm.InvoiceDetail:
    who = actor(caller)
    try:
        done = await asyncio.to_thread(filament.invoices.confirm, invoice_id, actor=who)
    except (
        InvoiceNoLinesError,
        InvoiceCurrencyError,
        InvoiceSupplierArchivedError,
        InvoiceNotReconciledError,
        InvoiceLineError,
    ) as exc:
        raise _conflict(exc) from None
    if done is None:
        raise _no_invoice(invoice_id)
    invoice, _, made = done
    # After the commit; a failure to start is logged, never the confirm's (Decision 12).
    await start_profile_fetches(state, made, actor=who)
    return invoice


# -- an invoice's file ------------------------------------------------------------------

#: A second upload replaces the file at the same URL, so it is never `immutable` (ruling F2).
FILE_CACHE = "private, no-cache"


@router.post(
    "/invoices/{invoice_id}/file",
    response_model=pm.FileRef,
    summary="Attach a file to an invoice",
    description=(
        "A PDF or a photo, as one multipart `file` part up to `media_upload_max_bytes` "
        "(`GET /settings`). A second upload replaces the first. A confirmed invoice accepts "
        "a file. 404 for no such invoice (before a byte is read), 422 for no file, an empty "
        "one, or one that is not a PDF or a photo (judged by its bytes, never its name)."
    ),
    responses={413: {"description": "Larger than the upload limit in Settings"}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
)
async def put_invoice_file(
    invoice_id: UUID,
    request: Request,
    filament: FilamentDep,
    state: StateDep,
    paths: PathsDep,
) -> pm.FileRef:
    if await asyncio.to_thread(filament.invoices.get, invoice_id) is None:
        raise _no_invoice(invoice_id)
    received = await media_api.receive_upload(request, paths.cache)
    try:
        path = received.files.get("file")
        if path is None or received.sizes["file"] == 0:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "the upload has no `file` part, or it is empty",
            )
        name = attachments.clean_filename(received.filenames.get("file", ""))
        with path.open("rb") as handle:
            kind, media_type = attachments.sniff(handle.read(attachments.SNIFF_BYTES), name)
            if kind not in ("document", "image"):
                raise ApiError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "an invoice's file is a PDF or a photo",
                )
            handle.seek(0)
            key = await attachments.keep_file(
                state.store.blobs, handle, received.hashes["file"].hexdigest()
            )
        size = received.sizes["file"]
    finally:
        received.discard()
    made = await asyncio.to_thread(
        filament.invoices.set_file, invoice_id, key=key, name=name, media_type=media_type, size=size
    )
    if made is None:
        raise _no_invoice(invoice_id)
    return made


@router.get(
    "/invoices/{invoice_id}/file",
    response_class=FileResponse,
    responses={200: {"content": {"*/*": {}}}, 404: {"description": "No file, or it is gone"}},
    summary="An invoice's file",
    description=(
        "The PDF or photo, inline, under its name. Not cached: a new upload replaces it at "
        "this URL. 404 for no such invoice, no file, or a file the store has lost."
    ),
)
async def get_invoice_file(
    invoice_id: UUID, filament: FilamentDep, state: StateDep
) -> FileResponse:
    found = await asyncio.to_thread(filament.invoices.file_key, invoice_id)
    if found is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"invoice {invoice_id} has no file")
    key, ref = found
    path = await attachments.blob_path(state.store.blobs, key)
    if path is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"the file of invoice {invoice_id} is gone")
    headers = {"Cache-Control": FILE_CACHE, **NOSNIFF}
    if ref.media_type != "application/pdf":
        headers |= INERT
    return FileResponse(
        path,
        media_type=ref.media_type,
        filename=ref.name,
        content_disposition_type="inline",
        headers=headers,
    )


# -- the shopping list ------------------------------------------------------------------


class ShoppingRemoved(BaseModel):
    removed: int


@router.get(
    "/shopping-list",
    response_model=list[pm.ShoppingItem],
    summary="The shopping list",
    description=(
        "The spools to buy: pending items first, then the newest. Only one `status` "
        "(`pending` or `purchased`) when given."
    ),
)
async def list_shopping(
    filament: FilamentDep, status: pm.ShoppingStatus | None = None
) -> list[pm.ShoppingItem]:
    return await asyncio.to_thread(filament.shopping.list, status=status)


@router.post(
    "/shopping-list",
    response_model=pm.ShoppingItem,
    status_code=status.HTTP_201_CREATED,
    summary="Add to the shopping list",
    description=(
        "A spool to buy, by `type_id` (its material, subtype, brand and colour name fill what "
        "the body leaves out) or by `material` and any of `subtype`, `brand`, `colour_name`. "
        "`quantity_spools` is 1 to 1000, default 1. 422 for neither a type nor a material, "
        "for a bad quantity, or for a type that does not exist."
    ),
)
async def add_shopping(filament: FilamentDep, body: pm.ShoppingItemCreate) -> pm.ShoppingItem:
    try:
        return await asyncio.to_thread(filament.shopping.add, body)
    except ShoppingRefError as exc:
        raise _unprocessable(exc) from None


@router.delete(
    "/shopping-list",
    response_model=ShoppingRemoved,
    summary="Clear the shopping list",
    description=(
        "Remove every item, or only the purchased ones with `purchased_only=true`. "
        "Answers how many it `removed`."
    ),
)
async def clear_shopping(filament: FilamentDep, purchased_only: bool = False) -> ShoppingRemoved:
    count = await asyncio.to_thread(filament.shopping.clear, purchased_only=purchased_only)
    return ShoppingRemoved(removed=count)


@router.patch(
    "/shopping-list/{item_id}",
    response_model=pm.ShoppingItem,
    summary="Change a shopping list item",
    description=(
        "Only the fields sent change: `status` (`purchased` stamps `purchased_at`, `pending` "
        "clears it), `quantity_spools` and `note` (`null` clears it). 404 for none, 422 for "
        "a bad value."
    ),
)
async def update_shopping(
    filament: FilamentDep, item_id: UUID, body: pm.ShoppingItemPatch
) -> pm.ShoppingItem:
    found = await asyncio.to_thread(filament.shopping.update, item_id, body)
    if found is None:
        raise _no_item(item_id)
    return found


@router.delete(
    "/shopping-list/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a shopping list item",
    description="404 for none.",
)
async def remove_shopping(filament: FilamentDep, item_id: UUID) -> Response:
    if not await asyncio.to_thread(filament.shopping.remove, item_id):
        raise _no_item(item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
