"""Bambu Studio's colour table (#6267): ``filaments_color_codes.json`` maps a Bambu colour
code (the digits of a spool's SKU) to its colours, name, type and filament id. The image
copies it from the slicer's AppImage (`Settings.slicer_profiles_dir`); the table is read
once per process."""

from __future__ import annotations

import json
import logging
from functools import cache
from pathlib import Path

from pydantic import BaseModel

from scadbuddy.filament.colour import normalise_rgba

logger = logging.getLogger(__name__)

TABLE = Path("BBL") / "filament" / "filaments_color_codes.json"


class ColourTableMissingError(Exception):
    """The table is not where the image should have put it."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"the Bambu colour table is missing at {path}")
        self.path = path


class ColourCode(BaseModel):
    code: str
    name: str
    #: One ``#RRGGBBAA`` per colour (a dual or tri colour spool has several). A translucent
    #: colour keeps the alpha the table gives it (``80``).
    rgba: list[str]
    fila_type: str
    fila_id: str


class ColourCodes:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._by_code: dict[str, ColourCode] | None = None

    def lookup(self, code: str) -> ColourCode | None:
        if self._by_code is None:
            self._by_code = self._load()
        return self._by_code.get(code)

    def _load(self) -> dict[str, ColourCode]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ColourTableMissingError(self._path) from None
        table: dict[str, ColourCode] = {}
        for d in raw["data"]:
            code = str(d.get("fila_color_code")) if isinstance(d, dict) else None
            try:
                table[str(d["fila_color_code"])] = ColourCode(
                    code=str(d["fila_color_code"]),
                    name=d["fila_color_name"]["en"],
                    rgba=[normalise_rgba(c) for c in d["fila_color"]],
                    fila_type=d["fila_type"],
                    fila_id=d["fila_id"],
                )
            except (KeyError, TypeError, ValueError) as error:
                logger.warning(
                    "filament.colour_code.skipped",
                    extra={"code": code, "error": type(error).__name__},
                )
        return table


@cache
def colour_codes(profiles_dir: Path) -> ColourCodes:
    """The table under a profiles directory, one per process."""
    return ColourCodes(profiles_dir / TABLE)
