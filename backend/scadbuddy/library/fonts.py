from __future__ import annotations

import logging
import shutil
import subprocess

from pydantic import BaseModel

FC_LIST = "fc-list"
FC_LIST_TIMEOUT = 10.0

logger = logging.getLogger(__name__)


class FontFamily(BaseModel):
    family: str
    styles: list[str]


def parse_fc_list(output: str) -> list[FontFamily]:
    """``fc-list : family style`` prints ``Family[,alias]:style=Style[,alias]``.

    Only the first family and first style of each line are kept: those are the names
    OpenSCAD's ``"Family:style=Style"`` font string is written with.
    """
    families: dict[str, set[str]] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        head, separator, tail = line.partition(":style=")
        family = head.split(",", 1)[0].strip()
        if not family:
            continue
        style = tail.split(",", 1)[0].strip() if separator else ""
        styles = families.setdefault(family, set())
        if style:
            styles.add(style)
    return [
        FontFamily(family=family, styles=sorted(families[family])) for family in sorted(families)
    ]


def list_fonts() -> list[FontFamily]:
    """Font families installed in the container, or an empty list without fontconfig."""
    if shutil.which(FC_LIST) is None:
        logger.warning("fc-list is not on PATH; reporting no fonts")
        return []
    try:
        completed = subprocess.run(  # fixed argv, no shell
            [FC_LIST, ":", "family", "style"],
            capture_output=True,
            text=True,
            timeout=FC_LIST_TIMEOUT,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        logger.exception("fc-list failed; reporting no fonts")
        return []
    return parse_fc_list(completed.stdout)
