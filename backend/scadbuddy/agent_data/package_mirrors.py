"""The bytes of a plugin package's bytes pin, kept as a blob (#6935, spec 2026-11-21
§6.6, area ``package-mirrors``, `api/crud_agent_packages.py`).

An ``archive`` or ``npm`` package (or a marketplace entry of either) is pinned to the
sha256 of the file the agent fetched once at install. The agent sends that file here
before it stores the pin; a load unpacks this copy, never the upstream URL, so an approved
pin still loads after upstream deletes or changes the file.

Each mirror is the blob ``plugin-mirror-<sha256>`` (one file, `BLOB_FILE`), held in
``blob_refs`` by ``(HOLDER, <package name>/<sha256>)`` (`holder_id`): the package the agent
sent the file for. A pin of a package is stored only while that package's hold of its
sha256 stands (`held`), so one package's upload never stands in for another's. A hold
stays while that package's pin or pending re-pin names the sha256 (`release_unnamed`); a
hold no row names goes once it is older than `UNNAMED_GRACE`, so an install between its
upload and its row is never cut short. The blob sweep removes the bytes once no hold of
any package is left.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, BinaryIO

import psycopg

from scadbuddy.store import BlobStore
from scadbuddy.store.cache import StaleBlobError
from scadbuddy.store.content_models import BlobScope

#: ``blob_refs.holder_kind`` of a mirror's hold.
HOLDER = "plugin_package_mirror"
#: The one file in a mirror's blob directory.
BLOB_FILE = "file"
#: The largest mirror: the largest file the agent downloads, its ``ARCHIVE_MAX_BYTES``
#: (``agent/src/plugins/packages/download.ts``: the unpacked cap of 20 MiB plus 1 MiB;
#: spec 2026-11-21 §6.6).
MAX_MIRROR_BYTES = 21 * 1024 * 1024
#: How long a hold no package names is kept: far longer than an install's run (300 s).
UNNAMED_GRACE = "1 day"


def blob_key(sha256: str) -> str:
    return f"plugin-mirror-{sha256}"


def _place(blobs: BlobStore, key: str, source: BinaryIO) -> None:
    """``source``'s bytes as the blob's `BLOB_FILE`, moved into place under a name of its
    own, so two uploads of the same bytes never leave half a file for the other."""
    directory = blobs.dir_for(key)
    staging = directory / f".{BLOB_FILE}.{uuid.uuid4().hex}"
    try:
        source.seek(0)
        with staging.open("xb") as out:
            shutil.copyfileobj(source, out, 1 << 20)
        os.replace(staging, directory / BLOB_FILE)
    finally:
        with contextlib.suppress(FileNotFoundError):
            staging.unlink()


async def keep(blobs: BlobStore, source: BinaryIO, sha256: str) -> str:
    """``source`` (whose sha256 the caller checked is ``sha256``) kept and published as
    the blob ``plugin-mirror-<sha256>``; its key. Bytes the store holds already are its
    blob as it is (`BlobStore.claim` touches it, so the sweep keeps it until the hold is
    committed), as ``orders/attachments.py`` ``_keep`` does."""
    key = blob_key(sha256)
    if await blobs.claim(key):
        return key
    await asyncio.to_thread(_place, blobs, key, source)
    try:
        await blobs.publish_fresh(key, scope=BlobScope(), expected=None)
    except StaleBlobError:
        if not await blobs.claim(key):
            raise
    return key


async def path_of(blobs: BlobStore, sha256: str) -> Path | None:
    """The kept file on this process's volume, fetched first where the store is remote;
    None when the store holds no mirror of ``sha256``."""
    key = blob_key(sha256)
    if not await blobs.fetch(key):
        return None
    path = blobs.dir_for(key) / BLOB_FILE
    return path if path.is_file() else None


def holder_id(name: str, sha256: str) -> str:
    """The hold's ``holder_id``: a package name never holds a ``/``."""
    return f"{name}/{sha256}"


def hold(conn: psycopg.Connection[Any], name: str, sha256: str) -> None:
    """The package ``name``'s hold of the mirror, its grace counted from now."""
    conn.execute(
        "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES (%s, %s, %s)"
        " ON CONFLICT (key, holder_kind, holder_id) DO UPDATE SET created_at = now()",
        (blob_key(sha256), HOLDER, holder_id(name, sha256)),
    )


def held(conn: psycopg.Connection[Any], name: str, sha256: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM blob_refs WHERE key = %s AND holder_kind = %s AND holder_id = %s",
            (blob_key(sha256), HOLDER, holder_id(name, sha256)),
        ).fetchone()
        is not None
    )


def release_unnamed(conn: psycopg.Connection[Any]) -> None:
    """Drops each package's mirror hold that its pin or pending re-pin does not name and
    that is older than `UNNAMED_GRACE`. Runs in every package write's transaction."""
    conn.execute(
        "DELETE FROM blob_refs WHERE holder_kind = %s"
        f" AND created_at < now() - interval '{UNNAMED_GRACE}'"
        " AND holder_id NOT IN ("
        "   SELECT name || '/' || bytes_sha256 FROM ai_plugin_packages"
        "   WHERE bytes_sha256 IS NOT NULL"
        "   UNION SELECT name || '/' || pending_bytes_sha256 FROM ai_plugin_packages"
        "   WHERE pending_bytes_sha256 IS NOT NULL)",
        (HOLDER,),
    )
