"""A print's photos and videos (#309), ``print_attachments`` on the projection's
Postgres pool, as a component (`core/components.py`)."""

from __future__ import annotations

from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.bambuddy.attachments import AttachmentStore
from scadbuddy.core.components import Component, Components, Core, Key

PRINT_ATTACHMENTS: Key[AttachmentStore] = Key("print_attachments")


def _build(core: Core, components: Components) -> AttachmentStore:
    return AttachmentStore(core.projection.pool)


COMPONENT = Component(PRINT_ATTACHMENTS, build=_build)

PrintAttachmentsDep = Annotated[AttachmentStore, component_dep(PRINT_ATTACHMENTS)]
