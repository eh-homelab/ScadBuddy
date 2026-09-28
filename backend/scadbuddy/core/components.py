"""Feature services, each wired in its own file rather than in a shared list (#508).

A feature ships ``COMPONENT = Component(...)`` in a ``component.py`` beside its code
(``scadbuddy/<feature>/component.py``). :func:`discover_components` finds them the way
``main._api_router`` finds routers, and ``api/deps.py`` ``build_state`` builds every one
over the core services. A component reads another with ``components.get(OTHER)`` inside
its own ``build``, so the build order is the order of those reads: there is no list to
keep in order, and a cycle is refused by name. A route reads one through
``api/components.py`` ``component_dep``.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Any, Protocol, cast

import scadbuddy

if TYPE_CHECKING:
    import asyncio

    from scadbuddy.core.config import Config
    from scadbuddy.core.events import EventBus
    from scadbuddy.core.metrics import Metrics
    from scadbuddy.core.paths import DataPaths
    from scadbuddy.core.settings import Settings
    from scadbuddy.library.catalogue import Catalogue
    from scadbuddy.library.history import ModelHistory
    from scadbuddy.library.libraries import CheckoutGate
    from scadbuddy.library.outputs import OutputStore
    from scadbuddy.render.jobs import RenderQueue

#: The module a feature package names its component in, and what it exports.
COMPONENT_MODULE = "component"
COMPONENT_ATTR = "COMPONENT"


class Core(Protocol):
    """What every component may read: the services that are not features. ``AppState``
    is one. Read only; a component never reassigns them."""

    settings: Settings
    config: Config
    paths: DataPaths
    history: ModelHistory
    catalogue: Catalogue
    outputs: OutputStore
    events: EventBus
    queue: RenderQueue
    metrics: Metrics
    checkouts: CheckoutGate
    installs: asyncio.Semaphore
    checks: asyncio.Semaphore


@dataclass(frozen=True)
class Key[T]:
    """Names a component and types what it builds. Keys are equal by name, so two
    components under one name are refused rather than one shadowing the other."""

    name: str


@dataclass(frozen=True)
class Component[T]:
    key: Key[T]
    build: Callable[[Core, Components], T]
    #: For as long as the app runs: entered once the render queue and the event bus
    #: have started, exited before they close (``main.py`` lifespan).
    run: Callable[[T], AbstractAsyncContextManager[None]] | None = None


class ComponentCycleError(RuntimeError):
    pass


class DuplicateComponentError(ValueError):
    pass


class Components:
    """The registry: builds each component on first :meth:`get`, once."""

    def __init__(self, core: Core, components: Iterable[Component[Any]]) -> None:
        self.core = core
        self._registered: dict[Key[Any], Component[Any]] = {}
        for component in components:
            if component.key in self._registered:
                raise DuplicateComponentError(
                    f"two components are registered as {component.key.name!r}"
                )
            self._registered[component.key] = component
        self._values: dict[Key[Any], Any] = {}
        #: Each built component with what it built, in the order the builds finished:
        #: a component's dependencies are always before it.
        self._built: list[tuple[Component[Any], Any]] = []
        self._building: list[Key[Any]] = []

    def get[T](self, key: Key[T]) -> T:
        if key in self._values:
            return cast(T, self._values[key])
        component = self._registered.get(key)
        if component is None:
            raise KeyError(f"no component is registered as {key.name!r}")
        if key in self._building:
            cycle = [*self._building[self._building.index(key) :], key]
            raise ComponentCycleError(
                "components depend on each other: " + " -> ".join(k.name for k in cycle)
            )
        self._building.append(key)
        try:
            value = component.build(self.core, self)
        finally:
            self._building.pop()
        self._values[key] = value
        self._built.append((component, value))
        return cast(T, value)

    def build_all(self) -> None:
        """Build every registered component, so a failing ``build`` or a cycle stops the
        start rather than the first request that reads it."""
        for key in self._registered:
            self.get(key)

    def override[T](self, key: Key[T], value: T) -> None:
        """For tests: :meth:`get` answers ``value`` from now on. It is never ``run``; a
        value already built still is, and a component built before this call keeps
        the value it read."""
        self._values[key] = value

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """Every built component's ``run``, in build order, on one exit stack: a failure
        part-way exits exactly those that started, in reverse, then propagates."""
        async with AsyncExitStack() as stack:
            for component, value in list(self._built):
                if component.run is not None:
                    await stack.enter_async_context(component.run(value))
            yield


def discover_components(package: ModuleType = scadbuddy) -> list[Component[Any]]:
    """The ``COMPONENT`` of each ``<package>.<feature>.component`` module, by feature
    name. Only packages are features: a ``component`` module directly in ``package``
    is not one."""
    found: list[Component[Any]] = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda i: i.name):
        if not info.ispkg:
            continue
        name = f"{package.__name__}.{info.name}.{COMPONENT_MODULE}"
        if importlib.util.find_spec(name) is None:
            continue
        component = getattr(importlib.import_module(name), COMPONENT_ATTR, None)
        if isinstance(component, Component):
            found.append(component)
    return found
