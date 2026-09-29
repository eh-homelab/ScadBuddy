"""Checking parameter values against a template's customizer schema, shared by the
render route, the preset routes and the metadata route that edits a template's presets."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping

from fastapi import status

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.assets import AssetStore, file_assets
from scadbuddy.library.fonts import FontService, cut_by_dash, font_families, normalise_family
from scadbuddy.library.history import GitError, ModelHistory, RevisionNotFoundError
from scadbuddy.library.libraries import CheckoutFetcher
from scadbuddy.render.jobs import ModelSource, resolve_source
from scadbuddy.render.runner import (
    ParameterValueError,
    UnknownParameterError,
    build_defines,
    cached_schema,
)
from scadbuddy.render.schema import CustomizerSchema, ParamValue


async def schema_of(
    slug: str,
    requested: str | None,
    *,
    paths: DataPaths,
    history: ModelHistory,
    config: Config,
    version: str | None = None,
    fetcher: CheckoutFetcher | None = None,
) -> tuple[ModelSource, CustomizerSchema]:
    """The source a render of ``slug`` at ``requested`` reads, and its schema.

    The schema parameters are validated against has to be the schema of the revision
    being rendered, not the one the model is currently at. `resolve_source` also hands
    back which revision that is, so the job can be stamped without asking git again.
    ``version`` is what the client asked for, for the 404's message.
    """
    try:
        source = await resolve_source(
            slug, requested, paths=paths, history=history, fetcher=fetcher
        )
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
    """422 unless every one of ``params`` is a parameter of ``schema``, of its type,
    inside its customizer range and, for a select, one of its options (#432)."""
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
    except ParameterValueError as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error), parameters=[error.parameter]
        ) from None
    except ValueError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None


class InstalledFamilies:
    """The families fontconfig resolves, asked for once however many values are
    judged against them: a template's presets are checked in one request, and each
    ask shells out to fc-list (#740 review)."""

    def __init__(self, fonts: FontService) -> None:
        self._fonts = fonts
        self._asked = False
        self._known: set[str] | None = None

    async def get(self) -> set[str] | None:
        if not self._asked:
            # fc-list shells out; off the loop.
            self._known = await asyncio.to_thread(self._fonts.resolvable)
            self._asked = True
        return self._known


async def require_installed_fonts(
    schema: CustomizerSchema,
    params: Mapping[str, ParamValue],
    fonts: FontService | InstalledFamilies,
) -> None:
    """422 unless every family a `// font` value names resolves in this image (#253).

    OpenSCAD never refuses one: a family fontconfig does not have renders in the
    default font (DejaVu Sans here) with other geometry and no warning
    (library/fonts.py, module docstring). So a caller that asks for one is told here.
    Only values the caller chose are judged -- the template's own default and options
    are its business, as for a path-like value (render/runner.py
    `_refuse_path_like`) -- and "" is the default font. Without fontconfig to ask
    (a development machine), nothing is refused.
    """
    by_name = {parameter.name: parameter for parameter in schema.parameters}
    wanted: dict[str, tuple[str, list[str]]] = {}
    for name, value in params.items():
        parameter = by_name.get(name)
        if parameter is None or parameter.type != "font" or not isinstance(value, str):
            continue
        if value == parameter.initial or any(o.value == value for o in parameter.options):
            continue
        families = font_families(value)
        if families:
            wanted[name] = (value, families)
    if not wanted:
        return
    families_of = fonts if isinstance(fonts, InstalledFamilies) else InstalledFamilies(fonts)
    known = await families_of.get()
    if known is None:
        return
    problems: list[str] = []
    missing_names: list[str] = []
    missing_families: list[str] = []
    for name, (value, families) in wanted.items():
        missing = [family for family in families if normalise_family(family) not in known]
        if not missing:
            continue
        missing_names.append(name)
        missing_families.extend(family for family in missing if family not in missing_families)
        # A bare dash ends the scan, so only the last family listed is the one it cut.
        hint = (
            " (a bare '-' starts a point size in fontconfig's syntax; write it '\\-')"
            if cut_by_dash(value) and families[-1] in missing
            else ""
        )
        problems.append(
            f"parameter {name!r} names font family "
            f"{', '.join(repr(family) for family in missing)}{hint}, which is not installed"
        )
    if problems:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "; ".join(problems)
            + ". OpenSCAD would silently draw it in the default font instead; install the "
            "family (POST /fonts/install) or name one GET /fonts lists",
            parameters=missing_names,
            families=missing_families,
        )


def require_valid_preset_params(schema: CustomizerSchema, params: Mapping[str, ParamValue]) -> None:
    """422 unless ``params`` would render as they are *and* every dropdown value is one
    of its options or a value the template retired.

    Stricter than a render, which takes any value of the right type: a preset is kept
    and replayed, so it holds only what the dropdown itself could pick, or picked
    before the option was renamed (`// retired`, #432; `render/runner.py` accepts the
    same values).
    """
    require_valid_params(schema, params)
    by_name = {parameter.name: parameter for parameter in schema.parameters}
    for name, value in params.items():
        parameter = by_name[name]
        options = [option.value for option in parameter.options]
        if options and value not in (*options, *parameter.retired):
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{value!r} is not one of the options of {name!r}",
                parameters=[name],
            )


async def require_valid_presets(
    slug: str,
    presets: Iterable[Mapping[str, ParamValue]],
    *,
    paths: DataPaths,
    history: ModelHistory,
    config: Config,
    assets: AssetStore,
    fetcher: CheckoutFetcher | None = None,
    fonts: FontService | None = None,
) -> None:
    """422 unless every one of ``presets`` would render the template as it is now:
    the values a render takes, a dropdown value among its options, a `file` value
    that names an upload or a sample (#204), and, given ``fonts``, a `font` value whose
    family is installed (#253). Checking a file value marks the upload
    used, so the sweep cannot take it from under the preset being saved (#296).

    The one check for a preset's values, whether it is saved on its own or defined
    in the template's `model.json` (#326), so the two can never drift apart.
    """
    source, schema = await schema_of(
        slug, None, paths=paths, history=history, config=config, fetcher=fetcher
    )
    has_files = any(parameter.type == "file" for parameter in schema.parameters)
    installed = InstalledFamilies(fonts) if fonts is not None else None
    for params in presets:
        require_valid_preset_params(schema, params)
        if installed is not None:
            await require_installed_fonts(schema, params, installed)
        if has_files:
            try:
                await asyncio.to_thread(file_assets, schema, params, assets, source.scad.parent)
            except ValueError as error:
                raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
