"""Downloaded Google Fonts through the store (spec 2026-09-27 §6.1). A worker without
the API's volume would otherwise fall back to DejaVu silently, and that changes the
geometry (CLAUDE.md, "Fonts"). One blob per family directory under `Shared/Work/`."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from scadbuddy.library.fonts import FontService
from scadbuddy.store.archive import pack_dir, unpack_dir
from scadbuddy.store.bambuddy import SHARED_TITLE
from scadbuddy.store.content import BlobCorruptError, BlobMissingError, BlobScope, ContentStore

SHARED = BlobScope(slug=None, title=SHARED_TITLE)


def font_key(directory_name: str) -> str:
    return "font-" + re.sub(r"[^A-Za-z0-9._-]", "_", directory_name)[:100]


class FontMirror:
    def __init__(self, content: ContentStore, fonts: FontService) -> None:
        self.content = content
        self.fonts = fonts

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

    async def sync(self) -> list[str]:
        """Install every mirrored family this process lacks; one index query when none."""
        added: list[str] = []
        # This backend's rows only: `read` downloads from this backend.
        stats = await asyncio.to_thread(
            self.content.index.stats, ("font",), backend=self.content.name
        )
        for stat in stats:
            name = str(stat.meta.get("dir", ""))
            if not name or "/" in name or name.startswith("."):
                continue
            target = self.fonts.root / name
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
