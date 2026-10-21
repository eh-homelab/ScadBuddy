"""A line item's reference, checked (#4249, spec 2026-10-18 §3.3, plan Rulings 1, 6, 7).

The reference is ``{slug, commit, inputs, libraries}``: the slug must be a template of
the catalogue (one the history remembers but the catalogue no longer has was deleted,
:class:`TemplateDeletedError`), the commit a commit of *that* model's history, resolved
to its full id first (``GitRepo.resolve``, ``rev-parse --verify``, which answers against
the whole models repository, so ``ModelHistory.touched`` then checks it changed the
slug's own directory: a model's directory is its slug and never moves), and the inputs
must normalise against the schema of the template at that commit, as a render's do.
An item made from an output takes the output's commit, libraries and colours; one made
by hand takes the libraries a render at that commit would resolve and no colours, which
the quote fills (phase 2). Nothing here imports from ``api/``.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from scadbuddy.core.config import Config
from scadbuddy.core.gitrepo import COMMIT_ID_PATTERN, RevisionNotFoundError
from scadbuddy.core.paths import MODEL_META_NAME, DataPaths, model_path
from scadbuddy.library.catalogue import Catalogue, declares_pipeline, meta_from_raw
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import CheckoutFetcher, ModelLibrary
from scadbuddy.library.outputs import OutputMeta, OutputNotFoundError, OutputStore
from scadbuddy.orders.models import ColourSlot, Reference
from scadbuddy.render.inputs import InputsError, inputs_key, normalize_inputs
from scadbuddy.render.job_models import render_key
from scadbuddy.render.jobs import resolve_source
from scadbuddy.render.runner import (
    ParameterValueError,
    UnknownParameterError,
    build_defines,
    cached_schema,
)

_COMMIT = re.compile(COMMIT_ID_PATTERN)


class ReferenceError(ValueError):
    """The reference is not one the catalogue can make: ``field`` names which part
    (``slug``, ``commit``, ``inputs`` or ``output_id``) and ``problem`` says why. The
    routes answer 422 on the field."""

    def __init__(self, field: str, problem: str) -> None:
        super().__init__(f"{field}: {problem}")
        self.field = field
        self.problem = problem


class TemplateDeletedError(Exception):
    """The slug is a template the history remembers and the catalogue no longer lists
    (a tombstone): refused with 409 ``template_deleted`` (Ruling 6), since a restore
    would bring it back under the same slug."""

    def __init__(self, slug: str) -> None:
        super().__init__(f"template {slug!r} was deleted")
        self.slug = slug


@dataclass(frozen=True)
class ResolvedReference:
    reference: Reference
    #: Ruling 7: the output's render colours, or none for the quote to fill.
    colours: list[ColourSlot]


@dataclass(frozen=True)
class OutputFacts:
    """What an output's ``meta.json`` and ``inputs.json`` say a reference is."""

    slug: str
    commit: str | None
    inputs: dict[str, Any]
    libraries: list[ModelLibrary]
    colours: list[ColourSlot]


def colours_of(colors: Sequence[str]) -> list[ColourSlot]:
    """The render's colour slots, in order (Ruling 7); ``filament_id`` waits for the
    price book. A default-render preview record (`library/previews.py`) keeps no
    colours, so only an output can give them in phase 1."""
    return [ColourSlot(slot=index, colour=colour) for index, colour in enumerate(colors)]


def reference_from_output(meta: OutputMeta, inputs: Mapping[str, Any]) -> OutputFacts:
    """``meta.json``'s slug, ``model_version``, libraries and colours, with ``inputs.json``."""
    return OutputFacts(
        slug=meta.slug,
        commit=meta.model_version,
        inputs=dict(inputs),
        libraries=list(meta.libraries),
        colours=colours_of(meta.colors),
    )


def render_key_of(slug: str, inputs: Mapping[str, Any], commit: str, *, pipeline: bool) -> str:
    """The job key a render of the reference would take (§3.3): the whole inputs for a
    pipeline template, else the params, as ``render/submit.py`` chooses."""
    if pipeline:
        return inputs_key(slug, inputs, commit)
    params = inputs.get("params", {})
    return render_key(slug, params if isinstance(params, dict) else {}, commit)


def _template_name(directory: Any, slug: str) -> str:
    try:
        raw = json.loads((directory / MODEL_META_NAME).read_text(encoding="utf-8"))
        return meta_from_raw(raw if isinstance(raw, dict) else {}, slug).name
    except (OSError, ValueError):
        return slug


async def resolve_reference(
    catalogue: Catalogue,
    history: ModelHistory,
    outputs: OutputStore,
    *,
    paths: DataPaths,
    config: Config,
    fetcher: CheckoutFetcher | None,
    slug: str | None,
    commit: str | None,
    inputs: Mapping[str, Any] | None,
    output_id: str | None,
) -> ResolvedReference:
    """The checked reference, and its colours.

    With ``output_id``, the output's own slug, commit and inputs stand in for any the
    caller leaves ``None``, and one the caller does give must agree with the output's:
    the item keeps the output's colours and pointer, which describe that render only.
    """
    facts: OutputFacts | None = None
    given_commit = commit if output_id is not None else None
    given_inputs = inputs if output_id is not None else None
    if output_id is not None:
        try:
            meta = await asyncio.to_thread(outputs.get, output_id)
            facts = reference_from_output(meta, await asyncio.to_thread(outputs.inputs, output_id))
        except OutputNotFoundError:
            raise ReferenceError("output_id", f"no output {output_id!r}") from None
        if slug is not None and slug != facts.slug:
            raise ReferenceError(
                "output_id", f"output {output_id!r} is a render of {facts.slug!r}, not {slug!r}"
            )
        slug = facts.slug
        if commit is None:
            commit = facts.commit
            if commit is None:
                raise ReferenceError(
                    "commit",
                    f"output {output_id!r} names no revision of {slug!r}; the model was rendered"
                    " without a history to pin",
                )
        if inputs is None:
            inputs = facts.inputs
    if slug is None:
        raise ReferenceError("slug", "a line item needs a template slug or an output")
    if commit is None:
        raise ReferenceError("commit", "a line item pins one commit of its template")
    if inputs is None:
        inputs = {}

    if not await asyncio.to_thread(catalogue.exists, slug):
        remembered = (
            await asyncio.to_thread(history.last_commit, model_path(slug))
            if history.available
            else None
        )
        if remembered is not None:
            raise TemplateDeletedError(slug)
        raise ReferenceError("slug", f"no model named {slug!r}")
    if not _COMMIT.match(commit):
        raise ReferenceError("commit", f"{commit!r} is not a commit id (7 to 40 hex characters)")
    if not history.available:
        raise ReferenceError("commit", "the models repository has no history to pin a commit in")
    try:
        full = await asyncio.to_thread(history.resolve, commit)
    except RevisionNotFoundError:
        raise ReferenceError("commit", f"no commit {commit!r} in the models repository") from None
    if not await asyncio.to_thread(history.touched, full, model_path(slug)):
        raise ReferenceError("commit", f"commit {full} is not a revision of {slug!r}")
    if facts is not None and facts.commit is not None and given_commit is not None:
        try:
            rendered = await asyncio.to_thread(history.resolve, facts.commit)
        except RevisionNotFoundError:
            rendered = facts.commit
        if rendered != full:
            raise ReferenceError(
                "commit", f"output {output_id!r} is a render of commit {rendered}, not {full}"
            )

    source = await resolve_source(slug, full, paths=paths, history=history, fetcher=fetcher)
    try:
        normalized = normalize_inputs(inputs, None)
    except InputsError as error:
        raise ReferenceError("inputs", str(error)) from None
    if facts is not None and given_inputs is not None:
        try:
            rendered_inputs = normalize_inputs(facts.inputs, None).data
        except InputsError:
            rendered_inputs = dict(facts.inputs)
        if rendered_inputs != normalized.data:
            raise ReferenceError(
                "inputs",
                f"output {output_id!r} was rendered with other inputs than these; leave"
                " inputs out to take the output's, or give them whole, `v` included",
            )
    pipeline = await asyncio.to_thread(declares_pipeline, source.scad.parent, slug)
    if not pipeline:
        schema = await cached_schema(
            source.scad, source.schema_cache, config=source.configure(config)
        )
        try:
            build_defines(schema, normalized.params)
        except (UnknownParameterError, ParameterValueError, ValueError) as error:
            raise ReferenceError("inputs", str(error)) from None
    libraries = facts.libraries if facts is not None else list(source.libraries)
    reference = Reference(
        slug=slug,
        commit=full,
        inputs=normalized.data,
        libraries=libraries,
        template_name=await asyncio.to_thread(_template_name, source.scad.parent, slug),
        render_key=render_key_of(slug, normalized.data, full, pipeline=pipeline),
    )
    return ResolvedReference(reference, facts.colours if facts is not None else [])
