"""Downloaded Google Fonts through the store (spec 2026-09-27 §6.1). A worker without
the API's volume would otherwise fall back to DejaVu silently, and that changes the
geometry (CLAUDE.md, "Fonts"). One blob per family directory under `Shared/Work/`."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Collection, Mapping
from pathlib import Path

from scadbuddy.library.fonts import FontService
from scadbuddy.library.googlefonts import licence_slug
from scadbuddy.store.archive import pack_dir, unpack_dir
from scadbuddy.store.bambuddy import SHARED_TITLE
from scadbuddy.store.content import BlobCorruptError, BlobMissingError, BlobScope, ContentStore
from scadbuddy.store.locks import KeyLocks

SHARED = BlobScope(slug=None, title=SHARED_TITLE)


_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


def font_key(directory_name: str) -> str:
    return "font-" + re.sub(r"[^A-Za-z0-9._-]", "_", directory_name)[:100]


def _family_dir_name(value: str) -> str:
    # An OpenSCAD font string: "Family:style=Bold". The directory is the family's
    # licence slug (`FontService.family_dir`).
    return licence_slug(value.split(":", 1)[0].strip())


def wanted_families(source: Path, params: Mapping[str, object]) -> set[str]:
    """The family directories a template could name: every string literal in its
    `.scad` files (a `// font` parameter's default, a `text(font=...)`) and every
    string parameter value. Loose on purpose: a string that is no family matches no
    row."""
    names = {value for value in params.values() if isinstance(value, str)}
    for path in sorted(source.rglob("*.scad")):
        if any(part.startswith(".") for part in path.relative_to(source).parts):
            continue
        names.update(_STRING.findall(path.read_text(encoding="utf-8", errors="replace")))
    return {slug for slug in map(_family_dir_name, names) if slug}


class FontMirror:
    def __init__(
        self, content: ContentStore, fonts: FontService, *, locks: KeyLocks | None = None
    ) -> None:
        self.content = content
        self.fonts = fonts
        #: Shared with the worker's `SnapshotStore` (Task 8 passes one `KeyLocks`).
        self.locks = locks or KeyLocks()

    async def _publish_dir(self, directory: Path) -> None:
        data = await asyncio.to_thread(pack_dir, directory)
        await self.content.put(
            "font",
            data,
            name=f"{font_key(directory.name)}.zip",
            scope=SHARED,
            key=font_key(directory.name),
            meta={"dir": directory.name},
        )

    async def publish(self, family: str) -> None:
        directory = self.fonts.family_dir(family)
        if directory.is_dir():
            await self._publish_dir(directory)

    async def backfill(self) -> int:
        """Mirror every family installed before the store was (the API's boot)."""
        root = self.fonts.root
        if not root.is_dir():
            return 0
        done = 0
        for directory in sorted(
            p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
        ):
            if await asyncio.to_thread(self.content.index.get, font_key(directory.name)) is None:
                await self._publish_dir(directory)
                done += 1
        return done

    async def sync(self, families: Collection[str] | None = None) -> list[str]:
        """Install the mirrored families this process lacks: only ``families`` (family
        directory names, `wanted_families`) when given, else every one. One index query
        when there is nothing to do."""
        added: list[str] = []
        # This backend's rows only: `read` downloads from this backend.
        stats = await asyncio.to_thread(
            self.content.index.stats, ("font",), backend=self.content.name
        )
        for stat in stats:
            name = str(stat.meta.get("dir", ""))
            if not name or "/" in name or name.startswith("."):
                continue
            if families is not None and name not in families:
                continue
            target = self.fonts.root / name
            if target.is_dir():
                continue
            async with self.locks.hold(stat.key):
                if target.is_dir():
                    continue
                try:
                    data = await self.content.read(stat.ref)
                except (BlobMissingError, BlobCorruptError):
                    # Gone or altered: forget it; the API's `backfill` publishes it again.
                    await self.content.forget(stat.key)
                    continue
                await asyncio.to_thread(unpack_dir, data, target)
            added.append(name)
        if added:
            await asyncio.to_thread(self.fonts.prepare)
            await asyncio.to_thread(self.fonts.refresh_cache)
        return added
