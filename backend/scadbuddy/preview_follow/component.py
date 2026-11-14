"""`PREVIEW_FOLLOWS`: the background slices the API process follows (#265), each by one
replica (#6158, `claims.py`), as a component (`core/components.py`). Its run cancels the
follows at shutdown and closes the claims' connection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.preview_follow.claims import PgFollowClaims
from scadbuddy.preview_follow.follows import PreviewFollows

PREVIEW_FOLLOWS: Key[PreviewFollows] = Key("preview_follows")


def _build(core: Core, components: Components) -> PreviewFollows:
    return PreviewFollows(core.events, claims=PgFollowClaims(core.settings.database_url))


@asynccontextmanager
async def _run(follows: PreviewFollows) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await follows.aclose()


COMPONENT = Component(PREVIEW_FOLLOWS, build=_build, run=_run)

PreviewFollowsDep = Annotated[PreviewFollows, component_dep(PREVIEW_FOLLOWS)]
