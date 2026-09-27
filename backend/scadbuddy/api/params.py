"""Checking parameter values against a template's customizer schema, shared by the
render route, the preset routes and the metadata route that edits a template's presets."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import status

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.history import GitError, ModelHistory, RevisionNotFoundError
from scadbuddy.render.jobs import ModelSource, resolve_source
from scadbuddy.render.runner import UnknownParameterError, build_defines, cached_schema
from scadbuddy.render.schema import CustomizerSchema, ParamValue


async def schema_of(
    slug: str,
    requested: str | None,
    *,
    paths: DataPaths,
    history: ModelHistory,
    config: Config,
    version: str | None = None,
) -> tuple[ModelSource, CustomizerSchema]:
    """The source a render of ``slug`` at ``requested`` reads, and its schema.

    The schema parameters are validated against has to be the schema of the revision
    being rendered, not the one the model is currently at. `resolve_source` also hands
    back which revision that is, so the job can be stamped without asking git again.
    ``version`` is what the client asked for, for the 404's message.
    """
    try:
        source = await resolve_source(slug, requested, paths=paths, history=history)
        schema = await cached_schema(
            source.scad, source.schema_cache, config=source.configure(config)
        )
    except RevisionNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} does not exist at {version}") from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
    except FileNotFoundError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "openscad is not available to build the schema"
        ) from None
    return source, schema


def require_valid_params(schema: CustomizerSchema, params: Mapping[str, ParamValue]) -> None:
    """422 unless every one of ``params`` is a parameter of ``schema``, of its type."""
    unknown = sorted(set(params) - {p.name for p in schema.parameters})
    if unknown:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"unknown parameters: {', '.join(unknown)}",
            parameters=unknown,
        )
    try:
        build_defines(schema, params)
    except UnknownParameterError as error:  # pragma: no cover - covered by the check above
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except ValueError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None


def require_valid_preset_params(schema: CustomizerSchema, params: Mapping[str, ParamValue]) -> None:
    """422 unless ``params`` would render as they are *and* every dropdown value is one
    of its options.

    Stricter than a render, which takes any value of the right type: a preset is kept
    and replayed, so it holds only what the dropdown itself could pick.
    """
    require_valid_params(schema, params)
    by_name = {parameter.name: parameter for parameter in schema.parameters}
    for name, value in params.items():
        options = [option.value for option in by_name[name].options]
        if options and value not in options:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{value!r} is not one of the options of {name!r}",
                parameters=[name],
            )
