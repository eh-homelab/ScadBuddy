from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from scadbuddy.core.config import Config
from scadbuddy.render.runner import OpenSCADError, export_param_json

logger = logging.getLogger(__name__)

NUL = b"\x00"


class NotOpenSCADError(ValueError):
    """The upload is not something OpenSCAD will parse."""

    def __init__(self, message: str, log_tail: list[str] | None = None) -> None:
        super().__init__(message)
        self.log_tail = log_tail or []


def decode_source(raw: bytes) -> str:
    """Reject anything that is not UTF-8 text before it reaches OpenSCAD."""
    if NUL in raw:
        raise NotOpenSCADError("the upload is binary, not an OpenSCAD source file")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise NotOpenSCADError("the upload is not valid UTF-8 text") from error


async def verify_parses(source: str, *, config: Config) -> None:
    """Sniff by content: hand the source to OpenSCAD and see whether it parses.

    A missing binary is not a rejection — the decode check above still stands.
    """
    if shutil.which(config.openscad) is None:
        logger.warning("openscad is not on PATH; accepting the upload on the text check alone")
        return
    with tempfile.TemporaryDirectory(prefix="scadbuddy-sniff-") as tmp:
        scad_path = Path(tmp) / "model.scad"
        scad_path.write_text(source, encoding="utf-8")
        try:
            await export_param_json(scad_path, config=config)
        except OpenSCADError as error:
            raise NotOpenSCADError("OpenSCAD could not parse the upload", error.log_tail) from error
