from __future__ import annotations

import os
import sys
import textwrap
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Annotated, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import scadbuddy
from scadbuddy.api import deps
from scadbuddy.api.components import component_dep, getter_for
from scadbuddy.api.deps import DATABASE_REQUIRED_PROBLEM, STATE_ATTR
from scadbuddy.core.components import (
    Component,
    ComponentCycleError,
    Components,
    Core,
    DuplicateComponentError,
    Key,
    discover_components,
)
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.core.settings import Settings
from scadbuddy.library import operations as library_operations
from scadbuddy.library.libraries import CheckoutGate
from scadbuddy.main import create_app
from scadbuddy.operations.component import OPERATIONS
from scadbuddy.operations.kinds import DuplicateKindError, OperationKind, build_kinds
from tests.conftest import UNUSED_TEMPORAL_ADDRESS

#: A stand-in: these tests only check that `build` is handed the core it was given.
CORE = cast(Core, object())

NUMBER: Key[int] = Key("number")
WORD: Key[str] = Key("word")
MAYBE: Key[str | None] = Key("maybe")


def _run(log: list[str], name: str, *, fail: bool = False) -> AbstractAsyncContextManager[None]:
    @asynccontextmanager
    async def run() -> AsyncIterator[None]:
        if fail:
            raise RuntimeError(f"{name} failed to start")
        log.append(f"enter {name}")
        try:
            yield
        finally:
            log.append(f"exit {name}")

    return run()


def test_get_builds_on_first_use_and_memoises() -> None:
    calls: list[Core] = []

    def build(core: Core, components: Components) -> int:
        calls.append(core)
        return 42

    registry = Components(CORE, [Component(NUMBER, build=build)])
    assert calls == []
    first: int = registry.get(NUMBER)
    assert first == 42
    assert registry.get(NUMBER) == 42
    assert calls == [CORE]


def test_a_component_reads_another_so_the_build_order_is_demand_order() -> None:
    order: list[str] = []

    def build_word(core: Core, components: Components) -> str:
        word = f"n={components.get(NUMBER)}"
        order.append("word")
        return word

    def build_number(core: Core, components: Components) -> int:
        order.append("number")
        return 7

    # Registered dependent-first: the order of the list does not decide the build.
    registry = Components(
        CORE, [Component(WORD, build=build_word), Component(NUMBER, build=build_number)]
    )
    registry.build_all()
    # `word` asked for `number` inside its build, so `number` finished first.
    assert order == ["number", "word"]
    assert registry.get(WORD) == "n=7"


def test_a_cycle_is_refused_naming_its_keys() -> None:
    a: Key[int] = Key("a")
    b: Key[int] = Key("b")
    registry = Components(
        CORE,
        [
            Component(a, build=lambda core, components: components.get(b)),
            Component(b, build=lambda core, components: components.get(a)),
        ],
    )
    with pytest.raises(ComponentCycleError, match=r"a -> b -> a"):
        registry.get(a)


def test_an_unregistered_key_is_named() -> None:
    with pytest.raises(KeyError, match="number"):
        Components(CORE, []).get(NUMBER)


def test_two_components_under_one_key_are_refused() -> None:
    with pytest.raises(DuplicateComponentError, match="number"):
        Components(
            CORE,
            [
                Component(NUMBER, build=lambda core, components: 1),
                # Another Key object, the same name: the same key.
                Component(Key[int]("number"), build=lambda core, components: 2),
            ],
        )


def test_override_replaces_the_value_without_building() -> None:
    def build(core: Core, components: Components) -> int:
        raise AssertionError("an overridden component is not built")

    registry = Components(CORE, [Component(NUMBER, build=build)])
    registry.override(NUMBER, 5)
    assert registry.get(NUMBER) == 5
    registry.override(NUMBER, 6)
    assert registry.get(NUMBER) == 6


def test_override_is_typed_by_the_key() -> None:
    registry = Components(CORE, [])
    # mypy must refuse this: an unused ignore fails the strict run (warn_unused_ignores),
    # so a `Key` whose parameter went covariant again would be caught here.
    registry.override(NUMBER, "not an int")  # type: ignore[misc]


async def test_running_enters_in_build_order_and_exits_in_reverse() -> None:
    log: list[str] = []
    registry = Components(
        CORE,
        [
            Component(
                WORD,
                build=lambda core, components: f"{components.get(NUMBER)}",
                run=lambda value: _run(log, "word"),
            ),
            Component(
                NUMBER, build=lambda core, components: 1, run=lambda value: _run(log, "number")
            ),
            # No `run`: built, never entered.
            Component(MAYBE, build=lambda core, components: None),
        ],
    )
    registry.build_all()
    async with registry.running():
        assert log == ["enter number", "enter word"]
    assert log == ["enter number", "enter word", "exit word", "exit number"]


async def test_a_failing_run_unwinds_exactly_what_started() -> None:
    log: list[str] = []
    third: Key[int] = Key("third")
    registry = Components(
        CORE,
        [
            Component(NUMBER, build=lambda core, components: 1, run=lambda v: _run(log, "number")),
            Component(
                WORD,
                build=lambda core, components: f"{components.get(NUMBER)}",
                run=lambda v: _run(log, "word", fail=True),
            ),
            Component(
                third,
                build=lambda core, components: len(components.get(WORD)),
                run=lambda v: _run(log, "third"),
            ),
        ],
    )
    registry.build_all()
    with pytest.raises(RuntimeError, match="word failed to start"):
        async with registry.running():
            raise AssertionError("never reached")
    assert log == ["enter number", "exit number"]


async def test_running_hands_run_the_built_value() -> None:
    seen: list[int] = []

    @asynccontextmanager
    async def run(value: int) -> AsyncIterator[None]:
        seen.append(value)
        yield

    registry = Components(CORE, [Component(NUMBER, build=lambda core, components: 3, run=run)])
    registry.build_all()
    async with registry.running():
        assert seen == [3]


def _app(registry: Components) -> FastAPI:
    app = FastAPI()
    install_problem_handlers(app)
    # What `get_state` reads; only `components` is used here.
    setattr(app.state, STATE_ATTR, SimpleNamespace(components=registry))

    @app.get("/word")
    def word(value: Annotated[str, component_dep(WORD)]) -> str:
        return value

    @app.get("/maybe")
    def maybe(
        value: Annotated[str, component_dep(MAYBE, required="maybe needs a database")],
    ) -> str:
        return value

    return app


def test_component_dep_reads_the_registry_and_honours_dependency_overrides() -> None:
    registry = Components(CORE, [Component(WORD, build=lambda core, components: "built")])
    app = _app(registry)
    # One getter per key, so an override set on it reaches every route using the key.
    assert getter_for(WORD) is getter_for(WORD)
    with TestClient(app) as client:
        assert client.get("/word").json() == "built"
        app.dependency_overrides[getter_for(WORD)] = lambda: "overridden"
        assert client.get("/word").json() == "overridden"


def test_a_required_component_that_is_none_answers_503() -> None:
    registry = Components(CORE, [Component(MAYBE, build=lambda core, components: None)])
    app = _app(registry)
    with TestClient(app) as client:
        response = client.get("/maybe")
        assert response.status_code == 503
        problem = response.json()
        assert problem["type"] == DATABASE_REQUIRED_PROBLEM
        assert problem["detail"] == "maybe needs a database"
        # The required dependency reads the key's one getter, so its override counts too.
        app.dependency_overrides[getter_for(MAYBE)] = lambda: "present"
        assert client.get("/maybe").json() == "present"


@pytest.fixture
def fake_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """``fakepkg`` with three feature packages: two ship a component, one does not."""
    root = tmp_path / "fakepkg"
    for feature in ("beta", "alpha", "gamma"):
        (root / feature).mkdir(parents=True)
        (root / feature / "__init__.py").write_text("", encoding="utf-8")
    (root / "__init__.py").write_text("", encoding="utf-8")
    # A plain module beside the packages is not a feature.
    (root / "component.py").write_text("raise AssertionError('not imported')\n", encoding="utf-8")
    for feature in ("alpha", "beta"):
        (root / feature / "component.py").write_text(
            textwrap.dedent(
                f"""
                from scadbuddy.core.components import Component, Key

                COMPONENT = Component(Key[str]("{feature}"), build=lambda core, c: "{feature}")
                """
            ),
            encoding="utf-8",
        )
    monkeypatch.syspath_prepend(str(tmp_path))
    import fakepkg  # type: ignore[import-not-found]

    yield fakepkg
    for name in [name for name in sys.modules if name.split(".")[0] == "fakepkg"]:
        del sys.modules[name]


def test_discovery_finds_each_feature_packages_component_in_name_order(
    fake_package: ModuleType,
) -> None:
    found = discover_components(fake_package)
    assert [component.key.name for component in found] == ["alpha", "beta"]


def test_every_discovered_key_is_unique() -> None:
    names = [component.key.name for component in discover_components(scadbuddy)]
    assert len(names) == len(set(names))


def test_a_replaced_state_keeps_its_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.startswith("SCADBUDDY_"):
            monkeypatch.delenv(name)
    probe: Key[str] = Key("probe")
    component = Component(probe, build=lambda core, components: "probe")
    monkeypatch.setattr(deps, "discover_components", lambda: [component])
    seed = tmp_path / "seed"
    seed.mkdir()
    state = deps.build_state(
        Settings(
            openscad=str(tmp_path / "no-openscad"),
            data_dir=tmp_path / "data",
            seed_models_dir=seed,
            frontend_dir=Path("/nonexistent"),
            # build_state connects to nothing; the lifespan opens the pools.
            database_url="postgresql://unused@127.0.0.1:1/unused",
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
            preview_renders=False,
        )
    )
    copy = replace(state, checkouts=CheckoutGate())
    assert copy.components is state.components
    assert copy.components.get(probe) == "probe"


def test_the_lifespan_enters_a_discovered_components_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_conninfo: str
) -> None:
    for name in list(os.environ):
        if name.startswith("SCADBUDDY_"):
            monkeypatch.delenv(name)
    log: list[str] = []
    probe: Key[str] = Key("probe")
    component = Component(
        probe, build=lambda core, components: "probe", run=lambda value: _run(log, value)
    )
    monkeypatch.setattr(deps, "discover_components", lambda: [component])
    seed = tmp_path / "seed"
    seed.mkdir()
    app = create_app(
        Settings(
            openscad=str(tmp_path / "no-openscad"),
            data_dir=tmp_path / "data",
            seed_models_dir=seed,
            frontend_dir=Path("/nonexistent"),
            database_url=pg_conninfo,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
            preview_renders=False,
        )
    )
    state = getattr(app.state, STATE_ATTR)
    # Built with the state, before the app starts; run only once it does.
    assert state.components.get(probe) == "probe"
    assert log == []
    with TestClient(app):
        assert log == ["enter probe"]
    assert log == ["enter probe", "exit probe"]


def test_build_state_alone_registers_every_features_operation_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #1063 4: the kinds come from the features' registrations as the operations
    component builds, so a state that never went through ``create_app`` has them."""
    for name in list(os.environ):
        if name.startswith("SCADBUDDY_"):
            monkeypatch.delenv(name)
    seed = tmp_path / "seed"
    seed.mkdir()
    state = deps.build_state(
        Settings(
            openscad=str(tmp_path / "no-openscad"),
            data_dir=tmp_path / "data",
            seed_models_dir=seed,
            frontend_dir=Path("/nonexistent"),
            database_url="postgresql://unused@127.0.0.1:1/unused",
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
            preview_renders=False,
        )
    )
    kinds = state.components.get(OPERATIONS).kinds
    assert {"send", "reprint", "attach_project", "register_sidebar"} <= set(kinds)
    with pytest.raises(TypeError):
        kinds["late"] = kinds["send"]  # type: ignore[index]


def test_two_features_claiming_one_kind_name_are_refused() -> None:
    def kind(name: str) -> OperationKind:
        async def step(*args: object) -> dict[str, object]:
            return {}

        return OperationKind(name, step, step)

    with pytest.raises(DuplicateKindError, match="'send'"):
        build_kinds(CORE, Components(CORE, []), [lambda c, cs: [kind("send")]] * 2)


def test_the_model_kinds_refuse_a_core_that_is_not_the_app_state() -> None:
    """Review #1126 1.2: their runs are the routes' bodies, which read services the
    ``Core`` does not name; a core without them is refused when the kinds are built,
    not with an ``AttributeError`` inside an operation."""
    with pytest.raises(TypeError, match="AppState"):
        library_operations.OPERATION_KINDS(CORE, Components(CORE, []))


@pytest.mark.parametrize("attempts", [0, -1])
def test_a_kind_whose_effect_could_retry_forever_is_refused(attempts: int) -> None:
    """Review #1063 fourth review 2: Temporal reads ``maximum_attempts=0`` as unlimited."""

    async def step(*args: object) -> dict[str, object]:
        return {}

    with pytest.raises(ValueError, match="run_attempts"):
        OperationKind("send", step, step, run_attempts=attempts)
