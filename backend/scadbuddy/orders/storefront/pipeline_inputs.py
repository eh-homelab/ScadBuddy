"""A pipeline template's own inputs on a listing (#5701, spec 2026-11-17 §4): everything in
``inputs`` but ``params`` and ``v``, which the shop sets and no customer changes.

A template declares their shape in ``model.json`` ``pipeline.inputs`` and the parameters
its pipeline sets itself in ``pipeline.sets_params``. A save checks only the shape a row
can hold (`check_shape`). Publish checks them against the commit, in the spec's order
(§4.3, first fault wins): `check_unexpected` on a template with no pipeline, else
`settled_inputs`, then `check_set_params`.

The API never runs template Python: `pipeline_facts` reads the declaration, the schema and
``INPUTS_VERSION`` as data, and an older ``inputs_v`` is migrated by the template's own
``migrate`` on the render worker (`RenderService.migrate_inputs`).
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from fastapi import status
from pydantic import JsonValue

from scadbuddy.core.input_schema import MAX_SCHEMA_BYTES, ValueProblem, value_problems
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import inputs_version_in, revision_meta
from scadbuddy.library.pipelines import HOST_INPUTS, inputs_schema_problems
from scadbuddy.orders.storefront.inputs import effective_default, same_value
from scadbuddy.orders.storefront.wire import ListingInput
from scadbuddy.render.inputs import InputsError, nul_at
from scadbuddy.render.runner import with_defaults
from scadbuddy.render.schema import CustomizerSchema, ParamValue
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS

if TYPE_CHECKING:
    from scadbuddy.render.submit import RenderService

#: The most a listing's inputs may hold, serialised (spec §4.2).
MAX_LISTING_INPUTS_BYTES: Final = 16_384
#: The most of a schema file the API reads: `inputs_schema_problems` sizes the schema
#: without the file's own layout.
MAX_SCHEMA_FILE_BYTES: Final = 4 * MAX_SCHEMA_BYTES


def _refused(slug: str, detail: str, name: str, **extensions: Any) -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail,
        type_=STOREFRONT_PROBLEMS + slug,
        name=name,
        **extensions,
    )


def _invalid(reason: str, detail: str, name: str = "inputs") -> ApiError:
    return _refused("listing_inputs_invalid", detail, name, reason=reason)


def _check_size(inputs: Mapping[str, JsonValue]) -> None:
    """422 ``listing_inputs_invalid`` for inputs no row can hold: not JSON (a NaN, a lone
    surrogate) or with a NUL (``unstorable``), or over `MAX_LISTING_INPUTS_BYTES`
    (``too_large``)."""
    try:
        encoded = json.dumps(
            inputs, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
        nul = nul_at(inputs, "inputs")
    except (ValueError, RecursionError):
        raise _invalid("unstorable", "the inputs are not JSON") from None
    if nul is not None:
        raise _invalid("unstorable", f"{nul} contains a NUL byte")
    if len(encoded) > MAX_LISTING_INPUTS_BYTES:
        raise _invalid(
            "too_large",
            f"the inputs are {len(encoded)} bytes; at most {MAX_LISTING_INPUTS_BYTES}",
        )


def check_shape(inputs: Mapping[str, JsonValue]) -> None:
    """What a save checks, and publish again: 422 ``listing_inputs_reserved`` for a key of
    the host's (`HOST_INPUTS`), and `_check_size`'s."""
    for key in HOST_INPUTS:
        if key in inputs:
            raise _refused(
                "listing_inputs_reserved",
                f"{key!r} is not one of the pipeline's own inputs",
                f"inputs.{key}",
            )
    _check_size(inputs)


def check_unexpected(listing: ListingInput) -> None:
    """422 ``listing_inputs_unexpected`` for inputs on a template with no pipeline."""
    if listing.inputs:
        raise _refused(
            "listing_inputs_unexpected",
            "this version of the template has no pipeline, so the listing has no inputs;"
            " clear them",
            "inputs",
        )


@dataclass(frozen=True)
class PipelineFacts:
    """What a commit declares of its pipeline, read without running it."""

    inputs_version: int
    #: The declared inputs schema, or None with why it cannot be used in ``fault``.
    schema: dict[str, Any] | None
    fault: str
    #: The parameters the pipeline sets itself (``pipeline.sets_params``).
    sets_params: tuple[str, ...] = ()


def pipeline_facts(directory: Path, slug: str) -> PipelineFacts | None:
    """What the template in ``directory`` (a revision's) declares of its pipeline, or None
    with no pipeline. Blocking: call it in a thread."""
    meta = revision_meta(directory, slug)
    if meta is None or (meta.pipeline is None and meta.pipeline_raw is None):
        return None
    declared = meta.pipeline
    if declared is None:
        # The catalogue keeps a declaration it cannot read (`pipeline_raw`).
        return PipelineFacts(0, None, f"the pipeline {slug!r} declares cannot be read")
    version = inputs_version_in(directory, declared)
    sets_params = tuple(declared.sets_params)
    if not (directory / declared.module).is_file():
        # Its version is then no version (0), and a render of it would fail.
        fault = f"{slug!r} has no pipeline module {declared.module}"
        return PipelineFacts(version, None, fault, sets_params)
    if declared.inputs is None:
        fault = f"{slug!r} declares no shape for its inputs (model.json pipeline.inputs)"
        return PipelineFacts(version, None, fault, sets_params)
    schema, fault = _schema(directory, declared.inputs)
    return PipelineFacts(version, schema, fault, sets_params)


def _schema(directory: Path, name: str) -> tuple[dict[str, Any] | None, str]:
    """The inputs schema in ``name`` under ``directory``, or why it cannot be used."""
    try:
        # Resolved, as the worker's `load_pipeline` does: a symlink must not lead the read
        # out of the template.
        path = (directory / name).resolve()
        if not path.is_relative_to(directory.resolve()):
            return None, f"{name} points outside the template"
        with path.open("rb") as handle:
            raw = handle.read(MAX_SCHEMA_FILE_BYTES + 1)
        if len(raw) > MAX_SCHEMA_FILE_BYTES:
            return None, f"{name} is over {MAX_SCHEMA_FILE_BYTES // 1024} KiB"
        schema = json.loads(raw)
        problems = inputs_schema_problems(schema)
    except (OSError, RuntimeError, ValueError) as error:  # a symlink loop, or too deep
        return None, f"{name} cannot be read: {error}"
    if problems:
        return None, f"{name}: {problems[0]}"
    return schema, ""


async def settled_inputs(
    render: RenderService,
    listing: ListingInput,
    slug: str,
    commit: str,
    facts: PipelineFacts,
    schema: CustomizerSchema,
) -> dict[str, JsonValue]:
    """The inputs a published ``listing`` of the pipeline template renders with, at the
    commit's ``INPUTS_VERSION``. 422 ``listing_inputs_undeclared``, `check_shape`'s,
    ``listing_inputs_invalid`` (``newer``, a failed keyword, ``too_large``) or
    ``listing_inputs_migration_failed``, in that order; a migration's 413, 503 or 504
    passes through. The row keeps what the shop saved, so a republish migrates from it
    again (spec §8): the result does not depend on how many times it ran."""
    if facts.schema is None:
        raise _refused("listing_inputs_undeclared", facts.fault, "inputs")
    check_shape(listing.inputs)
    inputs, made_at = listing.inputs, listing.inputs_v
    if made_at is not None and made_at > facts.inputs_version:
        raise _invalid(
            "newer",
            f"the inputs are of version {made_at}, newer than this version of the"
            f" template's {facts.inputs_version}",
        )
    if made_at is not None and made_at < facts.inputs_version:
        whole = {**inputs, "params": _params(listing, schema), "v": made_at}
        try:
            migrated = await render.migrate_inputs(slug, whole, version=commit)
        except InputsError as error:
            raise _refused("listing_inputs_migration_failed", str(error), "inputs") from None
        # Never its `params`: the bounds and the locks stay the shop's.
        inputs = {k: v for k, v in migrated.inputs.items() if k not in HOST_INPUTS}
        _check_size(inputs)
    for problem in value_problems(facts.schema, inputs):
        name = ".".join(("inputs", *_path(problem, facts.schema, inputs)))
        reason = "additional" if problem.keyword == "additionalProperties" else problem.keyword
        raise _invalid(reason, f"{name}: {problem.message}", name)
    return inputs


def _params(listing: ListingInput, schema: CustomizerSchema) -> dict[str, ParamValue]:
    """The parameters a render takes while the customer picks none: the shop's locks, each
    offered parameter at its starting value, and the rest at the schema's defaults."""
    known = {p.name: p for p in schema.parameters}
    params = {name: value for name, value in listing.fixed.items() if name in known}
    for name, bound in listing.params.items():
        if name in known and (start := effective_default(known[name], bound)) is not None:
            params[name] = start
    return with_defaults(schema, params)


def _path(problem: ValueProblem, schema: dict[str, Any], values: Any) -> list[str]:
    """The path of the input ``problem`` is of: a missing or an extra key's own path, not
    its object's, so the editor can show the refusal at that key. The
    object's own path when the walk meets anything but objects."""
    path = [part for part in problem.path.split(".") if part]
    if problem.keyword not in ("required", "additionalProperties"):
        return path
    for part in path:
        properties = schema.get("properties", {})
        if part not in properties or not isinstance(values, dict) or part not in values:
            return path
        schema, values = properties[part], values[part]
    if problem.keyword == "required":
        keys = [key for key in schema["required"] if key not in values]
    else:
        keys = [key for key in values if key not in schema.get("properties", {})]
    return [*path, *keys[:1]]


def check_set_params(
    listing: ListingInput, schema: CustomizerSchema, sets_params: Collection[str]
) -> None:
    """422 ``listing_param_pipeline_set`` for a parameter the pipeline sets itself
    (spec §6.1) that the listing offers, or locks at another value than its default: a
    render would not take it."""
    initial = {p.name: p.initial for p in schema.parameters}
    for name in sets_params:
        locked = listing.fixed.get(name)
        default = initial.get(name)
        moved = (
            name in initial
            and locked is not None
            and (default is None or not same_value(locked, default))
        )
        if name in listing.params or moved:
            raise _refused(
                "listing_param_pipeline_set",
                f"{name!r} is set by the template's pipeline; leave it out of the listing",
                f"params.{name}",
            )
