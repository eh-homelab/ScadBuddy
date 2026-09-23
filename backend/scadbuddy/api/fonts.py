from __future__ import annotations

from fastapi import APIRouter

from scadbuddy.library.fonts import FontFamily, list_fonts

router = APIRouter(tags=["fonts"])


@router.get("/fonts", response_model=list[FontFamily], summary="Installed font families")
def get_fonts() -> list[FontFamily]:
    return list_fonts()
