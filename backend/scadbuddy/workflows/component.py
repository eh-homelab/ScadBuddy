"""The progress route's follows of a print still moving (#268, #1053), as a component
(`core/components.py`, review #1091 3): started on the client and queue print runs use."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.workflows.follow import Follows

FOLLOWS: Key[Follows] = Key("print_follows")


def _build(core: Core, components: Components) -> Follows:
    return Follows(core.print_runs.client, core.print_runs.task_queue)


@asynccontextmanager
async def _run(follows: Follows) -> AsyncIterator[None]:
    try:
        yield
    finally:
        # Cancel what is still starting: a Temporal that does not answer never holds
        # the shutdown up.
        await follows.aclose()


COMPONENT = Component(FOLLOWS, build=_build, run=_run)

FollowsDep = Annotated[Follows, component_dep(FOLLOWS)]
