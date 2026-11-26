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

from scadbuddy.store.content_models import SWEPT_KINDS, BlobKind, BlobRef, content_index_key
from scadbuddy.store.index import BlobIndex, Pool
from scadbuddy.store.s3 import object_key

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "blob-upload-vectors.json").read_text()
)
CASES = VECTORS["cases"]


def test_a_tag_dump_is_a_kind_the_sweep_decides() -> None:
    assert "tag_dump" in get_args(BlobKind)
    assert "tag_dump" in SWEPT_KINDS


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
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT key, sha256, kind, backend, backend_id, size, slug, meta,"
            " verified_at IS NOT NULL AS verified FROM store_blobs WHERE key = %s",
            (case["index_key"],),
        ).fetchone()
    assert row == case["row"]
