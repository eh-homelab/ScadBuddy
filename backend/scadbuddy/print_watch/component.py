"""The watcher's records (#4699) as a component (`core/components.py`): on the
projection's pool, with the activity index, so a row and its activity row share a
transaction."""

from __future__ import annotations

from typing import Annotated

from scadbuddy.activity.component import ACTIVITY
from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.print_watch.store import AutoImportStore

AUTO_IMPORTS: Key[AutoImportStore] = Key("auto_imports")


def _build(core: Core, components: Components) -> AutoImportStore:
    return AutoImportStore(core.projection.pool, components.get(ACTIVITY).index)


COMPONENT = Component(AUTO_IMPORTS, build=_build)

AutoImportsDep = Annotated[AutoImportStore, component_dep(AUTO_IMPORTS)]
