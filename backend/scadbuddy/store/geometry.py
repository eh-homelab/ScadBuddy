from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class GeometryRow:
    piece_key: str
    #: What the piece's colour classes were rendered in (`colour_classes`).
    colours: list[str]
    #: The piece's `RenderMainResult`, as JSON.
    main: dict[str, Any]


class GeometryIndex:
    """`piece_geometry`: the finished piece whose meshes a geometry key's pieces can be
    recoloured from (`render/geometry_cache.py`). A hint, never a reference: it keeps no
    blob alive, and a row naming a swept piece is a miss its reader forgets.

    The pool must be autocommit, as the render queue's is (`BlobRefs`)."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    def get(self, geometry_key: str) -> GeometryRow | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT piece_key, colours, main FROM piece_geometry WHERE geometry_key = %s",
                (geometry_key,),
            ).fetchone()
        if row is None:
            return None
        return GeometryRow(row["piece_key"], list(row["colours"]), dict(row["main"]))

    def put(
        self, geometry_key: str, piece_key: str, colours: list[str], main: dict[str, Any]
    ) -> None:
        """The newest finished piece wins: it is the one least likely to be swept."""
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO piece_geometry (geometry_key, piece_key, colours, main)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (geometry_key) DO UPDATE SET"
                " piece_key = EXCLUDED.piece_key, colours = EXCLUDED.colours,"
                " main = EXCLUDED.main, updated_at = now()",
                (geometry_key, piece_key, Jsonb(colours), Jsonb(main)),
            )

    def forget(self, geometry_key: str, piece_key: str) -> None:
        """Drop the row, unless another piece has replaced it since it was read."""
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM piece_geometry WHERE geometry_key = %s AND piece_key = %s",
                (geometry_key, piece_key),
            )
