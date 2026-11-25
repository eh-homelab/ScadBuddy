"""A template media list in memory, for a catalogue built without Postgres."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from scadbuddy.library.media import MediaItem

if TYPE_CHECKING:
    from scadbuddy.library.catalogue import Catalogue


async def thumbnail_of(catalogue: Catalogue, slug: str) -> tuple[bytes, str] | None:
    """The catalogue thumbnail and its content type, as `GET …/thumbnail` reads it."""
    ref = await catalogue.thumbnail_ref(slug)
    found = None if ref is None else await ref.read()
    return None if found is None else (found[0], found[1])


class MemoryMediaStore:
    """`library.media_store.MediaStore` over dicts, as `PostgresMediaStore` keeps it."""

    def __init__(self) -> None:
        self._items: dict[str, list[MediaItem]] = {}
        self._covers: dict[str, str] = {}

    def items(self, template_id: str) -> list[MediaItem]:
        return list(self._items.get(template_id, []))

    def items_for(self, template_ids: Sequence[str]) -> dict[str, list[MediaItem]]:
        return {t: list(self._items[t]) for t in template_ids if self._items.get(t)}

    def replace(self, template_id: str, items: list[MediaItem]) -> None:
        self._items[template_id] = list(items)

    def delete(self, template_id: str) -> None:
        self._items.pop(template_id, None)
        self._covers.pop(template_id, None)

    def cover(self, template_id: str) -> str | None:
        return self._covers.get(template_id)

    def covers_for(self, template_ids: Sequence[str]) -> dict[str, str]:
        return {t: self._covers[t] for t in template_ids if t in self._covers}

    def set_cover(self, template_id: str, item_id: str | None) -> None:
        if item_id is None:
            self._covers.pop(template_id, None)
        else:
            self._covers[template_id] = item_id
