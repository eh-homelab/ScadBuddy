"""The app's routes as the relay's `RouteMatcher` (`telemetry/payload.py`): a page's
fetch span names a backend path, and only that path's route template is forwarded, as
a server span keeps only its ``http.route`` (`core/trace_scrub.py`)."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi.routing import iter_route_contexts
from starlette.routing import BaseRoute, Match

from scadbuddy.telemetry.payload import RouteMatcher


def route_matcher(routes: Sequence[BaseRoute]) -> RouteMatcher:
    """Matches a path against ``routes`` for a ``GET`` as the FastAPI instrumentation
    names a server span's ``http.route``: the routes ``include_router`` nests flattened
    (``iter_route_contexts``), the first full match, else the last route that takes the
    path for another method. A mount's template is its prefix, so the SPA's mount at
    ``/`` (prefix ``""``) names nothing."""
    contexts = tuple(iter_route_contexts(routes))

    def match(path: str) -> str | None:
        scope = {"type": "http", "path": path, "root_path": "", "method": "GET"}
        partial: str | None = None
        for context in contexts:
            found, _ = context.matches(scope)
            template = context.path
            if found is Match.NONE or not template:
                continue
            if found is Match.FULL:
                return template
            partial = template
        return partial

    return match


__all__ = ["route_matcher"]
