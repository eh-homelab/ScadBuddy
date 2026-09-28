"""Route dependencies on a component (`core/components.py`).

A feature declares its own alias beside its component, as ``deps.py`` does for the
core services::

    PrintWatcherDep = Annotated[PrintWatcher, component_dep(PRINT_WATCHER)]

Not in ``core/components.py``: the getter reads ``deps.get_state``, and ``deps.py``
imports the registry. No ``from __future__ import annotations`` here either: FastAPI
reads the getters' annotations at runtime, and one of them names a local.
"""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, status

from scadbuddy.api.deps import DATABASE_REQUIRED_PROBLEM, StateDep
from scadbuddy.core.components import Key
from scadbuddy.core.problems import ApiError

_getters: dict[Key[Any], Callable[..., Any]] = {}
_required: dict[tuple[Key[Any], str], Callable[..., Any]] = {}


def getter_for[T](key: Key[T]) -> Callable[..., T]:
    """The one dependency that reads ``key``: the same function every call, so
    ``app.dependency_overrides[getter_for(KEY)]`` reaches every route that reads it."""
    if key not in _getters:

        def get(state: StateDep) -> Any:
            return state.components.get(key)

        get.__name__ = f"get_{key.name}"
        _getters[key] = get
    return _getters[key]


def component_dep(key: Key[Any], required: str | None = None) -> Any:
    """``Depends`` on ``key``'s getter. With ``required``, a ``None`` value answers a
    503 ``deps.DATABASE_REQUIRED_PROBLEM`` with ``required`` as its detail; it reads
    the same getter, so an override of that reaches this too."""
    if required is None:
        return Depends(getter_for(key))
    if (key, required) not in _required:
        plain = getter_for(key)

        def require(value: Annotated[Any, Depends(plain)]) -> Any:
            if value is None:
                raise ApiError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    required,
                    type_=DATABASE_REQUIRED_PROBLEM,
                )
            return value

        require.__name__ = f"require_{key.name}"
        _required[(key, required)] = require
    return Depends(_required[(key, required)])
