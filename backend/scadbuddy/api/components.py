"""Route dependencies on a component (`core/components.py`).

A feature declares its own alias beside its component, as ``bambuddy/component.py``
does::

    ArchiveCacheDep = Annotated[ArchiveCache, component_dep(ARCHIVE_CACHE)]

Not in ``core/components.py``: the getter reads ``deps.get_state``, and ``deps.py``
imports the registry. No ``from __future__ import annotations`` here either: FastAPI
reads the getters' annotations at runtime, and one of them names a local.
"""

from collections.abc import Callable
from typing import Any

from fastapi import Depends

from scadbuddy.api.deps import StateDep
from scadbuddy.core.components import Key

_getters: dict[Key[Any], Callable[..., Any]] = {}


def getter_for[T](key: Key[T]) -> Callable[..., T]:
    """The one dependency that reads ``key``: the same function every call, so
    ``app.dependency_overrides[getter_for(KEY)]`` reaches every route that reads it."""
    if key not in _getters:

        def get(state: StateDep) -> Any:
            return state.components.get(key)

        get.__name__ = f"get_{key.name}"
        _getters[key] = get
    return _getters[key]


def component_dep(key: Key[Any]) -> Any:
    """``Depends`` on ``key``'s getter.

    Not type-checked against ``key``: ``Annotated[int, component_dep(WORD)]`` passes
    mypy, since ``Annotated`` metadata is opaque to it. Name the key's own type in the
    alias beside the key."""
    return Depends(getter_for(key))
