"""The edge's routes. The visitor routes under `STOREFRONT_PREFIX` come with the CRUD client."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}
