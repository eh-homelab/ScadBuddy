"""Key-value properties on any entity (#7026, spec 6.3a): rows of `entity_properties`, one
per entity and key. A value is a string, a finite number, a boolean or a list of strings.
A kind's schema (`entity_property_schemas`) documents and types the keys it knows and never
refuses a key: a known key's value is checked against its subschema, any other is stored as
given. A person's or the agent's write makes a key `manual`."""

from __future__ import annotations

import builtins
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
from scadbuddy.entities.registry import EntityStore
from scadbuddy.orders.models import Actor

Conn = Connection[DictRow]

KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
MAX_KEYS = 200
MAX_STRING = 2000
MAX_LIST = 50
MAX_LIST_ITEM = 500
#: Top-level keywords that would refuse a key; a schema with any of them is refused.
RESTRICTING = ("required", "propertyNames")
_NO_REF = '"$ref" and "$dynamicRef" are not taken: each known key is checked on its own'
_NO_REGEX = " is not taken: a regex could hang a write"
#: Keywords refused at any depth. A regex in a stored schema can backtrack without end on
#: a crafted value while a write runs.
REFUSED_DEEP = {
    "$ref": _NO_REF,
    "$dynamicRef": _NO_REF,
    "pattern": '"pattern"' + _NO_REGEX,
    "patternProperties": '"patternProperties"' + _NO_REGEX,
}

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
    """A schema that is not a valid object schema, or one that would refuse a key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
        return None if len(value) <= MAX_STRING else f"at most {MAX_STRING} characters"
    if isinstance(value, list):
        if len(value) > MAX_LIST:
            return f"a list holds at most {MAX_LIST} strings"
        for item in value:
            if not isinstance(item, str):
                return "a list holds strings only"
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
            error = best_match(Draft202012Validator(known[key]).iter_errors(value))
            if error is not None:
                raise BadPropertyValue(key, error.message)


def check_schema(schema: dict[str, Any]) -> None:
    """`BadPropertySchema` unless `schema` is a valid Draft 2020-12 object schema that
    refuses no key."""
    if not isinstance(schema, dict):
        raise BadPropertySchema("a schema is a JSON object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        raise BadPropertySchema(f"not a valid Draft 2020-12 schema: {e.message}") from None
    if schema.get("type") != "object":
        raise BadPropertySchema('a schema\'s "type" is "object"')
    if "additionalProperties" in schema and schema["additionalProperties"] is not True:
        raise BadPropertySchema('"additionalProperties" may only be true')
    for keyword in RESTRICTING:
        if keyword in schema:
            raise BadPropertySchema(f'"{keyword}" would refuse keys')
    for key, sub in (schema.get("properties") or {}).items():
        if sub is False:
            raise BadPropertySchema(f'"{key}": a false subschema would refuse the key')
    reason = _refused_in(schema)
    if reason is not None:
        raise BadPropertySchema(reason)


#: Keywords whose value is data, never a schema.
_DATA_KEYWORDS = frozenset({"const", "default", "enum", "examples"})
#: Keywords whose value maps NAMES to subschemas: a name is never a keyword. Draft-07's
#: `dependencies` maps a name to a subschema or to a list of names (plain strings).
_NAMED_SUBSCHEMAS = frozenset(
    {"properties", "$defs", "definitions", "dependentSchemas", "dependencies"}
)


def _refused_in(node: Any) -> str | None:
    """The reason a keyword anywhere in the schema `node` is refused, or None. Data values
    are skipped and the names in a map of subschemas are not keywords."""
    if isinstance(node, list):
        return next((r for v in node if (r := _refused_in(v)) is not None), None)
    if not isinstance(node, dict):
        return None
    if node.get("format") == "regex":
        return '"format": "regex"' + _NO_REGEX
    for k, v in node.items():
        if k in _DATA_KEYWORDS:
            continue
        if k in REFUSED_DEEP:
            return REFUSED_DEEP[k]
        if k in _NAMED_SUBSCHEMAS and isinstance(v, dict):
            subs: list[Any] = list(v.values())
        else:
            subs = [v]
        for sub in subs:
            reason = _refused_in(sub)
            if reason is not None:
                return reason
    return None


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
        properties after it, by key."""
        # Every value is checked before the entity's lock, so a check holds no lock.
        with self.pool.connection() as conn:
            schema = _schema_in(conn, ref.kind)
        _checked(patch, schema)
        with self.pool.connection() as conn, conn.transaction():
            entity_id = self.entities.ensure(conn, ref)
            # One writer at a time per entity, so the key count holds.
            conn.execute("SELECT 1 FROM entities WHERE id = %s FOR UPDATE", (entity_id,))
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
        """A person's write of the kind's schema; `BadPropertySchema` leaves the old one."""
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
