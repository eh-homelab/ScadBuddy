"""`ENTITIES`: the entity registry and the attachments and links on any entity (#6950), over the
shared rows pool (`rows/component.py`), opened on first use as the orders' store is."""

from __future__ import annotations

from typing import Annotated

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.entities.attachments import EntityAttachmentStore
from scadbuddy.entities.links import EntityLinkStore
from scadbuddy.entities.registry import EntityStore
from scadbuddy.rows.component import ROWS


class EntitiesComponent:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self.pool = pool
        self.store = EntityStore(pool)
        self._attachments = EntityAttachmentStore(pool, self.store)
        self._links = EntityLinkStore(pool, self.store)

    def opened(self) -> ConnectionPool[Connection[DictRow]]:
        """The shared pool, opened on first use (an open pool ignores it)."""
        self.pool.open(wait=False)
        return self.pool

    @property
    def attachments(self) -> EntityAttachmentStore:
        self.opened()
        return self._attachments

    @property
    def links(self) -> EntityLinkStore:
        self.opened()
        return self._links


ENTITIES: Key[EntitiesComponent] = Key("entities")


def _build(core: Core, components: Components) -> EntitiesComponent:
    return EntitiesComponent(components.get(ROWS).pool)


COMPONENT = Component(ENTITIES, build=_build)

EntitiesDep = Annotated[EntitiesComponent, component_dep(ENTITIES)]
