"""`GET /outputs/copy-status` (#7255): the last run of the copy of the saved outputs' files
to rows, at an API start or in a housekeeping sweep, and the last run of the copy of
their bytes to the blob store (#7256). The tests' apps skip the start's copies
(`app_output_copy_off`), so each test writes the runs it reads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.api.outputs import DAMAGED_SAMPLE
from scadbuddy.library.output_rows import ByteWrite, OutputBlobs, OutputRow, OutputRows
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.render.glb import BoundingBox


def _rows(client: TestClient) -> OutputRows:
    store: OutputStore = getattr(client.app.state, STATE_ATTR).outputs  # type: ignore[attr-defined]
    assert store.rows is not None
    return store.rows


def test_copy_status_reports_last_run(client: TestClient) -> None:
    rows = _rows(client)
    first = rows.start_run()
    damaged = [{"output_id": "a" * 32, "slug": "box", "file": "bom.json", "error": "OSError"}]
    rows.finish_run(
        first, copied=3, refreshed=2, dropped=1, not_copied=["box/" + "b" * 32], damaged=damaged
    )

    response = client.get("/api/v1/outputs/copy-status")
    assert response.status_code == 200
    body = response.json()
    assert datetime.fromisoformat(body["started_at"]) <= datetime.fromisoformat(body["finished_at"])
    assert (body["copied"], body["refreshed"], body["dropped"]) == (3, 2, 1)
    assert body["not_copied"] == ["box/" + "b" * 32]
    assert body["damaged"] == damaged
    assert body["incomplete"] is False
    assert "id" not in body

    # A run that has not finished is the last run: it has no end and counts nothing yet.
    rows.start_run()
    later = client.get("/api/v1/outputs/copy-status").json()
    assert later["finished_at"] is None
    assert (later["copied"], later["refreshed"], later["dropped"]) == (0, 0, 0)
    assert later["not_copied"] == [] and later["damaged"] == []
    assert later["incomplete"] is False


def test_copy_status_shows_an_incomplete_run(client: TestClient) -> None:
    """A walk that failed part-way records its counts and ``incomplete``, and no end."""
    rows = _rows(client)
    run = rows.start_run()
    rows.end_incomplete_run(run, copied=4, refreshed=0, dropped=0, not_copied=[], damaged=[])
    body = client.get("/api/v1/outputs/copy-status").json()
    assert body["incomplete"] is True
    assert body["finished_at"] is None
    assert body["copied"] == 4
    assert rows.copied() is False


def test_copy_status_404_before_any_run(client: TestClient) -> None:
    response = client.get("/api/v1/outputs/copy-status")
    assert response.status_code == 404
    assert "copy" in response.json()["detail"]


def _row(output_id: str) -> OutputRow:
    now = datetime.now(UTC)
    meta = OutputMeta(
        id=output_id,
        slug="box",
        job_id="j",
        created_at=now,
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    )
    return OutputRow(
        output_id=output_id,
        slug="box",
        meta=meta,
        params={},
        inputs=None,
        record=None,
        manifest=None,
        bom=None,
        arranged_from=None,
        backfill=None,
        has_plate_cover=False,
        copied_at=now,
    )


def test_copy_status_reports_byte_run_and_pending(client: TestClient) -> None:
    """#7256: the last byte copy run, and how many rows the files still hold the bytes
    of now."""
    rows = _rows(client)
    rows.finish_run(rows.start_run(), copied=0, refreshed=0, dropped=0, not_copied=[], damaged=[])
    for output_id in ("a" * 32, "b" * 32):
        rows.insert(_row(output_id))
    rows.write_byte_copy([ByteWrite("a" * 32, OutputBlobs(), "stamp", [])])
    failed = [{"output_id": "b" * 32, "role": "model", "error": "OSError"}]
    damaged = [{"output_id": "c" * 32, "role": "preview", "error": "missing"}]
    run = rows.start_byte_run()
    rows.end_byte_run(
        run,
        finished=True,
        incomplete=False,
        refused=None,
        copied=5,
        verified=4,
        skipped=3,
        needed_bytes=1024,
        needed_count=2,
        failed=failed,
        damaged=damaged,
    )

    body = client.get("/api/v1/outputs/copy-status").json()
    byte_run = body["bytes"]
    assert byte_run is not None
    started = datetime.fromisoformat(byte_run["started_at"])
    assert started <= datetime.fromisoformat(byte_run["finished_at"])
    assert byte_run["incomplete"] is False and byte_run["refused"] is None
    counts = ("copied", "verified", "skipped", "needed_bytes", "needed_count")
    assert tuple(byte_run[name] for name in counts) == (5, 4, 3, 1024, 2)
    assert byte_run["failed"] == failed and byte_run["damaged"] == damaged
    assert byte_run["pending"] == 1
    assert "id" not in byte_run

    # A refused run is the last run: no end, the reason, and nothing copied.
    refused = rows.start_byte_run()
    rows.end_byte_run(
        refused,
        finished=False,
        incomplete=False,
        refused="the store is full",
        copied=0,
        verified=0,
        skipped=0,
        needed_bytes=2048,
        needed_count=1,
        failed=[],
        damaged=[],
    )
    later = client.get("/api/v1/outputs/copy-status").json()["bytes"]
    assert later["finished_at"] is None
    assert later["refused"] == "the store is full"
    assert later["pending"] == 1


def test_copy_status_bytes_null_before_any_byte_run(client: TestClient) -> None:
    rows = _rows(client)
    rows.finish_run(rows.start_run(), copied=0, refreshed=0, dropped=0, not_copied=[], damaged=[])
    rows.insert(_row("a" * 32))
    body = client.get("/api/v1/outputs/copy-status").json()
    assert body["bytes"] is None


def _end_byte_run(rows: OutputRows, damaged: list[dict[str, Any]]) -> None:
    rows.end_byte_run(
        rows.start_byte_run(),
        finished=True,
        incomplete=False,
        refused=None,
        copied=0,
        verified=0,
        skipped=0,
        needed_bytes=0,
        needed_count=0,
        failed=[],
        damaged=damaged,
    )


def test_copy_status_reports_the_rows_damage(client: TestClient) -> None:
    """#7256: the rows' damage, not only the last run's: a later run skips an output an
    earlier run marked damaged, so its ``damaged`` no longer names it."""
    rows = _rows(client)
    rows.finish_run(rows.start_run(), copied=0, refreshed=0, dropped=0, not_copied=[], damaged=[])
    preview = [{"role": "preview", "error": "no preview.glb"}]
    model = [{"role": "model", "error": "no model.3mf"}]
    writes = {"c" * 32: preview, "a" * 32: model, "b" * 32: []}
    for output_id in writes:
        rows.insert(_row(output_id))
    rows.write_byte_copy(
        [ByteWrite(output_id, OutputBlobs(), "stamp", found) for output_id, found in writes.items()]
    )
    _end_byte_run(rows, [])

    byte_run = client.get("/api/v1/outputs/copy-status").json()["bytes"]
    assert byte_run["damaged"] == []
    assert byte_run["damaged_rows"] == 2
    assert byte_run["damaged_sample"] == [
        {"output_id": "a" * 32, "damaged": model},
        {"output_id": "c" * 32, "damaged": preview},
    ]


def test_copy_status_damage_sample_is_bounded(client: TestClient) -> None:
    rows = _rows(client)
    rows.finish_run(rows.start_run(), copied=0, refreshed=0, dropped=0, not_copied=[], damaged=[])
    damaged = [{"role": "preview", "error": "no preview.glb"}]
    ids = [f"{n:032x}" for n in range(DAMAGED_SAMPLE + 1)]
    for output_id in ids:
        rows.insert(_row(output_id))
    rows.write_byte_copy([ByteWrite(output_id, OutputBlobs(), "s", damaged) for output_id in ids])
    _end_byte_run(rows, [])
    byte_run = client.get("/api/v1/outputs/copy-status").json()["bytes"]
    assert byte_run["damaged_rows"] == DAMAGED_SAMPLE + 1
    assert [item["output_id"] for item in byte_run["damaged_sample"]] == ids[:DAMAGED_SAMPLE]


def test_copy_status_reports_cut_over_at(client: TestClient) -> None:
    """#7257: when the cut-over mark was written; ``None`` before it."""
    rows = _rows(client)
    rows.finish_run(rows.start_run(), copied=0, refreshed=0, dropped=0, not_copied=[], damaged=[])
    _end_byte_run(rows, [])
    assert client.get("/api/v1/outputs/copy-status").json()["cut_over_at"] is None
    assert rows.try_cut_over() is True
    at = rows.cut_over_at()
    assert at is not None
    body = client.get("/api/v1/outputs/copy-status").json()
    assert datetime.fromisoformat(body["cut_over_at"]) == at
