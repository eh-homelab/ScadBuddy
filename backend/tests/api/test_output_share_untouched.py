"""Release B touches the old outputs share in no way (#7257, plan D10, Review Focus 5): the
rows and the blobs are the only record. The model delete, the orphan sweep and the hold
reaper read rows."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.output_rows import OUTPUT_HOLDER
from scadbuddy.library.outputs import hold_parts, reap_orphan_holds, release_parts
from scadbuddy.main import sweep_assets
from scadbuddy.render.job_models import ManifestObject
from tests.api.conftest import PNG_BYTES, wait_for_job
from tests.support.operations import press
from tests.support.row_outputs import insert_row, output_meta

pytestmark = pytest.mark.requires_postgres

#: The audit events whose first arguments are paths: every open, listing, write, rename
#: and removal (`sys.addaudithook`). A ``stat`` raises no event; the folder's own listing
#: at the end covers what a write would change.
_PATH_EVENTS = frozenset(
    {
        "open",
        "os.listdir",
        "os.scandir",
        "os.mkdir",
        "os.rename",
        "os.remove",
        "os.rmdir",
        "os.chmod",
        "os.utime",
        "os.truncate",
        "os.link",
        "os.symlink",
        "shutil.rmtree",
    }
)

#: The share's root while a test watches it, and what touched it. An audit hook cannot be
#: removed, so one hook serves every test of this process.
_watched: list[str] = []
_touched: list[tuple[str, str]] = []


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if not _watched or event not in _PATH_EVENTS:
        return
    for arg in args[:2]:
        if isinstance(arg, str | bytes | os.PathLike):
            path = os.fsdecode(os.fspath(arg))
            if path == _watched[0] or path.startswith(_watched[0] + os.sep):
                _touched.append((event, path))


sys.addaudithook(_audit)


def _tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


@pytest.fixture
def share(paths: DataPaths) -> Iterator[Path]:
    """The old outputs folder, as prod still has it, with an output's backup folder in
    it; any access under it while the test runs is recorded."""
    root = paths.root / "outputs"
    backup = root / "gone" / ("b" * 32)
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "meta.json").write_text("{}", encoding="utf-8")
    before = _tree(root)
    _touched.clear()
    _watched[:] = [str(root)]
    try:
        yield root
    finally:
        _watched.clear()
    assert _touched == []
    assert _tree(root) == before


def _state(client: TestClient) -> AppState:
    app = client.app
    assert isinstance(app, FastAPI)
    state: AppState = getattr(app.state, STATE_ATTR)
    return state


def _save(client: TestClient, model: str, width: float) -> str:
    job_id: str = client.post(
        f"/api/v1/models/{model}/render", json={"params": {"width": width}}
    ).json()["job_id"]
    wait_for_job(client, job_id)
    saved = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job_id}, headers=press())
    assert saved.status_code == 201, saved.text
    output_id: str = saved.json()["id"]
    return output_id


def test_no_output_path_touches_the_share(client: TestClient, model: str, share: Path) -> None:
    state = _state(client)
    first = _save(client, model, 1)
    second = _save(client, model, 2)
    listed = client.get(f"/api/v1/models/{model}/outputs").json()
    assert {row["id"] for row in listed} == {first, second}
    assert {row["id"] for row in client.get("/api/v1/outputs").json()} == {first, second}
    download = client.get(f"/api/v1/outputs/{first}/model.3mf")
    assert download.status_code == 200 and download.content
    renamed = client.patch(f"/api/v1/outputs/{first}", json={"name": "kept"}, headers=press())
    assert renamed.status_code == 200, renamed.text
    put = client.put(
        f"/api/v1/outputs/{first}/thumbnail",
        files={"file": ("capture.png", PNG_BYTES, "image/png")},
        headers=press(),
    )
    assert put.status_code == 204, put.text
    assert client.delete(f"/api/v1/outputs/{second}", headers=press()).status_code == 204
    assert client.delete(f"/api/v1/models/{model}", headers=press()).status_code == 204
    assert state.outputs.rows.for_slug(model) == []
    reap_orphan_holds(state.refs, state.outputs, grace=timedelta(0))
    portal = client.portal
    assert portal is not None
    portal.call(sweep_assets, state)


def test_model_delete_forgets_rows_after_rename(
    client: TestClient, model: str, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model goes first, then its rows: a rename that fails keeps both, and no
    folder is read to put rows back."""
    state = _state(client)
    meta = output_meta("c" * 32, model)
    insert_row(state.outputs, meta)
    rename = Path.rename

    def refused(self: Path, target: Any) -> Path:
        if self == paths.model_dir(model):
            raise OSError("EBUSY")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", refused)
    assert client.delete(f"/api/v1/models/{model}", headers=press()).status_code >= 500
    assert state.outputs.rows.ids_for_slug(model) == [meta.id]
    monkeypatch.setattr(Path, "rename", rename)
    assert client.delete(f"/api/v1/models/{model}", headers=press()).status_code == 204
    assert state.outputs.rows.ids_for_slug(model) == []


def test_sweep_orphans_forgets_rows_of_missing_model(
    client: TestClient, model: str, share: Path
) -> None:
    state = _state(client)
    gone = output_meta("d" * 32, "gone")
    live = output_meta("e" * 32, model)
    insert_row(state.outputs, gone)
    insert_row(state.outputs, live)
    removed = state.catalogue.sweep_orphans()
    assert "outputs/gone" in removed
    assert state.outputs.rows.ids_for_slug("gone") == []
    assert state.outputs.rows.ids_for_slug(model) == [live.id]


def test_reaper_releases_holds_of_output_without_row(
    client: TestClient, model: str, share: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The reaper reads rows only, and `hold_parts` / `release_parts` write no slug to
    the dropped ``output_hold_slugs``: no warning per call."""
    state = _state(client)
    kept = output_meta("f" * 32, model)
    insert_row(state.outputs, kept)
    box = {"min": [0, 0, 0], "max": [1, 1, 1], "size": [1, 1, 1]}
    part = ManifestObject.model_validate(
        {
            "part": "piece-a",
            "file": "a.3mf",
            "slug": model,
            "revision": None,
            "bbox": box,
            "footprint": [1, 1],
            "colours": ["#FF0000"],
            "count": 1,
        }
    )
    caplog.set_level(logging.WARNING)
    hold_parts(state.refs, kept.id, [part])
    hold_parts(state.refs, "9" * 32, [part])
    assert "its table is gone" not in caplog.text
    assert reap_orphan_holds(state.refs, state.outputs, grace=timedelta(0)) == 1
    with state.refs.pool.connection() as conn:
        held = conn.execute(
            "SELECT holder_id FROM blob_refs WHERE holder_kind = %s ORDER BY holder_id",
            (OUTPUT_HOLDER,),
        ).fetchall()
    assert [row["holder_id"] for row in held] == [kept.id]
    release_parts(state.refs, kept.id)
    assert "its table is gone" not in caplog.text
