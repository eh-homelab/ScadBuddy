"""`/api/v1/customers` (#4249, spec 2026-10-18 §3.1, §4): plain reads and writes of the
customers and their contact points and addresses.

Bodies carry the nested lists whole; a row the body names by ``id`` is kept (updated in
place), one without an ``id`` is inserted, and the rest are deleted (plan Ruling 11), so
an address an open order points at keeps its id. No body carries an author or a status
(Ruling 12). Unknown query parameters answer 400, as the Activity list's do.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scadbuddy.api.activity import _no_nul, _unknown
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.models import AddressInput, ContactInput, Customer
from scadbuddy.orders.store import UNSET, OrderStore, RowNotOwnedError, UnsetType

router = APIRouter(prefix="/customers", tags=["customers"])

#: Ruling 9: the list is bounded; the pickers search by `q` rather than scroll.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
LIST_PARAMS = frozenset({"q", "include_archived", "limit"})


def _one_primary_per_kind(contacts: list[ContactInput] | None) -> None:
    seen: set[str] = set()
    for contact in contacts or []:
        if contact.primary:
            if contact.kind in seen:
                raise ValueError(f"contacts: more than one primary {contact.kind}")
            seen.add(contact.kind)


def _one_default_per_role(addresses: list[AddressInput] | None) -> None:
    for role in ("default_shipping", "default_billing"):
        if sum(1 for address in addresses or [] if getattr(address, role)) > 1:
            raise ValueError(f"addresses: more than one {role} address")


class CustomerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    notes: str = ""
    tags: list[str] = Field(default_factory=list)
    contacts: list[ContactInput] = Field(default_factory=list)
    addresses: list[AddressInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def _checked(self) -> "CustomerCreate":
        _one_primary_per_kind(self.contacts)
        _one_default_per_role(self.addresses)
        contact_ids = [c.id for c in self.contacts if c.id is not None]
        address_ids = [a.id for a in self.addresses if a.id is not None]
        if contact_ids or address_ids:
            raise ValueError("a new customer's contacts and addresses have no ids yet")
        return self


class CustomerPatch(BaseModel):
    """Each field given replaces the customer's; a list given replaces the rows whole
    (by id, Ruling 11); one left out is untouched."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    company: str | UnsetType | None = UNSET
    notes: str | None = None
    tags: list[str] | None = None
    contacts: list[ContactInput] | None = None
    addresses: list[AddressInput] | None = None

    @model_validator(mode="after")
    def _checked(self) -> "CustomerPatch":
        _one_primary_per_kind(self.contacts)
        _one_default_per_role(self.addresses)
        return self


def _not_found(customer_id: str) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no customer {customer_id!r}")


def _duplicate_primary(error: psycopg.errors.UniqueViolation) -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "contacts: at most one primary contact per kind",
        errors=[{"loc": ["body", "contacts"], "msg": str(error.diag.message_primary)}],
    )


@router.get("", response_model=list[Customer], summary="List customers")
async def list_customers(
    request: Request,
    orders: OrdersDep,
    q: str | None = None,
    include_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> list[Customer]:
    """By name, at most ``limit``; ``q`` matches the name, the company and any contact
    point. Archived customers are left out unless asked for."""
    _no_nul(request)
    _unknown(request, LIST_PARAMS)
    return await asyncio.to_thread(
        orders.store.customers, q=q, include_archived=include_archived, limit=limit
    )


@router.post(
    "", response_model=Customer, status_code=status.HTTP_201_CREATED, summary="Create a customer"
)
async def create_customer(body: CustomerCreate, orders: OrdersDep) -> Customer:
    try:
        return await asyncio.to_thread(
            orders.store.insert_customer,
            display_name=body.display_name,
            company=body.company,
            notes=body.notes,
            tags=body.tags,
            contacts=body.contacts,
            addresses=body.addresses,
        )
    except psycopg.errors.UniqueViolation as error:
        raise _duplicate_primary(error) from None


@router.get("/{customer_id}", response_model=Customer, summary="One customer")
async def get_customer(customer_id: str, orders: OrdersDep) -> Customer:
    _no_nul_path(customer_id)
    customer = await asyncio.to_thread(orders.store.customer, customer_id)
    if customer is None:
        raise _not_found(customer_id)
    return customer


def _patch(store: OrderStore, customer_id: str, body: CustomerPatch) -> Customer | None:
    with store.transaction() as conn:
        fields: dict[str, Any] = {}
        if body.display_name is not None:
            fields["display_name"] = body.display_name
        if not isinstance(body.company, UnsetType):
            fields["company"] = body.company
        if body.notes is not None:
            fields["notes"] = body.notes
        if body.tags is not None:
            fields["tags"] = body.tags
        customer = store.update_customer(customer_id, conn=conn, **fields)
        if customer is None:
            return None
        if body.contacts is not None:
            store.replace_contacts(conn, customer_id, body.contacts)
        if body.addresses is not None:
            store.replace_addresses(conn, customer_id, body.addresses)
        return store.customer(customer_id, conn=conn)


@router.patch("/{customer_id}", response_model=Customer, summary="Edit a customer")
async def patch_customer(customer_id: str, body: CustomerPatch, orders: OrdersDep) -> Customer:
    _no_nul_path(customer_id)
    try:
        customer = await asyncio.to_thread(_patch, orders.store, customer_id, body)
    except RowNotOwnedError as error:
        field = "contacts" if error.table == "contact" else "addresses"
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{field}: {error.row_id} is not a row of this customer",
            errors=[{"loc": ["body", field], "msg": str(error)}],
        ) from None
    except psycopg.errors.UniqueViolation as error:
        raise _duplicate_primary(error) from None
    if customer is None:
        raise _not_found(customer_id)
    return customer


@router.post("/{customer_id}/archive", response_model=Customer, summary="Archive a customer")
async def archive_customer(customer_id: str, orders: OrdersDep) -> Customer:
    """Hides the customer from the pickers; their orders keep naming them (§3.1)."""
    _no_nul_path(customer_id)
    customer = await asyncio.to_thread(orders.store.archive_customer, customer_id)
    if customer is None:
        raise _not_found(customer_id)
    return customer


@router.post("/{customer_id}/unarchive", response_model=Customer, summary="Unarchive a customer")
async def unarchive_customer(customer_id: str, orders: OrdersDep) -> Customer:
    """Back in the pickers, so a new order can name them again (§3.1)."""
    _no_nul_path(customer_id)
    customer = await asyncio.to_thread(orders.store.unarchive_customer, customer_id)
    if customer is None:
        raise _not_found(customer_id)
    return customer


def _no_nul_path(*values: str) -> None:
    if any("\x00" in value for value in values):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "a path value holds a NUL byte")
