"""``GET /api/v1/internal/crud/<caller>/areas`` (#5298, plan "Rolling deploy"): the CRUD
areas this API pod serves to that caller. A new worker waits until it lists every area
the worker needs before it polls Temporal. Each area's task appends its name to
`CRUD_AREAS` once the area is complete."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from scadbuddy.crud.guard import CrudCaller, crud_router

#: Each caller's complete areas.
CRUD_AREAS: dict[CrudCaller, list[str]] = {
    "projects": ["flows", "operations", "payload-keys"],
    "render": ["settings"],
    "print": [],
}


class Areas(BaseModel):
    areas: list[str]


def _areas_router(caller: CrudCaller) -> APIRouter:
    caller_router = crud_router(caller)

    @caller_router.get("/areas", name=f"crud_{caller}_areas")
    def areas() -> Areas:
        return Areas(areas=CRUD_AREAS[caller])

    return caller_router


#: The three callers' routes, each built by its own `crud_router` (prefix, guard, headers),
#: in one flat router as `main.py` `_api_router` mounts every module's.
router = APIRouter()
for _caller in CRUD_AREAS:
    router.routes.extend(_areas_router(_caller).routes)
