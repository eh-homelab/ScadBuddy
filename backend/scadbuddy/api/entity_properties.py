"""Key-value properties on any entity (#7026, spec 6.3a): ``/entities/{kind}:{ref}/properties``
and the per-kind reads and schema under ``/entity-kinds/{kind}``.

A kind's schema documents and types the keys it knows and never refuses a key. A print
archive takes no properties (422 ``unknown_entity_kind``).

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.core.problems import ApiError
from scadbuddy.entities.component import EntitiesDep
from scadbuddy.entities.problems import (
    bad_property_schema,
    bad_property_value,
    entity_not_found,
    missing_required,
    no_properties_kind,
    parsed,
    schema_owned_by_item_kind,
)
from scadbuddy.entities.properties import (
    ITEM,
    BadPropertySchema,
    BadPropertyValue,
    EntityProperty,
    MissingRequiredProperties,
    PropertyChange,
    PropertyPatch,
    PropertyValue,
    SchemaChange,
    SchemaOwnedByKind,
    schema_for,
)
from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.registry import KINDS, EntityNotFound
from scadbuddy.inventory.component import InventoryDep
from scadbuddy.inventory.kinds import keys_in_use
from scadbuddy.orders.actor import current_actor

router = APIRouter(tags=["entities"])

RefPath = Annotated[
    str, Path(description="The entity, `kind:ref`: `spool_type:<id>`, `printer:<id>`, …")
]
KindPath = Annotated[str, Path(description="An entity kind: `spool_type`, `printer`, …")]
_CODES = "`bad_entity_ref` or `unknown_entity_kind`"
_RULES = (
    "A key is `^[a-z][a-z0-9_]{0,63}$`. A value is a string (up to 2000 characters), a "
    "finite number, a boolean or a list of up to 50 strings of up to 500 characters each. "
    "An entity has up to 200 keys. "
    "A key the kind's schema knows is checked against its subschema; any other is stored "
    "as given."
)


class EntityPropertyList(BaseModel):
    properties: list[EntityProperty]


class KindProperties(BaseModel):
    properties: dict[str, dict[str, PropertyValue]]


class KeyInUse(BaseModel):
    key: str
    count: int


class KindPropertySchema(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: str
    schema_: dict[str, Any] = Field(alias="schema")
    keys_in_use: list[KeyInUse]


class PropertyHistory(BaseModel):
    changes: list[PropertyChange]


class SchemaHistory(BaseModel):
    changes: list[SchemaChange]


LimitQuery = Annotated[int, Query(ge=1, le=500, description="At most this many changes")]
_HISTORY_LAG = (
    " The history subscriber writes a change about two seconds after its commit, so a "
    "change just made may not be listed yet."
)


class SchemaBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: dict[str, Any] = Field(alias="schema")


def _entity(text: str) -> EntityRef:
    ref = parsed(text)
    _kind(ref.kind)
    return ref


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise no_properties_kind(kind)
    return kind


async def _exists(entities: EntitiesDep, ref: EntityRef) -> None:
    def there() -> bool:
        with entities.opened().connection() as conn:
            return entities.store.exists(conn, ref)

    if not await asyncio.to_thread(there):
        raise entity_not_found(ref)


async def _schema_view(entities: EntitiesDep, kind: str) -> KindPropertySchema:
    schema = await asyncio.to_thread(entities.properties.schema, kind)
    keys = await asyncio.to_thread(entities.properties.keys_in_use, kind)
    return KindPropertySchema(
        kind=kind, schema=schema, keys_in_use=[KeyInUse(key=k, count=n) for k, n in keys]
    )


@router.get(
    "/entities/{ref}/properties",
    response_model=EntityPropertyList,
    summary="An entity's properties",
    description="By key. " + _RULES,
    responses={
        404: {"description": "No such entity (`entity_not_found`)"},
        422: {"description": _CODES},
    },
)
async def list_entity_properties(ref: RefPath, entities: EntitiesDep) -> EntityPropertyList:
    entity = _entity(ref)
    await _exists(entities, entity)
    return EntityPropertyList(properties=await asyncio.to_thread(entities.properties.list, entity))


@router.get(
    "/entities/{ref}/property-schema",
    response_model=KindPropertySchema,
    summary="The property schema of one entity",
    description=(
        "The schema that types this entity's properties, for every kind: an item's is its "
        "item kind's `attribute_schema` merged down the kind tree (each ancestor's keys, the "
        "kind's own last); any other entity's is its kind's schema. `kind` is the entity's "
        "kind. `keys_in_use` lists each key entities typed by the same schema have, with "
        "how many: for an item, the items of its item kind and of its descendants."
    ),
    responses={
        404: {"description": "No such entity (`entity_not_found`)"},
        422: {"description": _CODES},
    },
)
async def get_entity_property_schema(
    ref: RefPath, entities: EntitiesDep, inventory: InventoryDep
) -> KindPropertySchema:
    entity = _entity(ref)
    await _exists(entities, entity)

    def schema() -> dict[str, Any]:
        with entities.opened().connection() as conn:
            return schema_for(conn, entity)

    found = await asyncio.to_thread(schema)
    if entity.kind == ITEM:
        # The item's kind is scadbuddy-core's to read (#6949 K1, C16); the keys are rows.
        try:
            item = await inventory.core.read(f"/items/{UUID(entity.ref)}")
        except ApiError as missing:
            if missing.status == status.HTTP_404_NOT_FOUND:
                raise entity_not_found(entity) from None
            raise

        def keys_of_kind() -> list[tuple[str, int]]:
            with entities.opened().connection() as conn:
                return keys_in_use(conn, str(item["kind_id"]))

        keys = await asyncio.to_thread(keys_of_kind)
    else:
        keys = await asyncio.to_thread(entities.properties.keys_in_use, entity.kind)
    return KindPropertySchema(
        kind=entity.kind, schema=found, keys_in_use=[KeyInUse(key=k, count=n) for k, n in keys]
    )


@router.patch(
    "/entities/{ref}/properties",
    response_model=EntityPropertyList,
    summary="Set or remove an entity's properties",
    description=(
        "`set` maps keys to values; a key set to `null` is removed. The whole patch applies "
        "or none of it. A person's or the agent's write makes a key `manual`. "
        + _RULES
        + " A bad key or value is 422 `bad_property_value`, naming the key and the reason. A "
        "malformed body (an empty `set`, an unknown field) is the generic request-validation "
        "422, not `bad_property_value`."
    ),
    responses={
        404: {"description": "No such entity (`entity_not_found`)"},
        422: {
            "description": _CODES
            + ", `bad_property_value`, or `missing_required_properties` for a patch that"
            " removes or empties a required key that has a value. An item's required keys"
            " are its item kind's and its special type's: a printer item's `model_code`, a"
            " filament spool item's `rgba` and `diameter_mm` (R-K1-33)"
        },
    },
)
async def patch_entity_properties(
    ref: RefPath, body: PropertyPatch, entities: EntitiesDep
) -> EntityPropertyList:
    entity = _entity(ref)
    try:
        found = await asyncio.to_thread(entities.properties.apply, entity, body, current_actor())
    except EntityNotFound:
        raise entity_not_found(entity) from None
    except BadPropertyValue as e:
        raise bad_property_value(e.key, e.reason) from None
    except MissingRequiredProperties as e:
        raise missing_required(e) from None
    return EntityPropertyList(properties=found)


@router.get(
    "/entities/{ref}/properties/history",
    response_model=PropertyHistory,
    summary="The history of an entity's properties",
    description=(
        "Each set and remove of the entity's keys (only `key`'s when given), newest first, "
        "with its old and new value, source, time and actor. A remove's `new` and `source` "
        "are null. The history stays after the entity goes, so an entity with none, or "
        "one that is gone, lists what there is." + _HISTORY_LAG
    ),
    responses={422: {"description": _CODES + ", or a `limit` outside 1 to 500"}},
)
async def get_entity_property_history(
    ref: RefPath,
    entities: EntitiesDep,
    key: str | None = None,
    limit: LimitQuery = 100,
) -> PropertyHistory:
    entity = _entity(ref)
    changes = await asyncio.to_thread(entities.properties.history, entity, key, limit)
    return PropertyHistory(changes=changes)


@router.get(
    "/entity-kinds/{kind}/properties",
    response_model=KindProperties,
    summary="The properties of every entity of a kind",
    description=(
        "Every entity of the kind that has properties, as its ref to its keys and values. "
        "Not capped: a list screen loads the kind's entities whole (hundreds in a shop), "
        "and this read is one row per key of those."
    ),
    responses={422: {"description": "`unknown_entity_kind`"}},
)
async def list_kind_properties(kind: KindPath, entities: EntitiesDep) -> KindProperties:
    found = await asyncio.to_thread(entities.properties.of_kind, _kind(kind))
    return KindProperties(properties=found)


@router.get(
    "/entity-kinds/{kind}/property-schema",
    response_model=KindPropertySchema,
    summary="A kind's property schema",
    description=(
        "A Draft 2020-12 object schema that documents and types the keys the kind knows. "
        "It never refuses a key. `keys_in_use` lists each key entities of the kind have, "
        "with how many. An item's schema is its item kind's `attribute_schema`, so `item` "
        "is 409 `schema_owned_by_item_kind`."
    ),
    responses={
        409: {"description": "`schema_owned_by_item_kind`"},
        422: {"description": "`unknown_entity_kind`"},
    },
)
async def get_property_schema(kind: KindPath, entities: EntitiesDep) -> KindPropertySchema:
    try:
        return await _schema_view(entities, _kind(kind))
    except SchemaOwnedByKind:
        raise schema_owned_by_item_kind() from None


@router.get(
    "/entity-kinds/{kind}/property-schema/history",
    response_model=SchemaHistory,
    summary="The history of a kind's property schema",
    description=(
        "Each write of the kind's schema, newest first, with the schema before and after "
        "it, its time and actor. An item's schema is its item kind's `attribute_schema`, so "
        "`item` is 409 `schema_owned_by_item_kind`." + _HISTORY_LAG
    ),
    responses={
        409: {"description": "`schema_owned_by_item_kind`"},
        422: {"description": "`unknown_entity_kind`, or a `limit` outside 1 to 500"},
    },
)
async def get_property_schema_history(
    kind: KindPath, entities: EntitiesDep, limit: LimitQuery = 100
) -> SchemaHistory:
    name = _kind(kind)
    if name == ITEM:
        raise schema_owned_by_item_kind()
    changes = await asyncio.to_thread(entities.properties.schema_history, f"kind:{name}", limit)
    return SchemaHistory(changes=changes)


@router.put(
    "/entity-kinds/{kind}/property-schema",
    response_model=KindPropertySchema,
    summary="Set a kind's property schema",
    description=(
        "Replaces the schema. It is a Draft 2020-12 object schema in a small subset that "
        "documents keys and never refuses one. Top level: `type` `object`, `properties`, "
        "`additionalProperties` `true`, `$schema` (the 2020-12 URI), `$comment`, `title`, "
        "`description`, `examples`, and, for a spool, a spool type or a filament catalog kind "
        "only, `required` (distinct keys of `properties`, #7703). Per key: `type` (string, "
        "number, integer, boolean, array, "
        'or a list of them with `"null"`), annotations, `enum`, `const`, number and length '
        "bounds, `format` (an annotation, not `regex`), `items` (strings), `minItems`, "
        "`maxItems`, `uniqueItems`; an `enum` or `const` must fit its key, and no bound may "
        "cross its pair or a cap. Anything else is 422 "
        "`bad_property_schema`, naming the keyword, and the old schema stays. The PUT rewrites "
        "and refuses no stored value; for the kinds that may hold `required`, reads mark a "
        "stored value that no longer fits (`invalid_properties`) and the row's next checked "
        "save refuses it. An item's schema is its item kind's "
        "`attribute_schema`, so `item` is 409 `schema_owned_by_item_kind`."
    ),
    responses={
        409: {"description": "`schema_owned_by_item_kind`"},
        422: {"description": "`unknown_entity_kind` or `bad_property_schema`"},
    },
)
async def put_property_schema(
    kind: KindPath, body: SchemaBody, entities: EntitiesDep
) -> KindPropertySchema:
    name = _kind(kind)
    try:
        await asyncio.to_thread(entities.properties.set_schema, name, body.schema_, current_actor())
    except BadPropertySchema as e:
        raise bad_property_schema(e.reason) from None
    except SchemaOwnedByKind:
        raise schema_owned_by_item_kind() from None
    return await _schema_view(entities, name)
