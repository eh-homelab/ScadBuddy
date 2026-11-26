"""The object key and the content index row of a blob that core's shared CRUD area
``blobs`` commits (#7958, `tools/go/core/internal/blobstore`): `fixtures/blob-upload-vectors.json`
is what Python's store writes for each, and core's tests check that core writes the same, so
the ContentStore reads what core indexed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from psycopg.types.json import Jsonb

from scadbuddy.library.settings_store import S3Settings
from scadbuddy.store.content_models import SWEPT_KINDS, BlobKind, BlobRef, content_index_key
from scadbuddy.store.index import BlobIndex, Pool
from scadbuddy.store.s3 import S3ContentBackend, object_key, staged_upload, staging_key

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "blob-upload-vectors.json").read_text()
)
CASES = VECTORS["cases"]


def test_a_tag_dump_is_a_kind_the_sweep_decides() -> None:
    assert "tag_dump" in get_args(BlobKind)
    assert "tag_dump" in SWEPT_KINDS


def test_an_uploads_staging_key_is_one_only_the_staging_sweep_takes() -> None:
    """Core's upload PUTs to a staging key: the storefront's form, so `sweep_staged`
    removes one left behind, and not a content key, so `sweep_unrecorded` never does."""
    prefix, key = VECTORS["prefix"], VECTORS["staging_key"]
    assert staging_key(prefix, VECTORS["staging_nonce"]) == key
    assert staged_upload(prefix, key) == key
    backend = S3ContentBackend(
        S3Settings(
            endpoint="http://s3.test",
            bucket="b",
            prefix=prefix,
            access_key_id="k",
            secret_access_key="s",
        )
    )
    with pytest.raises(ValueError, match="not an S3 object key"):
        backend._key(key)


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_keys_are_pythons(case: dict[str, Any]) -> None:
    assert object_key(VECTORS["prefix"], case["kind"], case["sha256"]) == case["object_key"]
    assert content_index_key(case["kind"], case["sha256"]) == case["index_key"]


@pytest.mark.requires_postgres
@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_row_is_the_one_python_writes(pool: Pool, case: dict[str, Any]) -> None:
    before = case["before"]
    if before is not None:
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO store_blobs"
                " (key, sha256, kind, backend, backend_id, size, slug, meta, verified_at)"
                " VALUES (%s, %s, %s, 's3', %s, %s, %s, %s,"
                " CASE WHEN %s THEN now() END)",
                (
                    case["index_key"],
                    case["sha256"],
                    case["kind"],
                    before["backend_id"] or case["object_key"],
                    case["size"],
                    before["slug"],
                    Jsonb(before["meta"]),
                    before["verified"],
                ),
            )
    ref = BlobRef(
        sha256=case["sha256"],
        kind=case["kind"],
        backend="s3",
        backend_id=case["object_key"],
        size=case["size"],
    )
    BlobIndex(pool).put(case["index_key"], ref, slug=None, meta={})
    assert _row(pool, case["index_key"]) == case["row"]
    # Core's commit read the object, so it marks the row as the render route does.
    with pool.connection() as conn:
        BlobIndex.mark_verified_in(conn, case["index_key"], ref)
    assert _row(pool, case["index_key"]) == {**case["row"], **case["committed"]}


def _row(pool: Pool, key: str) -> dict[str, Any] | None:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT key, sha256, kind, backend, backend_id, size, slug, meta,"
            " verified_at IS NOT NULL AS verified FROM store_blobs WHERE key = %s",
            (key,),
        ).fetchone()
