"""Links on any entity (#6950, spec 6.3): ``/entities/{kind}:{ref}/links``.

Any registered kind takes links, an order's included: a link has no order rules. A print
archive's are not kept here (422 ``unknown_entity_kind``).

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Response, status
from pydantic import BaseModel

from scadbuddy.entities.component import EntitiesDep
from scadbuddy.entities.links import EntityLink, LinkInput, LinkPatch
from scadbuddy.entities.problems import (
    checked_url,
    entity_not_found,
    link_not_found,
    parsed,
    unknown_kind,
)
from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.registry import KINDS, EntityNotFound
from scadbuddy.orders.actor import current_actor

router = APIRouter(prefix="/entities", tags=["entities"])

RefPath = Annotated[
    str, Path(description="The entity, `kind:ref`: `spool:<id>`, `printer:<id>`, …")
]
_CODES = "`bad_entity_ref`, `unknown_entity_kind` or `bad_link_url`"


class EntityLinkList(BaseModel):
    links: list[EntityLink]


def _entity(text: str) -> EntityRef:
    ref = parsed(text)
    if ref.kind not in KINDS:
        raise unknown_kind(ref)
    return ref


async def _exists(entities: EntitiesDep, ref: EntityRef) -> None:
    def there() -> bool:
        with entities.opened().connection() as conn:
            return entities.store.exists(conn, ref)

    if not await asyncio.to_thread(there):
        raise entity_not_found(ref)


def _link_id(ref: EntityRef, link_id: str) -> UUID:
    try:
        return UUID(link_id)
    except ValueError:
        raise link_not_found(ref, link_id) from None


@router.get(
    "/{ref}/links",
    response_model=EntityLinkList,
    summary="An entity's links",
    description="By `position`, then oldest first.",
    responses={
        404: {"description": "No such entity (`entity_not_found`)"},
        422: {"description": "`bad_entity_ref` or `unknown_entity_kind`"},
    },
)
async def list_entity_links(ref: RefPath, entities: EntitiesDep) -> EntityLinkList:
    entity = _entity(ref)
    await _exists(entities, entity)
    return EntityLinkList(links=await asyncio.to_thread(entities.links.list, entity))


@router.post(
    "/{ref}/links",
    response_model=EntityLink,
    summary="Add a link to an entity",
    description=(
        "A web page about it: a support page, a datasheet, a store listing. `url` is "
        "`http` or `https` with a host (422 `bad_link_url`). `kind` is open text "
        "(`^[a-z_]{1,32}$`, `other` when left out). `position` puts it there and moves the "
        "rest down; left out, it goes last."
    ),
    responses={
        404: {"description": "No such entity (`entity_not_found`)"},
        422: {"description": _CODES},
    },
)
async def add_entity_link(ref: RefPath, body: LinkInput, entities: EntitiesDep) -> EntityLink:
    entity = _entity(ref)
    checked_url(body.url)
    try:
        return await asyncio.to_thread(entities.links.add, entity, body, current_actor())
    except EntityNotFound:
        raise entity_not_found(entity) from None


@router.patch(
    "/{ref}/links/{link_id}",
    response_model=EntityLink,
    summary="Change an entity's link",
    description=(
        "A field left out stays. A person's change makes the link `manual`, so an import "
        "never replaces it."
    ),
    responses={
        404: {"description": "No such link of the entity (`link_not_found`)"},
        422: {"description": _CODES},
    },
)
async def patch_entity_link(
    ref: RefPath, link_id: str, body: LinkPatch, entities: EntitiesDep
) -> EntityLink:
    entity = _entity(ref)
    if body.url is not None:
        checked_url(body.url)
    found = _link_id(entity, link_id)
    try:
        return await asyncio.to_thread(entities.links.update, entity, found, body)
    except LookupError:
        raise link_not_found(entity, link_id) from None


@router.delete(
    "/{ref}/links/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove an entity's link",
    responses={404: {"description": "No such link of the entity (`link_not_found`)"}},
)
async def delete_entity_link(ref: RefPath, link_id: str, entities: EntitiesDep) -> Response:
    entity = _entity(ref)
    found = _link_id(entity, link_id)
    try:
        await asyncio.to_thread(entities.links.delete, entity, found)
    except LookupError:
        raise link_not_found(entity, link_id) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
