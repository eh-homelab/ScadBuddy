"""Where a template's media list lives (#274): the `template_media` table.

The files stay in ``models/<slug>/media/`` (images committed, videos gitignored);
only the order, captions and posters are rows. A row is written after its file and
removed before it, so a file with no row is possible (it is ignored) and a row
with no file is reported ``missing``.

A built-in's rows (#722) are the media people added to it, under its
``builtin:<slug>`` id, the files in ``builtin-media/<slug>/``; what it ships is its
bundled model.json's. Its cover is chosen in `template_media_cover`, since the
shipped items come first.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.library.media import MediaItem


class MediaStore(Protocol):
    def items(self, template_id: str) -> list[MediaItem]: ...

    def items_for(self, template_ids: Sequence[str]) -> dict[str, list[MediaItem]]: ...

    def replace(self, template_id: str, items: list[MediaItem]) -> None: ...

    def delete(self, template_id: str) -> None: ...

    def cover(self, template_id: str) -> str | None: ...

    def covers_for(self, template_ids: Sequence[str]) -> dict[str, str]: ...

    def set_cover(self, template_id: str, item_id: str | None) -> None: ...


class PostgresMediaStore:
    """`template_media` over the render queue's connection pool."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    def items(self, template_id: str) -> list[MediaItem]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, file, kind, caption, poster FROM template_media"
                " WHERE template_id = %s ORDER BY position",
                (template_id,),
            ).fetchall()
        return [MediaItem.model_validate(row) for row in rows]

    def items_for(self, template_ids: Sequence[str]) -> dict[str, list[MediaItem]]:
        """Every listed template's items in ONE query, for a page of the catalogue;
        a template with none is absent from the answer."""
        if not template_ids:
            return {}
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT template_id, id, file, kind, caption, poster FROM template_media"
                " WHERE template_id = ANY(%s) ORDER BY template_id, position",
                (list(template_ids),),
            ).fetchall()
        listed: dict[str, list[MediaItem]] = {}
        for row in rows:
            template_id = row.pop("template_id")
            listed.setdefault(template_id, []).append(MediaItem.model_validate(row))
        return listed

    def replace(self, template_id: str, items: list[MediaItem]) -> None:
        """Make ``items`` the template's whole list, in one transaction."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM template_media WHERE template_id = %s", (template_id,))
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO template_media"
                    " (template_id, id, position, file, kind, caption, poster)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            template_id,
                            item.id,
                            index,
                            item.file,
                            item.kind,
                            item.caption,
                            item.poster,
                        )
                        for index, item in enumerate(items)
                    ],
                )

    def delete(self, template_id: str) -> None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM template_media WHERE template_id = %s", (template_id,))
            conn.execute("DELETE FROM template_media_cover WHERE template_id = %s", (template_id,))

    def cover(self, template_id: str) -> str | None:
        """The item a built-in's overlay made its cover, or None."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT item_id FROM template_media_cover WHERE template_id = %s",
                (template_id,),
            ).fetchone()
        return None if row is None else str(row["item_id"])

    def covers_for(self, template_ids: Sequence[str]) -> dict[str, str]:
        """Every listed template's chosen cover in ONE query; one with none is absent."""
        if not template_ids:
            return {}
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT template_id, item_id FROM template_media_cover WHERE template_id = ANY(%s)",
                (list(template_ids),),
            ).fetchall()
        return {str(row["template_id"]): str(row["item_id"]) for row in rows}

    def set_cover(self, template_id: str, item_id: str | None) -> None:
        """Choose ``item_id`` as the cover, or with None go back to the list's order."""
        with self._pool.connection() as conn:
            if item_id is None:
                conn.execute(
                    "DELETE FROM template_media_cover WHERE template_id = %s", (template_id,)
                )
            else:
                conn.execute(
                    "INSERT INTO template_media_cover (template_id, item_id) VALUES (%s, %s)"
                    " ON CONFLICT (template_id) DO UPDATE SET item_id = EXCLUDED.item_id",
                    (template_id, item_id),
                )
