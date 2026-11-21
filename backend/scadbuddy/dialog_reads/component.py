"""The print dialog's kept Bambuddy reads (#4196), as a component
(`core/components.py`): one per app, so each test's app keeps its own."""

from __future__ import annotations

from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.dialog_reads.reads import DialogReads

DIALOG_READS: Key[DialogReads] = Key("dialog_reads")


def _build(core: Core, components: Components) -> DialogReads:
    return DialogReads()


COMPONENT = Component(DIALOG_READS, build=_build)

DialogReadsDep = Annotated[DialogReads, component_dep(DIALOG_READS)]
