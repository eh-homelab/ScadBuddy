"""The catalogue thumbnail's `ETag` (#7317 phase 2, plan Task 9; phase 3: from the
start, which copies first): it names the source the template's row points at, so a matching
`If-None-Match` is a 304 with no file, archive or preview read. A 200 tags what it
read (plan decision 12)."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.previews import PREVIEW_ID_LENGTH, PreviewStore
from tests.api.conftest import PNG_BYTES
from tests.api.test_models_from_rows import (
    BUILTIN,
    COVER,
    PREVIEW,
    _copy,
    _create,
    _refresh_fails,
    _rows,
    _state,
    bundled,
    client,
    world,
)
from tests.support.operations import press

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]

__all__ = ["bundled", "client", "world"]

#: The world's preview, rendered from the source key ``"f" * 64``.
PREVIEW_ETAG = f'"preview-{"f" * PREVIEW_ID_LENGTH}"'


def _thumbnail(client: TestClient, slug: str, etag: str | None = None) -> Any:
    headers = {} if etag is None else {"If-None-Match": etag}
    return client.get(f"/api/v1/models/{slug}/thumbnail", headers=headers)


def _catalogue(client: TestClient) -> Catalogue:
    catalogue: Catalogue = _state(client).catalogue
    return catalogue


def _refused(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("read a file")


def _no_reads(monkeypatch: pytest.MonkeyPatch, paths: DataPaths) -> None:
    """No file under the data volume is read or stat'ed, and no archive or preview
    image is read. Paths elsewhere (pytest's own) are left alone."""
    for name in ("read_bytes", "open", "stat", "is_file", "exists"):
        original = getattr(Path, name)

        def guarded(self: Path, *args: Any, _original: Any = original, **kwargs: Any) -> Any:
            if self.is_relative_to(paths.root):
                raise AssertionError(f"touched {self}")
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, guarded)
    monkeypatch.setattr(zipfile, "ZipFile", _refused)
    monkeypatch.setattr(PreviewStore, "image", _refused)
    monkeypatch.setattr(PreviewStore, "current_image", _refused)


def test_own_cover_etag_is_the_hash_of_its_bytes(world: TestClient) -> None:
    before = _thumbnail(world, "widget")
    assert before.status_code == 200, before.text
    assert before.headers["etag"] == f'"{hashlib.sha256(PNG_BYTES).hexdigest()}"'

    _copy(world)

    after = _thumbnail(world, "widget")
    assert (after.status_code, after.content) == (200, PNG_BYTES)
    # The same value after a copy, so the clients' copies stay valid.
    assert after.headers["etag"] == before.headers["etag"]
    assert after.headers["cache-control"] == before.headers["cache-control"]


@pytest.mark.parametrize("slug", ["widget", "gadget", "plain"])
def test_a_304_reads_no_file(
    world: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch, slug: str
) -> None:
    """Each source: the own cover, an output's plate cover and the preview."""
    _copy(world)
    etag = _thumbnail(world, slug).headers["etag"]
    _no_reads(monkeypatch, paths)

    again = _thumbnail(world, slug, etag)

    assert (again.status_code, again.content) == (304, b"")
    assert again.headers["etag"] == etag


def test_output_cover_etag_names_the_output(world: TestClient) -> None:
    before = _thumbnail(world, "gadget")
    assert before.status_code == 200, before.text
    _copy(world)
    outputs = _state(world).outputs
    output_id = outputs.plate_cover_output("gadget")
    assert output_id is not None

    after = _thumbnail(world, "gadget")

    assert (after.status_code, after.content) == (200, COVER)
    assert after.content == before.content
    assert after.headers["etag"] == f'"output-{output_id}"'
    assert after.headers["content-type"] == "image/png"


def test_preview_etag_names_the_preview(world: TestClient) -> None:
    _copy(world)

    after = _thumbnail(world, "plain")

    assert (after.status_code, after.content) == (200, PREVIEW)
    assert after.headers["etag"] == PREVIEW_ETAG
    assert after.headers["content-type"] == "image/png"


def test_a_200_tags_what_it_read(world: TestClient) -> None:
    """The preview is rendered again between the reference and its read: the answer
    is the new image, tagged with the new preview's id."""
    _copy(world)
    catalogue = _catalogue(world)
    ref = asyncio.run(catalogue.thumbnail_ref("plain"))
    assert ref is not None and ref.etag == PREVIEW_ETAG
    previews = catalogue.previews
    assert previews is not None
    rendered = PREVIEW + b" again"
    assert previews.write("plain", "e" * 64, rendered)

    assert asyncio.run(ref.read()) == (
        rendered,
        "image/png",
        f'"preview-{"e" * PREVIEW_ID_LENGTH}"',
    )
    answer = _thumbnail(world, "plain", PREVIEW_ETAG)
    assert (answer.status_code, answer.content) == (200, rendered)
    assert answer.headers["etag"] == f'"preview-{"e" * PREVIEW_ID_LENGTH}"'


def test_a_template_with_no_row_falls_back_to_its_files(world: TestClient) -> None:
    """#7631 review: a folder whose row is gone (a failed refresh) is read from its
    files, as its record is; no row and no folder is 404."""
    before = _thumbnail(world, "plain")
    assert before.status_code == 200, before.text
    _copy(world)
    _rows(world).put([], ["plain"])

    answer = _thumbnail(world, "plain")
    record = world.get("/api/v1/models/plain").json()

    assert answer.status_code == 200, answer.text
    assert answer.content == before.content
    assert answer.headers["etag"] == f'"{hashlib.sha256(answer.content).hexdigest()}"'
    assert record["thumbnail_source"] == "preview"
    nobody = _thumbnail(world, "nobody")
    assert nobody.status_code == 404, nobody.text
    assert "no model named" in nobody.json()["detail"]


def test_no_cover_is_404(world: TestClient) -> None:
    _create(world, "bare")
    before = _thumbnail(world, "bare")
    assert before.status_code == 404, before.text
    _copy(world)

    after = _thumbnail(world, "bare")

    assert (after.status_code, after.json()) == (404, before.json())


def test_a_cover_the_row_does_not_know_yet_is_served(
    world: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An image added while its row's refresh failed (the row lags its media rows,
    Task 8 review M1) is the thumbnail, from its files."""
    _copy(world)
    _refresh_fails(monkeypatch)
    image = PNG_BYTES + b" plain"
    added = world.post(
        "/api/v1/models/plain/media",
        files={"file": ("first.png", image, "image/png")},
        headers=press(),
    )
    assert added.status_code == 200, added.text
    row = _rows(world).get("plain")
    assert row is not None and row.cover is None

    answer = _thumbnail(world, "plain")

    assert (answer.status_code, answer.content) == (200, image)
    assert answer.headers["etag"] == f'"{hashlib.sha256(image).hexdigest()}"'
    assert paths.model_dir("plain").is_dir()


def _media_ids(client: TestClient, slug: str) -> list[str]:
    record = client.get(f"/api/v1/models/{slug}")
    assert record.status_code == 200, record.text
    return [item["id"] for item in record.json()["media"]]


def test_a_media_list_emptied_while_the_row_lags_gets_no_304(
    world: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review M3 (a): every item is removed while the refresh fails. The row
    still names the old cover, but the removal marks the row as lagging, so the old
    tag gets no 304."""
    _copy(world)
    etag = _thumbnail(world, "widget").headers["etag"]
    _refresh_fails(monkeypatch)
    for item_id in _media_ids(world, "widget"):
        removed = world.delete(f"/api/v1/models/widget/media/{item_id}", headers=press())
        assert removed.status_code == 200, removed.text
    row = _rows(world).get("widget")
    assert row is not None and row.cover is not None

    answer = _thumbnail(world, "widget", etag)

    assert answer.status_code == 404, answer.text


def test_a_builtin_cover_choice_while_the_row_lags_gets_no_304(
    world: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review M3 (b): a built-in's cover is chosen while the refresh fails. The
    choice writes no `template_media` row, but it marks the row as lagging too."""
    _copy(world)
    etag = _thumbnail(world, BUILTIN).headers["etag"]
    _refresh_fails(monkeypatch)
    added = _media_ids(world, BUILTIN)[-1]
    chosen = world.put(f"/api/v1/models/{BUILTIN}/media/cover", json={"id": added}, headers=press())
    assert chosen.status_code == 200, chosen.text

    answer = _thumbnail(world, BUILTIN, etag)

    assert (answer.status_code, answer.content) == (200, PNG_BYTES + b"!")
    assert answer.headers["etag"] != etag


def test_a_row_whose_folder_is_gone_has_no_thumbnail(world: TestClient, paths: DataPaths) -> None:
    """Final review M4: a row left by a hand removal answers 404, as `record` does,
    and not the preview that its row still points at."""
    _copy(world)
    shutil.rmtree(paths.model_dir("plain"))
    assert _rows(world).get("plain") is not None
    assert world.get("/api/v1/models/plain").status_code == 404

    answer = _thumbnail(world, "plain")

    assert answer.status_code == 404, answer.text
    assert "no model named" in answer.json()["detail"]
