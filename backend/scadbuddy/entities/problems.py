"""The refusals of the entity routes (#6950): an RFC 9457 problem whose ``type`` ends in
its ``code``."""

from __future__ import annotations

import re

from fastapi import status

from scadbuddy.core.problems import ApiError
from scadbuddy.entities.attachments import ORDER_KINDS, PRINT_ARCHIVE
from scadbuddy.entities.links import is_web_url
from scadbuddy.entities.properties import MissingRequiredProperties
from scadbuddy.entities.refs import BadEntityRef, EntityRef
from scadbuddy.entities.registry import KINDS

PROBLEMS = "https://scadbuddy.dev/problems/entities/"
#: An attachment's role: open text (spec 6.2 lists the known values).
ROLE = re.compile(r"^[a-z_]{1,32}$")


def problem(status_code: int, code: str, detail: str) -> ApiError:
    return ApiError(status_code, detail, type_=PROBLEMS + code, code=code)


def bad_entity_ref() -> ApiError:
    return problem(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "bad_entity_ref",
        "an entity is named `kind:ref`, such as `spool:<id>` or `print_archive:<archive id>`",
    )


def parsed(text: str) -> EntityRef:
    """``kind:ref``, else 422 ``bad_entity_ref``."""
    try:
        if "\x00" in text:
            raise BadEntityRef(text)
        return EntityRef.parse(text)
    except BadEntityRef:
        raise bad_entity_ref() from None


def unknown_kind(ref: EntityRef) -> ApiError:
    return problem(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "unknown_entity_kind",
        f"no entity kind {ref.kind!r} takes files here",
    )


def readable(text: str) -> EntityRef:
    """A reference to a registered kind or a print archive, else 422."""
    ref = parsed(text)
    if ref.kind not in KINDS and ref.kind != PRINT_ARCHIVE:
        raise unknown_kind(ref)
    return ref


def writable(text: str) -> EntityRef:
    """A reference to a registered kind, else 422 (a print archive's files are
    Bambuddy's); 409 ``use_order_route`` for an order's entities."""
    ref = parsed(text)
    if ref.kind not in KINDS:
        raise unknown_kind(ref)
    if ref.kind in ORDER_KINDS:
        raise problem(
            status.HTTP_409_CONFLICT,
            "use_order_route",
            f"an order's files are changed on its own routes (`/orders/{{order_id}}/…"
            f"/attachments`), where the order's rules apply; {ref.kind!r} is one of them",
        )
    return ref


def entity_not_found(ref: EntityRef) -> ApiError:
    return problem(status.HTTP_404_NOT_FOUND, "entity_not_found", f"no {ref.kind} {ref.ref!r}")


def link_not_found(ref: EntityRef, link_id: str) -> ApiError:
    return problem(status.HTTP_404_NOT_FOUND, "link_not_found", f"{ref} has no link {link_id!r}")


def checked_url(url: str) -> str:
    if not is_web_url(url):
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "bad_link_url",
            "a link is an `http` or `https` URL with a host, up to 2000 characters",
        )
    return url


def checked_role(role: str) -> str:
    if not ROLE.match(role):
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "bad_role",
            "a role is 1 to 32 lower-case letters or underscores, such as `receipt`",
        )
    return role


def no_properties_kind(kind: str) -> ApiError:
    return problem(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "unknown_entity_kind",
        f"no entity kind {kind!r} takes properties",
    )


def bad_property_value(key: str, reason: str) -> ApiError:
    return problem(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "bad_property_value",
        f"property {key!r}: {reason}",
    )


def bad_property_schema(reason: str) -> ApiError:
    return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, "bad_property_schema", reason)


def schema_owned_by_item_kind() -> ApiError:
    return problem(
        status.HTTP_409_CONFLICT,
        "schema_owned_by_item_kind",
        "an item's properties follow its item kind's attribute_schema, so set the schema "
        "on the kind",
    )


def missing_required(exc: MissingRequiredProperties) -> ApiError:
    """422 `missing_required_properties` (spec 6.3b): every required key with no value in
    `missing`, every defined key whose value does not fit in `invalid`."""
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        str(exc),
        type_=PROBLEMS + "missing_required_properties",
        code="missing_required_properties",
        missing=list(exc.missing),
        invalid=[{"key": key, "reason": reason} for key, reason in exc.invalid],
    )
