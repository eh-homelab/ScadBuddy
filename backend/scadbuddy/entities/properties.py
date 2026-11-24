"""Key-value properties on any entity (#7026, spec 6.3a): rows of `entity_properties`, one
per entity and key. A value is a string, a finite number, a boolean or a list of strings.
A kind's schema (`entity_property_schemas`) documents and types the keys it knows and never
refuses a key: it uses only an allowlisted subset of JSON Schema, a known key's value is
checked against its subschema, and any other key is stored as given. A person's or the
agent's write makes a key `manual`."""

from __future__ import annotations

import builtins
import functools
import json
import math
import re
from datetime import datetime
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, best_match
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.registry import KINDS, EntityNotFound, EntityStore, UnknownEntityKind
from scadbuddy.orders.models import Actor

Conn = Connection[DictRow]

KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
MAX_KEYS = 200
MAX_STRING = 2000
MAX_LIST = 50
MAX_LIST_ITEM = 500
#: The only keywords a schema may use at its top level. `examples` holds data.
TOP_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "additionalProperties",
        "$schema",
        "$comment",
        "title",
        "description",
        "examples",
    }
)
#: The only keywords a known key's subschema may use. `enum`, `const`, `default` and
#: `examples` hold data. No keyword here can refuse another key or run a regex.
KEY_KEYWORDS = frozenset(
    {
        "type",
        "title",
        "description",
        "$comment",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "enum",
        "const",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "format",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)
#: The keywords an `items` subschema may use: a list holds no list.
ITEM_KEYWORDS = KEY_KEYWORDS - {"items"}
#: The types a known key's subschema may name; "null" only beside another, in a list.
KEY_TYPES = frozenset({"string", "number", "integer", "boolean", "array"})
#: The types an `items` subschema may name: a list holds strings only.
ITEM_TYPES = frozenset({"string"})
#: The only `$schema` a schema may name.
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

PropertyValue = str | float | int | bool | list[str]


class EntityProperty(BaseModel):
    key: str
    value: PropertyValue
    #: ``manual`` for a person's or the agent's write; an import's name otherwise.
    source: str
    updated_at: datetime
    updated_by: Actor


class PropertyPatch(BaseModel):
    """The keys to set; a key set to None is removed. The store checks the keys and values,
    so every bad one is one error, `BadPropertyValue`."""

    model_config = ConfigDict(extra="forbid")

    set: dict[str, JsonValue] = Field(min_length=1, max_length=MAX_KEYS)


class BadPropertyValue(ValueError):  # noqa: N818
    """A key or value outside the rules; `key` is ``*`` for too many keys."""

    def __init__(self, key: str, reason: str) -> None:
        super().__init__(f"{key}: {reason}")
        self.key = key
        self.reason = reason


class BadPropertySchema(ValueError):  # noqa: N818
    """A schema that is not a valid object schema, or one that uses a keyword outside the
    allowed subset."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


NUL_PROBLEM = "a string may not hold U+0000"  # Postgres jsonb refuses it


def _has_nul(node: Any) -> bool:
    if isinstance(node, str):
        return "\u0000" in node
    if isinstance(node, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in node.items())
    if isinstance(node, list):
        return any(_has_nul(v) for v in node)
    return False


def _shape_problem(value: JsonValue) -> str | None:
    """Why `value` is none of the four shapes, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            finite = math.isfinite(value)
        except OverflowError:  # an int past a double's range
            finite = False
        return None if finite else "a number must be finite and fit a double"
    if isinstance(value, str):
        if "\u0000" in value:
            return NUL_PROBLEM
        return None if len(value) <= MAX_STRING else f"at most {MAX_STRING} characters"
    if isinstance(value, list):
        if len(value) > MAX_LIST:
            return f"a list holds at most {MAX_LIST} strings"
        for item in value:
            if not isinstance(item, str):
                return "a list holds strings only"
            if "\u0000" in item:
                return NUL_PROBLEM
            if len(item) > MAX_LIST_ITEM:
                return f"a list's string is at most {MAX_LIST_ITEM} characters"
        return None
    return "a value is a string, a number, a boolean or a list of strings"


def _checked(patch: PropertyPatch, schema: dict[str, Any]) -> None:
    known = schema.get("properties") or {}
    for key, value in patch.set.items():
        if KEY_PATTERN.fullmatch(key) is None:
            raise BadPropertyValue(key, "a key is ^[a-z][a-z0-9_]{0,63}$")
        if value is None:
            continue
        problem = _shape_problem(value)
        if problem is not None:
            raise BadPropertyValue(key, problem)
        if key in known:
            validator = _validator(json.dumps(known[key], sort_keys=True))
            error = best_match(validator.iter_errors(value))
            if error is not None:
                raise BadPropertyValue(key, error.message)


@functools.lru_cache(maxsize=256)
def _validator(canonical: str) -> Draft202012Validator:
    """The validator of a known key's subschema, by its sorted-keys JSON, so a patch does
    not build it again."""
    return Draft202012Validator(json.loads(canonical))


def check_schema(schema: dict[str, Any]) -> None:
    """`BadPropertySchema` unless `schema` is a valid Draft 2020-12 object schema in the
    allowed subset, so it refuses no key and runs no regex. It also refuses a U+0000 in
    any string or object key, which Postgres jsonb cannot store. It does not prove that
    every known key takes some value; it checks only the cases below. The top level uses
    only `TOP_KEYWORDS`, with `additionalProperties` only true and `$schema`, when given,
    `DRAFT_2020_12`. Each key's subschema is an object that uses only `KEY_KEYWORDS` and
    names only `KEY_TYPES` (or a list of them, with "null"). Its `items` uses only
    `ITEM_KEYWORDS` and names only `ITEM_TYPES`. In either, `enum` holds a value the key
    takes, `const` is one, no lower bound passes its upper bound, and no minimum length or
    count passes the value caps. A property name must be a key a value can be set
    under (`KEY_PATTERN`), and the keywords are checked before the metaschema runs, so no
    pattern a person sent is ever compiled. Other empty schemas, such as an integer between two
    exclusive bounds, are not found. `format` is an annotation only (it checks nothing),
    but "regex" is refused all the same. Any other keyword is refused."""
    if not isinstance(schema, dict):
        raise BadPropertySchema("a schema is a JSON object")
    if _has_nul(schema):
        raise BadPropertySchema("a schema may not hold U+0000 in a string or a key")
    # Keywords first, so the metaschema never compiles a pattern a person sent.
    _keywords_only(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        raise BadPropertySchema(f"not a valid Draft 2020-12 schema: {e.message}") from None
    _only(schema, TOP_KEYWORDS, "at the top level")
    if schema.get("type") != "object":
        raise BadPropertySchema('a schema\'s "type" is "object"')
    if "additionalProperties" in schema and schema["additionalProperties"] is not True:
        raise BadPropertySchema('"additionalProperties" may only be true')
    if "$schema" in schema and schema["$schema"] != DRAFT_2020_12:
        raise BadPropertySchema(f'"$schema" may only be "{DRAFT_2020_12}"')
    for key, sub in schema.get("properties", {}).items():
        if KEY_PATTERN.fullmatch(key) is None:
            raise BadPropertySchema(f"property '{key}' is no key a value can be set under")
        _check_key(sub, f"in property '{key}'", item=False)


def _keywords_only(schema: dict[str, Any]) -> None:
    """The allowlist of keyword names alone, before any value is looked at."""
    _only(schema, TOP_KEYWORDS, "at the top level")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise BadPropertySchema('"properties" is a JSON object')
    for key, sub in properties.items():
        if not isinstance(sub, dict):
            raise BadPropertySchema(f"the subschema in property '{key}' is a JSON object")
        _only(sub, KEY_KEYWORDS, f"in property '{key}'")
        items = sub.get("items")
        if isinstance(items, dict):
            _only(items, ITEM_KEYWORDS, f"in the items of property '{key}'")


def _only(node: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    for keyword in node:
        if keyword not in allowed:
            raise BadPropertySchema(f"keyword '{keyword}' is not allowed {where}")


def _takes(value: Any, item: bool) -> bool:
    """Whether a key (or, with `item`, a list's string) can hold `value`."""
    if item:
        return isinstance(value, str) and len(value) <= MAX_LIST_ITEM
    return value is not None and _shape_problem(value) is None


def _check_key(sub: Any, where: str, item: bool) -> None:
    """`BadPropertySchema` unless the key's (or, with `item`, its items') subschema `sub`
    is in the subset and leaves some value."""
    if not isinstance(sub, dict):
        raise BadPropertySchema(f"the subschema {where} is a JSON object")
    _only(sub, ITEM_KEYWORDS if item else KEY_KEYWORDS, where)
    types = ITEM_TYPES if item else KEY_TYPES
    if "type" in sub:
        named = sub["type"] if isinstance(sub["type"], list) else [sub["type"]]
        if not set(named) - {"null"} or not set(named) <= types | {"null"}:
            allowed = ", ".join(sorted(types))
            raise BadPropertySchema(
                f'the type {where} is one of {allowed}, or a list of them and "null"'
            )
    if sub.get("format") == "regex":
        raise BadPropertySchema(f"format 'regex' is not allowed {where}")
    if "enum" in sub or "const" in sub:
        own = _validator(json.dumps(sub, sort_keys=True))
        if "enum" in sub and not any(_takes(v, item) and own.is_valid(v) for v in sub["enum"]):
            raise BadPropertySchema(f"'enum' {where} holds no value a key can take")
        if "const" in sub and not (_takes(sub["const"], item) and own.is_valid(sub["const"])):
            raise BadPropertySchema(f"'const' {where} is no value a key can take")
    _check_bounds(sub, where, MAX_LIST_ITEM if item else MAX_STRING)
    if "items" in sub:
        items_where = "in the items of " + where.removeprefix("in ")
        _check_key(sub["items"], items_where, item=True)


def _check_bounds(sub: dict[str, Any], where: str, max_string: int) -> None:
    """`BadPropertySchema` when the bounds in `sub` leave no number, length or count."""
    low, high = sub.get("minimum"), sub.get("maximum")
    ex_low, ex_high = sub.get("exclusiveMinimum"), sub.get("exclusiveMaximum")
    empty = (
        (low is not None and high is not None and low > high)
        or (ex_low is not None and high is not None and ex_low >= high)
        or (low is not None and ex_high is not None and low >= ex_high)
        or (ex_low is not None and ex_high is not None and ex_low >= ex_high)
    )
    if empty:
        raise BadPropertySchema(f"the number bounds {where} leave no number")
    if sub.get("minLength", 0) > min(sub.get("maxLength", max_string), max_string):
        raise BadPropertySchema(
            f"'minLength' {where} passes 'maxLength' or the cap of {max_string}"
        )
    if sub.get("minItems", 0) > min(sub.get("maxItems", MAX_LIST), MAX_LIST):
        raise BadPropertySchema(f"'minItems' {where} passes 'maxItems' or the cap of {MAX_LIST}")


def _of(row: dict[str, Any]) -> EntityProperty:
    return EntityProperty(
        key=row["key"],
        value=row["value"],
        source=row["source"],
        updated_at=row["updated_at"],
        updated_by=Actor.model_validate(row["updated_by"]),
    )


def _schema_in(conn: Conn, kind: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT schema FROM entity_property_schemas WHERE kind = %s", (kind,)
    ).fetchone()
    return {"type": "object", "properties": {}} if row is None else dict(row["schema"])


#: The entity kind whose schema is not its own row: an item's is its item kind's
#: `attribute_schema`, merged down the kind tree (#7067, spec 6.4).
ITEM = "item"


class SchemaOwnedByKind(ValueError):  # noqa: N818
    """An item's schema is its item kind's, set on the kind (#7067)."""


def schema_for(conn: Conn, ref: EntityRef) -> dict[str, Any]:
    """The schema that types `ref`'s properties: an item's kind's merged schema, else the
    entity kind's own."""
    if ref.kind == ITEM:
        # Imported here: the inventory imports this module's schema check.
        from scadbuddy.inventory.kinds import item_schema

        return item_schema(conn, ref.ref)
    return _schema_in(conn, ref.kind)


_LIST = (
    "SELECT p.key, p.value, p.source, p.updated_at, p.updated_by FROM entity_properties p"
    ' WHERE p.entity_id = %s ORDER BY p.key COLLATE "C"'
)


class EntityPropertyStore:
    def __init__(self, pool: Any, entities: EntityStore) -> None:
        self.pool = pool
        self.entities = entities

    def list(self, ref: EntityRef) -> builtins.list[EntityProperty]:
        """The entity's properties by key."""
        with self.pool.connection() as conn:
            entity_id = self.entities.find(conn, ref)
            if entity_id is None:
                return []
            rows = conn.execute(_LIST, (entity_id,)).fetchall()
        return [_of(r) for r in rows]

    def apply(
        self, ref: EntityRef, patch: PropertyPatch, by: Actor
    ) -> builtins.list[EntityProperty]:
        """The whole patch or nothing, in one transaction: `EntityNotFound` when the owner's
        row is not there, `BadPropertyValue` for a bad key or value, or (key ``*``) for more
        than `MAX_KEYS` keys after it and more than before it (a printer merge may leave an
        entity past the limit; it can still remove and change keys). The entity's
        properties after it, by key. A patch that only removes keys makes no entity row.
        The entity's schema (`schema_for`) is read before the transaction, so a schema
        written meanwhile types the next patch, not this one."""
        # Every value is checked before the entity's lock, so a check holds no lock.
        with self.pool.connection() as conn:
            schema = schema_for(conn, ref)
        _checked(patch, schema)
        with self.pool.connection() as conn, conn.transaction():
            if all(value is None for value in patch.set.values()):
                found = self.entities.find(conn, ref)
                if found is None:
                    if not self.entities.exists(conn, ref):
                        raise EntityNotFound(str(ref))
                    return []
                entity_id = found
            else:
                entity_id = self.entities.ensure(conn, ref)
            # One writer at a time per entity, so the key count holds.
            conn.execute("SELECT 1 FROM entities WHERE id = %s FOR UPDATE", (entity_id,))
            # A printer merge (`repoint_entity`) may have deleted the owner while this
            # waited; `ensure`'s insert then made a row for a gone ref, which the sweep
            # would delete with the write.
            if not self.entities.exists(conn, ref):
                raise EntityNotFound(str(ref))
            before = conn.execute(
                "SELECT count(*) AS n FROM entity_properties WHERE entity_id = %s",
                (entity_id,),
            ).fetchone()
            assert before is not None
            actor = Jsonb(by.model_dump(mode="json"))
            for key, value in patch.set.items():
                if value is None:
                    conn.execute(
                        "DELETE FROM entity_properties WHERE entity_id = %s AND key = %s",
                        (entity_id, key),
                    )
                    continue
                conn.execute(
                    "INSERT INTO entity_properties (entity_id, key, value, source, updated_by)"
                    " VALUES (%s, %s, %s, 'manual', %s) ON CONFLICT (entity_id, key) DO UPDATE"
                    " SET value = EXCLUDED.value, source = 'manual',"
                    " updated_at = clock_timestamp(), updated_by = EXCLUDED.updated_by",
                    (entity_id, key, Jsonb(value), actor),
                )
            rows = conn.execute(_LIST, (entity_id,)).fetchall()
            if len(rows) > max(MAX_KEYS, before["n"]):
                raise BadPropertyValue("*", f"an entity has at most {MAX_KEYS} keys")
        return [_of(r) for r in rows]

    def of_kind(self, kind: str) -> dict[str, dict[str, PropertyValue]]:
        """Every entity of `kind` that has properties: its ref to its keys and values."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT e.ref, p.key, p.value FROM entity_properties p"
                " JOIN entities e ON e.id = p.entity_id WHERE e.kind = %s"
                ' ORDER BY e.ref, p.key COLLATE "C"',
                (kind,),
            ).fetchall()
        found: dict[str, dict[str, PropertyValue]] = {}
        for r in rows:
            found.setdefault(r["ref"], {})[r["key"]] = r["value"]
        return found

    def schema(self, kind: str) -> dict[str, Any]:
        """The kind's property schema; an object schema with no properties when it has
        none."""
        with self.pool.connection() as conn:
            return _schema_in(conn, kind)

    def set_schema(self, kind: str, schema: dict[str, Any], by: Actor) -> dict[str, Any]:
        """A person's write of the kind's schema; `BadPropertySchema` leaves the old one,
        `UnknownEntityKind` refuses a kind the registry does not have, and
        `SchemaOwnedByKind` refuses `item`, whose schema is each item kind's."""
        if kind not in KINDS:
            raise UnknownEntityKind(kind)
        if kind == ITEM:
            raise SchemaOwnedByKind(kind)
        check_schema(schema)
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO entity_property_schemas (kind, schema, updated_by)"
                " VALUES (%s, %s, %s) ON CONFLICT (kind) DO UPDATE"
                " SET schema = EXCLUDED.schema, updated_at = clock_timestamp(),"
                " updated_by = EXCLUDED.updated_by RETURNING schema",
                (kind, Jsonb(schema), Jsonb(by.model_dump(mode="json"))),
            ).fetchone()
        assert row is not None
        return dict(row["schema"])

    def keys_in_use(self, kind: str) -> builtins.list[tuple[str, int]]:
        """Each key entities of `kind` have, with how many have it, by key."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT p.key, count(*) AS n FROM entity_properties p"
                " JOIN entities e ON e.id = p.entity_id WHERE e.kind = %s"
                ' GROUP BY p.key ORDER BY p.key COLLATE "C"',
                (kind,),
            ).fetchall()
        return [(r["key"], int(r["n"])) for r in rows]
