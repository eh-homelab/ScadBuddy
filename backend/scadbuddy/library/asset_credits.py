"""Crediting a fetched file (#959, #844's licence capture).

A fetch keeps the licence its caller named (`AssetMeta.licence`). When it names none,
the catalogues on the default allowlist have a licence known here, by the URL: each
catalogue's own terms, as Iconify's ``/collections`` answered them on 2026-11-03
(``GET https://api.iconify.design/collections?prefixes=mdi,tabler,game-icons,openmoji``,
``license`` and ``author`` of each) and as each project's repository states them. A
URL anywhere else has none, and is credited as "licence not stated".

Each fetch then adds a line to a "Credits" section of the model's README, once per
source URL: the file's name, where it came from, whom to credit and the licence.
Everything in the line except the URL comes from the caller or the file's name, so it
is flattened to one line and its Markdown escaped: a credit cannot add a heading or a
link of its own.
"""

from __future__ import annotations

import re
from typing import Final

import httpx

from scadbuddy.library.asset_fetch import host_allowed
from scadbuddy.library.assets import AssetLicence

_MDI = AssetLicence(
    spdx="Apache-2.0",
    name="Apache 2.0",
    url="https://github.com/Templarian/MaterialDesign/blob/master/LICENSE",
    author="Pictogrammers",
)
_TABLER = AssetLicence(
    spdx="MIT",
    name="MIT",
    url="https://github.com/tabler/tabler-icons/blob/master/LICENSE",
    author="Paweł Kuna",
)
_GAME_ICONS = AssetLicence(
    spdx="CC-BY-3.0",
    name="CC BY 3.0",
    url="https://github.com/game-icons/icons/blob/master/license.txt",
    author="GameIcons",
)
_OPENMOJI = AssetLicence(
    spdx="CC-BY-SA-4.0",
    name="CC BY-SA 4.0",
    url="https://creativecommons.org/licenses/by-sa/4.0/",
    author="OpenMoji",
)

#: Iconify prefix -> its catalogue's licence (`library/icon_search.py` `COLLECTIONS`).
ICONIFY_LICENCES: Final = {
    "mdi": _MDI,
    "tabler": _TABLER,
    "game-icons": _GAME_ICONS,
    "openmoji": _OPENMOJI,
}
#: A GitHub repository (owner/name, lower-cased) -> its licence, for raw files off
#: github.com and raw.githubusercontent.com.
GITHUB_LICENCES: Final = {
    "templarian/materialdesign": _MDI,
    "tabler/tabler-icons": _TABLER,
    "game-icons/icons": _GAME_ICONS,
    "hfg-gmuend/openmoji": _OPENMOJI,
}

CREDITS_HEADING: Final = "## Credits"
_HEADING = re.compile(r"^#{1,6}\s")
_CREDITS = re.compile(r"^##\s+Credits\s*$", re.IGNORECASE)
_MARKDOWN = re.compile(r"([\\`*_\[\]<>()#!|~])")
_SAFE_LINK = re.compile(r"^https://[^\s()<>\[\]]+$")


def known_licence(url: str) -> AssetLicence | None:
    """The licence of a file at ``url`` on one of the catalogues known here."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return None
    host = parsed.host.lower()
    segments = [part for part in parsed.path.split("/") if part]
    if host == "api.iconify.design" and len(segments) == 2:
        return ICONIFY_LICENCES.get(segments[0])
    if host_allowed(host, ("openmoji.org",)):
        return _OPENMOJI
    if host_allowed(host, ("game-icons.net",)):
        # .../1x1/<author>/<name>.svg: each icon's author is in its path.
        if "1x1" in segments[:-1]:
            author = segments[segments.index("1x1") + 1]
            if author != segments[-1]:
                return _GAME_ICONS.model_copy(update={"author": author})
        return _GAME_ICONS
    if host in ("github.com", "raw.githubusercontent.com") and len(segments) >= 2:
        return GITHUB_LICENCES.get(f"{segments[0]}/{segments[1]}".lower())
    return None


def _flat(text: str) -> str:
    """``text`` on one line, its Markdown escaped."""
    return _MARKDOWN.sub(r"\\\1", " ".join(text.split()))


def credit_line(name: str, source_url: str, licence: AssetLicence | None) -> str:
    """The README line that credits ``name``, fetched from ``source_url``."""
    shown = " ".join(name.replace("`", "'").split())
    parts = [f"- `{shown}` from <{source_url}>"]
    if licence is not None and licence.author:
        parts.append(f"by {_flat(licence.author)}")
    terms = None if licence is None else (licence.spdx or licence.name)
    if terms is None:
        parts.append("licence not stated")
    elif licence is not None and licence.url and _SAFE_LINK.match(licence.url):
        parts.append(f"under [{_flat(terms)}]({licence.url})")
    else:
        parts.append(f"under {_flat(terms)}")
    return ", ".join(parts)


def with_credit(readme: str, source_url: str, line: str) -> str | None:
    """``readme`` with ``line`` at the end of its Credits section, started at the end
    if it has none; ``None`` when ``source_url`` is credited there already."""
    if f"<{source_url}>" in readme:
        return None
    lines = readme.splitlines()
    start = next((i for i, text in enumerate(lines) if _CREDITS.match(text)), None)
    if start is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", CREDITS_HEADING, "", line] if lines else [CREDITS_HEADING, "", line]
        return "\n".join(lines) + "\n"
    end = next((i for i in range(start + 1, len(lines)) if _HEADING.match(lines[i])), len(lines))
    at = end
    while at > start + 1 and not lines[at - 1].strip():
        at -= 1
    if at == start + 1:
        lines[at:at] = ["", line]
    else:
        lines.insert(at, line)
    return "\n".join(lines) + "\n"
