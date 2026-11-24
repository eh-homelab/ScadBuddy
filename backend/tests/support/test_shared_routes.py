from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient


def named() -> str:
    return "real"


def test_shared_routes_read_the_serving_apps_overrides() -> None:
    module = APIRouter()

    @module.get("/name")
    def name(value: Annotated[str, Depends(named)]) -> str:
        return value

    def app() -> FastAPI:
        tree = APIRouter()
        tree.include_router(module)
        made = FastAPI()
        made.include_router(tree, prefix="/shared")
        return made

    first, second, third = app(), app(), app()
    assert first.router.routes[-1] is second.router.routes[-1]
    first.dependency_overrides[named] = lambda: "first"
    second.dependency_overrides[named] = lambda: "second"
    answers = [TestClient(a).get("/shared/name").json() for a in (first, second, third, first)]
    assert answers == ["first", "second", "real", "first"]
