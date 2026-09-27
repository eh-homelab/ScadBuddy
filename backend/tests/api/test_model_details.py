"""#179 -- a model's thumbnail, README and metadata, set after it was created."""

from __future__ import annotations

import itertools
import json
import shutil
import threading
import zipfile
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import scadbuddy.api.models as models_api
from scadbuddy.api import limits
from scadbuddy.api.models import MAX_SOURCE_CHARS, MAX_THUMBNAIL_BYTES, _mib
from scadbuddy.core.paths import DataPaths
from scadbuddy.library import outputs as outputs_module
from scadbuddy.render import provenance
from scadbuddy.render.bambu3mf import PLATE_THUMBNAIL
from tests.api.conftest import PNG_BYTES, wait_for_job

SLUG = "widget"
SOURCE = "width = 10;\ncube(width);\n"
OTHER_PNG = b"\x89PNG\r\n\x1a\n" + b"another png body"
COVER_ONE = b"\x89PNG\r\n\x1a\n" + b"first output cover"
COVER_TWO = b"\x89PNG\r\n\x1a\n" + b"second output cover"
# Distinct parameters per render, so no two outputs can share a job's files.
_WIDTHS = itertools.count(11)


def _create(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/v1/models",
        files={"file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _put_thumbnail(client: TestClient, png: bytes, slug: str = SLUG) -> httpx.Response:
    response: httpx.Response = client.put(
        f"/api/v1/models/{slug}/thumbnail", files={"file": ("thumb.png", png, "image/png")}
    )
    return response


def _put_readme(client: TestClient, content: str, slug: str = SLUG) -> httpx.Response:
    response: httpx.Response = client.put(
        f"/api/v1/models/{slug}/readme", json={"content": content}
    )
    return response


def _generate(client: TestClient, paths: DataPaths, slug: str, cover: bytes | None) -> str:
    """Render and save an output; `cover` goes into its 3MF as the plate image.

    The test render writes no cover images, as a real one does when the cover step
    times out, so the plate image is added to the job's 3MF -- before the output is
    saved, the way a real render carries it, so nothing changes behind the store.
    """
    job_id = client.post(
        f"/api/v1/models/{slug}/render", json={"params": {"width": next(_WIDTHS)}}
    ).json()["job_id"]
    wait_for_job(client, job_id)
    if cover is not None:
        with zipfile.ZipFile(paths.job_work_dir(job_id) / "model.3mf", "a") as archive:
            archive.writestr(PLATE_THUMBNAIL, cover)
    response = client.post(f"/api/v1/models/{slug}/outputs", json={"job_id": job_id})
    assert response.status_code == 201, response.text
    output_id: str = response.json()["id"]
    return output_id


class _Counting:
    """Counts calls through to the wrapped callable."""

    def __init__(self, wrapped: Callable[..., Any]) -> None:
        self.wrapped = wrapped
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self.wrapped(*args, **kwargs)


@pytest.fixture
def zip_opens(monkeypatch: pytest.MonkeyPatch) -> _Counting:
    """Every archive opened from here on; only the output store opens any on these routes."""
    counter = _Counting(zipfile.ZipFile)
    monkeypatch.setattr(zipfile, "ZipFile", counter)
    return counter


def _scans(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> _Counting:
    """Every full rescan of a model's outputs for its cover, from here on."""
    store = client.app.state.scadbuddy.outputs  # type: ignore[attr-defined]
    counter = _Counting(store._scan_plate_cover)
    monkeypatch.setattr(store, "_scan_plate_cover", counter)
    return counter


def _listed(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/v1/models")
    assert response.status_code == 200
    return {model["slug"]: model for model in response.json()}


# ── thumbnail ─────────────────────────────────────────────────────────────────


def test_a_thumbnail_can_be_set_after_creation(client: TestClient) -> None:
    assert _create(client)["has_thumbnail"] is False

    response = _put_thumbnail(client, PNG_BYTES)

    assert response.status_code == 200
    body = response.json()
    assert body["has_thumbnail"] is True
    assert body["thumbnail_source"] == "model"
    served = client.get(f"/api/v1/models/{SLUG}/thumbnail")
    assert served.status_code == 200
    assert served.content == PNG_BYTES
    assert served.headers["content-type"] == "image/png"


def test_a_thumbnail_can_be_replaced(client: TestClient) -> None:
    _create(client)
    assert _put_thumbnail(client, PNG_BYTES).status_code == 200
    assert _put_thumbnail(client, OTHER_PNG).status_code == 200
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == OTHER_PNG


def test_a_thumbnail_that_is_not_a_png_is_refused_and_nothing_changes(
    client: TestClient, paths: DataPaths
) -> None:
    _create(client)

    response = _put_thumbnail(client, b"GIF89a not a png")

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == "the thumbnail is not a PNG"
    assert not (paths.model_dir(SLUG) / "thumbnail.png").exists()


def test_a_thumbnail_can_be_removed(client: TestClient, paths: DataPaths) -> None:
    _create(client)
    _put_thumbnail(client, PNG_BYTES)

    response = client.delete(f"/api/v1/models/{SLUG}/thumbnail")

    assert response.status_code == 200
    assert response.json()["has_thumbnail"] is False
    assert response.json()["thumbnail_source"] is None
    assert not (paths.model_dir(SLUG) / "thumbnail.png").exists()
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").status_code == 404


def test_removing_a_thumbnail_that_is_not_there_is_a_404(client: TestClient) -> None:
    _create(client)
    response = client.delete(f"/api/v1/models/{SLUG}/thumbnail")
    assert response.status_code == 404
    assert "no thumbnail" in response.json()["detail"]


# ── thumbnail caching ─────────────────────────────────────────────────────────


def _etag(client: TestClient, slug: str = SLUG) -> str:
    response = client.get(f"/api/v1/models/{slug}/thumbnail")
    assert response.status_code == 200, response.text
    etag: str = response.headers["etag"]
    return etag


def test_a_thumbnail_carries_a_strong_etag_and_is_revalidated(client: TestClient) -> None:
    _create(client)
    _put_thumbnail(client, PNG_BYTES)

    first = client.get(f"/api/v1/models/{SLUG}/thumbnail")
    assert first.headers["cache-control"] == "no-cache"
    etag = first.headers["etag"]
    assert etag.startswith('"') and not etag.startswith("W/")
    # Stable: the same bytes, the same tag.
    assert _etag(client) == etag


@pytest.mark.parametrize("if_none_match", ["{etag}", "W/{etag}", '"other", {etag}', "*"])
def test_a_current_copy_is_answered_304_without_a_body(
    client: TestClient, if_none_match: str
) -> None:
    _create(client)
    _put_thumbnail(client, PNG_BYTES)
    etag = _etag(client)

    response = client.get(
        f"/api/v1/models/{SLUG}/thumbnail",
        headers={"If-None-Match": if_none_match.format(etag=etag)},
    )

    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == etag
    assert response.headers["cache-control"] == "no-cache"


def test_a_stale_copy_gets_the_image(client: TestClient) -> None:
    _create(client)
    _put_thumbnail(client, PNG_BYTES)
    response = client.get(f"/api/v1/models/{SLUG}/thumbnail", headers={"If-None-Match": '"old"'})
    assert (response.status_code, response.content) == (200, PNG_BYTES)


def test_the_etag_follows_every_change_of_image(client: TestClient, paths: DataPaths) -> None:
    _create(client)
    first = _generate(client, paths, SLUG, COVER_ONE)
    _generate(client, paths, SLUG, COVER_TWO)
    seen = [_etag(client)]  # the first output's plate image

    _put_thumbnail(client, PNG_BYTES)  # a thumbnail of its own
    seen.append(_etag(client))
    _put_thumbnail(client, OTHER_PNG)  # replaced
    seen.append(_etag(client))
    assert client.delete(f"/api/v1/models/{SLUG}/thumbnail").status_code == 200
    seen.append(_etag(client))  # back to the first output's plate image
    assert client.delete(f"/api/v1/outputs/{first}").status_code == 204
    seen.append(_etag(client))  # the next output's

    assert seen[0] == seen[3]  # the same bytes as before, the same tag
    assert len({seen[0], seen[1], seen[2], seen[4]}) == 4
    # A copy validated against an older tag is sent the new image, not a 304.
    stale = client.get(f"/api/v1/models/{SLUG}/thumbnail", headers={"If-None-Match": seen[0]})
    assert (stale.status_code, stale.content) == (200, COVER_TWO)


# ── the thumbnail size cap ────────────────────────────────────────────────────


def _png_of(size: int) -> bytes:
    return PNG_BYTES + b"\x00" * (size - len(PNG_BYTES))


@pytest.mark.requires_git
def test_a_thumbnail_one_byte_over_the_cap_is_refused_and_nothing_is_committed(
    client: TestClient, paths: DataPaths
) -> None:
    before = _create(client)
    history = client.get(f"/api/v1/models/{SLUG}/versions").json()

    response = _put_thumbnail(client, _png_of(MAX_THUMBNAIL_BYTES + 1))

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == (
        f"the thumbnail is too large: {MAX_THUMBNAIL_BYTES + 1} bytes, "
        f"and a thumbnail is at most {MAX_THUMBNAIL_BYTES} bytes (10 MiB)"
    )
    assert not (paths.model_dir(SLUG) / "thumbnail.png").exists()
    assert client.get(f"/api/v1/models/{SLUG}/versions").json() == history
    assert client.get(f"/api/v1/models/{SLUG}").json()["version"] == before["version"]


def test_a_thumbnail_one_byte_over_the_cap_is_refused_on_create(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models",
        files={
            "file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream"),
            "thumbnail": ("t.png", _png_of(MAX_THUMBNAIL_BYTES + 1), "image/png"),
        },
    )
    assert response.status_code == 422
    assert str(MAX_THUMBNAIL_BYTES) in response.json()["detail"]
    assert client.get(f"/api/v1/models/{SLUG}").status_code == 404


def test_one_create_at_every_part_cap_fits_the_multipart_limit(client: TestClient) -> None:
    """A thumbnail at 10 MiB beside a source and a README each at MAX_SOURCE_CHARS in
    four-byte characters, and a model.json at its cap, is under BodySizeGate's
    multipart ceiling: no part's own cap is shadowed by the body's."""
    widest = "\U0001f600"  # four bytes in UTF-8, one character to the caps
    source = "// " + widest * (MAX_SOURCE_CHARS - 4) + "\n"
    readme = widest * MAX_SOURCE_CHARS
    meta = json.dumps({"name": "Widget"}).encode()
    meta += b" " * (models_api.MAX_META_BYTES - len(meta))
    parts = {
        "file": (f"{SLUG}.scad", source.encode(), "application/octet-stream"),
        "thumbnail": ("t.png", _png_of(MAX_THUMBNAIL_BYTES), "image/png"),
        "readme": ("README.md", readme.encode(), "text/markdown"),
        "meta": ("model.json", meta, "application/json"),
    }
    assert sum(len(part[1]) for part in parts.values()) < limits.MAX_MULTIPART_BODY_BYTES

    created = client.post("/api/v1/models?force=true", files=parts)

    assert created.status_code == 201, created.text
    assert created.json()["has_thumbnail"] is True


def test_a_thumbnail_at_the_cap_is_accepted(client: TestClient) -> None:
    at_cap = _png_of(MAX_THUMBNAIL_BYTES)
    created = client.post(
        "/api/v1/models",
        files={
            "file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream"),
            "thumbnail": ("t.png", at_cap, "image/png"),
        },
    )
    assert created.status_code == 201, created.text
    assert _put_thumbnail(client, at_cap).status_code == 200
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == at_cap


# ── the plate-image fallback ──────────────────────────────────────────────────


def test_a_generated_model_without_a_thumbnail_shows_its_first_outputs_plate_image(
    client: TestClient, paths: DataPaths
) -> None:
    _create(client)
    _generate(client, paths, SLUG, COVER_ONE)
    _generate(client, paths, SLUG, COVER_TWO)

    record = client.get(f"/api/v1/models/{SLUG}").json()
    assert record["has_thumbnail"] is True
    assert record["thumbnail_source"] == "output"
    listed = {model["slug"]: model for model in client.get("/api/v1/models").json()}
    assert listed[SLUG]["has_thumbnail"] is True
    # The FIRST output, not the latest: the card does not change with every render.
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == COVER_ONE


def test_an_output_without_a_plate_image_is_skipped_for_the_next_one(
    client: TestClient, paths: DataPaths
) -> None:
    _create(client)
    _generate(client, paths, SLUG, None)
    _generate(client, paths, SLUG, COVER_TWO)

    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == COVER_TWO


def test_a_model_whose_outputs_have_no_plate_image_has_no_thumbnail(
    client: TestClient, paths: DataPaths
) -> None:
    _create(client)
    _generate(client, paths, SLUG, None)

    assert client.get(f"/api/v1/models/{SLUG}").json()["has_thumbnail"] is False
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").status_code == 404


def test_a_thumbnail_of_its_own_wins_over_the_fallback_and_removing_it_restores_it(
    client: TestClient, paths: DataPaths
) -> None:
    _create(client)
    _generate(client, paths, SLUG, COVER_ONE)

    _put_thumbnail(client, PNG_BYTES)
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == PNG_BYTES

    removed = client.delete(f"/api/v1/models/{SLUG}/thumbnail").json()
    assert removed["has_thumbnail"] is True
    assert removed["thumbnail_source"] == "output"
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == COVER_ONE


def test_an_unreadable_output_does_not_break_the_catalogue(
    client: TestClient, paths: DataPaths
) -> None:
    _create(client)
    broken = paths.output_dir(SLUG, "f" * 32)
    broken.mkdir(parents=True)
    (broken / "meta.json").write_text("{not json", encoding="utf-8")
    good = _generate(client, paths, SLUG, COVER_ONE)
    assert good != "f" * 32

    assert client.get("/api/v1/models").status_code == 200
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == COVER_ONE


# ── the fallback is resolved once, not per listing ────────────────────────────


def test_a_second_listing_opens_no_archive(
    client: TestClient, paths: DataPaths, zip_opens: _Counting
) -> None:
    _create(client)
    _generate(client, paths, SLUG, None)
    _generate(client, paths, SLUG, COVER_TWO)
    assert _listed(client)[SLUG]["thumbnail_source"] == "output"

    before = zip_opens.calls
    for _ in range(3):
        assert _listed(client)[SLUG]["has_thumbnail"] is True
        assert client.get(f"/api/v1/models/{SLUG}").json()["has_thumbnail"] is True

    assert zip_opens.calls == before


def test_the_thumbnail_after_a_listing_reads_only_the_resolved_archive(
    client: TestClient, paths: DataPaths, zip_opens: _Counting, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create(client)
    _generate(client, paths, SLUG, None)
    _generate(client, paths, SLUG, COVER_TWO)
    _listed(client)
    scans = _scans(client, monkeypatch)
    before = zip_opens.calls

    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == COVER_TWO

    assert scans.calls == 0
    # The one archive holding the cover -- not the coverless output before it.
    assert zip_opens.calls == before + 1


def test_a_new_output_with_a_cover_is_picked_up(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create(client)
    _generate(client, paths, SLUG, None)
    assert _listed(client)[SLUG]["has_thumbnail"] is False
    scans = _scans(client, monkeypatch)
    _listed(client)
    assert scans.calls == 0

    _generate(client, paths, SLUG, COVER_ONE)

    assert _listed(client)[SLUG]["thumbnail_source"] == "output"
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == COVER_ONE


def test_a_lookup_racing_an_output_being_saved_is_not_kept(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing that lands while an output is half-written (its 3MF is there, its
    record is not) resolves to nothing. The outputs directory's mtime does not move
    again when the record lands, so only `create` forgetting it makes it right."""
    _create(client)
    store = client.app.state.scadbuddy.outputs  # type: ignore[attr-defined]

    def stamp_then_list(*args: Any, **kwargs: Any) -> None:
        provenance.stamp(*args, **kwargs)
        assert store.plate_cover_archive(SLUG) is None

    monkeypatch.setattr(outputs_module, "stamp", stamp_then_list)

    _generate(client, paths, SLUG, COVER_ONE)

    assert _listed(client)[SLUG]["thumbnail_source"] == "output"
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == COVER_ONE


def test_deleting_a_model_forgets_its_resolved_cover(client: TestClient, paths: DataPaths) -> None:
    _create(client)
    _generate(client, paths, SLUG, COVER_ONE)
    assert _listed(client)[SLUG]["thumbnail_source"] == "output"
    store = client.app.state.scadbuddy.outputs  # type: ignore[attr-defined]
    assert store.remembers_plate_cover(SLUG)

    assert client.delete(f"/api/v1/models/{SLUG}").status_code == 204

    assert not store.remembers_plate_cover(SLUG)
    # Nothing else is kept per slug: the only other state is one store-wide counter.
    assert SLUG not in store._covers
    assert not [
        value for value in vars(store).values() if isinstance(value, dict) and SLUG in value
    ]


def test_a_cover_scan_in_flight_across_a_model_delete_is_not_stored(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create(client)
    _generate(client, paths, SLUG, COVER_ONE)
    store = client.app.state.scadbuddy.outputs  # type: ignore[attr-defined]
    real_scan = store._scan_plate_cover

    def scan_then_delete(slug: str) -> Any:
        found = real_scan(slug)
        # The model goes away while this scan holds its answer.
        assert client.delete(f"/api/v1/models/{SLUG}").status_code == 204
        return found

    monkeypatch.setattr(store, "_scan_plate_cover", scan_then_delete)

    assert store.plate_cover_archive(SLUG) is not None

    assert not store.remembers_plate_cover(SLUG)


def test_the_record_names_the_output_behind_the_fallback(
    client: TestClient, paths: DataPaths
) -> None:
    """The fallback moves with no commit, so `version` cannot tell a client its image
    is stale; `thumbnail_output_id` can (#179)."""
    _create(client)
    _generate(client, paths, SLUG, None)
    first = _generate(client, paths, SLUG, COVER_ONE)
    second = _generate(client, paths, SLUG, COVER_TWO)

    record = client.get(f"/api/v1/models/{SLUG}").json()
    assert (record["thumbnail_source"], record["thumbnail_output_id"]) == ("output", first)
    assert _listed(client)[SLUG]["thumbnail_output_id"] == first

    assert client.delete(f"/api/v1/outputs/{first}").status_code == 204
    moved = client.get(f"/api/v1/models/{SLUG}").json()
    assert (moved["thumbnail_source"], moved["thumbnail_output_id"]) == ("output", second)
    assert moved["version"] == record["version"]

    assert client.delete(f"/api/v1/outputs/{second}").status_code == 204
    gone = _listed(client)[SLUG]
    assert (gone["has_thumbnail"], gone["thumbnail_source"], gone["thumbnail_output_id"]) == (
        False,
        None,
        None,
    )


def test_a_thumbnail_of_its_own_names_no_output(client: TestClient, paths: DataPaths) -> None:
    _create(client)
    assert client.get(f"/api/v1/models/{SLUG}").json()["thumbnail_output_id"] is None
    _generate(client, paths, SLUG, COVER_ONE)
    _put_thumbnail(client, PNG_BYTES)

    record = client.get(f"/api/v1/models/{SLUG}").json()
    assert (record["thumbnail_source"], record["thumbnail_output_id"]) == ("model", None)


def test_deleting_the_covering_output_falls_back_to_the_next(
    client: TestClient, paths: DataPaths
) -> None:
    _create(client)
    first = _generate(client, paths, SLUG, COVER_ONE)
    _generate(client, paths, SLUG, COVER_TWO)
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == COVER_ONE

    assert client.delete(f"/api/v1/outputs/{first}").status_code == 204

    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == COVER_TWO


def test_deleting_the_only_covering_output_leaves_no_thumbnail(
    client: TestClient, paths: DataPaths
) -> None:
    _create(client)
    only = _generate(client, paths, SLUG, COVER_ONE)
    assert _listed(client)[SLUG]["has_thumbnail"] is True

    assert client.delete(f"/api/v1/outputs/{only}").status_code == 204

    assert _listed(client)[SLUG]["has_thumbnail"] is False
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").status_code == 404


def test_an_output_removed_behind_the_stores_back_is_noticed(
    client: TestClient, paths: DataPaths
) -> None:
    """The orphan sweep and a model delete remove outputs without going through the
    store's own `delete`; the outputs directory's mtime is what catches those."""
    _create(client)
    first = _generate(client, paths, SLUG, COVER_ONE)
    _generate(client, paths, SLUG, COVER_TWO)
    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == COVER_ONE

    shutil.rmtree(paths.output_dir(SLUG, first))

    assert client.get(f"/api/v1/models/{SLUG}/thumbnail").content == COVER_TWO


# ── README ────────────────────────────────────────────────────────────────────


def test_a_readme_can_be_set_read_replaced_and_removed(client: TestClient) -> None:
    assert _create(client)["has_readme"] is False
    assert client.get(f"/api/v1/models/{SLUG}/readme").status_code == 404

    set_response = _put_readme(client, "# Widget\n\nPrints flat.\n")
    assert set_response.status_code == 200
    assert set_response.json()["has_readme"] is True
    served = client.get(f"/api/v1/models/{SLUG}/readme")
    assert served.status_code == 200
    assert served.text == "# Widget\n\nPrints flat.\n"
    assert served.headers["content-type"] == "text/markdown; charset=utf-8"

    assert _put_readme(client, "# Widget v2\n").status_code == 200
    assert client.get(f"/api/v1/models/{SLUG}/readme").text == "# Widget v2\n"

    removed = client.delete(f"/api/v1/models/{SLUG}/readme")
    assert removed.status_code == 200
    assert removed.json()["has_readme"] is False
    assert client.get(f"/api/v1/models/{SLUG}/readme").status_code == 404


def test_the_readme_uploaded_at_creation_is_readable(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models",
        files={
            "file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream"),
            "readme": ("README.md", "# Wïdget\n".encode(), "text/markdown"),
        },
    )
    assert response.status_code == 201
    assert client.get(f"/api/v1/models/{SLUG}/readme").text == "# Wïdget\n"


def test_removing_a_readme_that_is_not_there_is_a_404(client: TestClient) -> None:
    _create(client)
    response = client.delete(f"/api/v1/models/{SLUG}/readme")
    assert response.status_code == 404
    assert "no README" in response.json()["detail"]


def test_a_readme_with_a_nul_byte_is_refused(client: TestClient, paths: DataPaths) -> None:
    _create(client)
    response = _put_readme(client, "binary\x00blob")
    assert response.status_code == 422
    assert "NUL" in response.json()["detail"]
    assert not (paths.model_dir(SLUG) / "README.md").exists()


def test_a_readme_body_of_the_wrong_shape_is_refused(client: TestClient) -> None:
    _create(client)
    response = client.put(f"/api/v1/models/{SLUG}/readme", json={"text": "# Widget\n"})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"


def test_a_readme_too_long_to_save_again_is_refused_at_creation(client: TestClient) -> None:
    """The create path holds a README to the cap `PUT /readme` does, so a model
    never starts with one that Edit details could not save back."""
    response = client.post(
        "/api/v1/models",
        files={
            "file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream"),
            "readme": ("README.md", b"x" * (MAX_SOURCE_CHARS + 1), "text/markdown"),
        },
    )

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert "the README is too large" in response.json()["detail"]
    assert client.get(f"/api/v1/models/{SLUG}").status_code == 404


def test_a_readme_at_the_cap_is_accepted_at_creation(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models",
        files={
            "file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream"),
            "readme": ("README.md", b"x" * MAX_SOURCE_CHARS, "text/markdown"),
        },
    )
    assert response.status_code == 201, response.text
    assert _put_readme(client, "x" * MAX_SOURCE_CHARS).status_code == 200


# ── unknown models ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "thumbnail"),
        ("DELETE", "thumbnail"),
        ("GET", "readme"),
        ("PUT", "readme"),
        ("DELETE", "readme"),
    ],
)
def test_every_new_route_answers_404_for_an_unknown_model(
    client: TestClient, paths: DataPaths, method: str, path: str
) -> None:
    kwargs: dict[str, Any] = {}
    if method == "PUT" and path == "thumbnail":
        kwargs["files"] = {"file": ("thumb.png", PNG_BYTES, "image/png")}
    elif method == "PUT":
        kwargs["json"] = {"content": "# Nothing\n"}

    response = client.request(method, f"/api/v1/models/missing/{path}", **kwargs)

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    # Nothing was written on the way to the 404, and no directory was conjured.
    assert not paths.model_dir("missing").exists()


# ── model.json on create ──────────────────────────────────────────────────────


def test_a_dropped_model_directory_lands_with_its_own_metadata(client: TestClient) -> None:
    meta = {
        "name": "Widget Deluxe",
        "description": "A widget.",
        "tags": ["a", "b"],
        "source": "inspired by a widget",
    }
    response = client.post(
        "/api/v1/models",
        files={
            "file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream"),
            "meta": ("model.json", json.dumps(meta).encode(), "application/json"),
            "thumbnail": ("thumbnail.png", PNG_BYTES, "image/png"),
            "readme": ("README.md", b"# Widget\n", "text/markdown"),
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["slug"] == SLUG
    assert {key: body[key] for key in meta} == meta
    assert (body["has_thumbnail"], body["has_readme"]) == (True, True)


@pytest.mark.parametrize(
    "origin_url",
    [
        "javascript:alert(document.domain)",
        "JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "https://example.com/widget.scad",
        "http://10.0.0.1/internal",
    ],
)
def test_a_dropped_model_json_never_sets_origin_url(client: TestClient, origin_url: str) -> None:
    """Only `POST /models/import` sets it; from an uploaded file it would be a stored
    link of the uploader's choosing on every catalogue card."""
    body = _create_with_meta(
        client, {"name": "Widget", "source": "inspired by a widget", "origin_url": origin_url}
    )

    assert body["origin_url"] is None
    # The rest of the file still carries over.
    assert body["source"] == "inspired by a widget"
    assert client.get(f"/api/v1/models/{SLUG}").json()["origin_url"] is None


def test_patch_cannot_set_origin_url(client: TestClient) -> None:
    _create(client)
    response = client.patch(
        f"/api/v1/models/{SLUG}", json={"description": "x", "origin_url": "javascript:alert(1)"}
    )
    assert response.status_code == 200
    assert response.json()["origin_url"] is None


def test_form_fields_win_over_the_model_json(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models",
        files={
            "file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream"),
            "meta": (
                "model.json",
                json.dumps({"name": "From JSON", "tags": ["json"]}).encode(),
                "application/json",
            ),
        },
        data={"name": "From Form"},
    )
    assert response.status_code == 201
    assert (response.json()["name"], response.json()["tags"]) == ("From Form", ["json"])


def _create_with_meta(
    client: TestClient, meta: dict[str, Any], data: dict[str, str] | None = None
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/models",
        files={
            "file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream"),
            "meta": ("model.json", json.dumps(meta).encode(), "application/json"),
        },
        data=data or {},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_model_json_name_falls_back_to_the_slug(client: TestClient, blank: str) -> None:
    assert _create_with_meta(client, {"name": blank})["name"] == SLUG


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_form_name_falls_through_to_the_model_json_name(
    client: TestClient, blank: str
) -> None:
    body = _create_with_meta(client, {"name": "From JSON"}, data={"name": blank})
    assert body["name"] == "From JSON"


def test_a_form_name_wins_over_the_model_json_name_and_is_stored_stripped(
    client: TestClient,
) -> None:
    body = _create_with_meta(client, {"name": "From JSON"}, data={"name": "  From Form  "})
    assert body["name"] == "From Form"


@pytest.mark.parametrize(
    "fields",
    [
        {"description": ""},
        {"tags": ""},
        {"description": "   "},
        {"tags": "   "},
        {"description": " \t ", "tags": " \t "},
    ],
)
def test_a_blank_description_or_tags_field_falls_through_to_the_model_json(
    client: TestClient, fields: dict[str, str]
) -> None:
    """Blank is absent for every detail, as for the name: a form that sends an empty
    field alongside a dropped model.json must not wipe what the file says."""
    body = _create_with_meta(
        client, {"name": "Widget", "description": "From JSON", "tags": ["json"]}, data=fields
    )
    assert (body["description"], body["tags"]) == ("From JSON", ["json"])


def test_a_non_blank_description_and_tags_win_and_the_description_is_kept_as_given(
    client: TestClient,
) -> None:
    body = _create_with_meta(
        client,
        {"name": "Widget", "description": "From JSON", "tags": ["json"]},
        data={"description": "  From form  ", "tags": "a, b"},
    )
    assert (body["description"], body["tags"]) == ("  From form  ", ["a", "b"])


def test_an_explicit_empty_tag_list_still_clears_the_model_json_tags(client: TestClient) -> None:
    body = _create_with_meta(client, {"name": "Widget", "tags": ["json"]}, data={"tags": "[]"})
    assert body["tags"] == []


def test_blank_fields_without_a_model_json_give_the_defaults(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models",
        files={"file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream")},
        data={"description": "   ", "tags": "   "},
    )
    assert response.status_code == 201
    assert (response.json()["description"], response.json()["tags"]) == ("", [])


def test_a_null_in_a_model_json_is_the_field_left_out(client: TestClient) -> None:
    """`null` falls through like a missing or blank value: the name to the slug, the
    rest to their defaults -- not a validation 422."""
    body = _create_with_meta(
        client, {"name": None, "description": None, "tags": None, "libraries": None}
    )
    assert (body["name"], body["description"], body["tags"], body["libraries"]) == (
        SLUG,
        "",
        [],
        [],
    )


def test_a_null_model_json_name_still_yields_to_the_form(client: TestClient) -> None:
    assert _create_with_meta(client, {"name": None}, data={"name": "From Form"})["name"] == (
        "From Form"
    )


def test_a_model_json_name_is_stored_stripped(client: TestClient) -> None:
    assert _create_with_meta(client, {"name": "  Widget  "})["name"] == "Widget"


def test_blank_everywhere_names_the_model_after_its_slug(client: TestClient) -> None:
    assert _create_with_meta(client, {"name": " "}, data={"name": " "})["name"] == SLUG


@pytest.mark.parametrize(
    "payload", [b"{not json", b"[1, 2]", json.dumps({"tags": "not-a-list"}).encode()]
)
def test_a_model_json_that_cannot_be_read_is_refused(client: TestClient, payload: bytes) -> None:
    response = client.post(
        "/api/v1/models",
        files={
            "file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream"),
            "meta": ("model.json", payload, "application/json"),
        },
    )
    assert response.status_code == 422
    assert client.get(f"/api/v1/models/{SLUG}").status_code == 404


# ── PATCH names ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_name_is_refused_and_nothing_is_committed(client: TestClient, blank: str) -> None:
    before = _create(client)

    response = client.patch(f"/api/v1/models/{SLUG}", json={"name": blank})

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    after = client.get(f"/api/v1/models/{SLUG}").json()
    assert after["name"] == before["name"]
    assert after["version"] == before["version"]


def test_a_patched_name_is_stored_stripped(client: TestClient) -> None:
    _create(client)
    response = client.patch(f"/api/v1/models/{SLUG}", json={"name": "  Widget  "})
    assert response.status_code == 200
    assert response.json()["name"] == "Widget"


def test_a_patch_without_a_name_leaves_it_alone(client: TestClient) -> None:
    before = _create(client)
    response = client.patch(f"/api/v1/models/{SLUG}", json={"description": "new"})
    assert response.status_code == 200
    assert response.json()["name"] == before["name"]


# ── hostile JSON ──────────────────────────────────────────────────────────────

#: Parses as a stack overflow, not a decode error: `json.loads` recurses per level.
DEEP = "[" * 100_000 + "]" * 100_000


def _refused_without_a_model(response: httpx.Response, client: TestClient) -> dict[str, Any]:
    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert client.get(f"/api/v1/models/{SLUG}").status_code == 404
    body: dict[str, Any] = response.json()
    return body


def test_a_deeply_nested_model_json_is_a_422_not_a_500(client: TestClient) -> None:
    # Under MAX_META_BYTES, so it is the parse that refuses it and not the cap:
    # 30,000 levels is still far past the recursion limit.
    deep = "[" * 30_000 + "]" * 30_000
    assert len(deep) <= models_api.MAX_META_BYTES
    response = client.post(
        "/api/v1/models",
        files={
            "file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream"),
            "meta": ("model.json", deep.encode(), "application/json"),
        },
    )
    assert _refused_without_a_model(response, client)["detail"] == (
        "the model.json is not valid JSON"
    )


def test_a_model_json_nested_too_deep_to_validate_is_a_422(client: TestClient) -> None:
    """Shallow enough for `json.loads`, deep enough to worry the validator."""
    nested = "[" * 900 + "]" * 900
    response = client.post(
        "/api/v1/models",
        files={
            "file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream"),
            "meta": ("model.json", f'{{"tags": {nested}}}'.encode(), "application/json"),
        },
    )
    _refused_without_a_model(response, client)


def test_deeply_nested_tags_are_a_422_not_a_500(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models",
        files={"file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream")},
        data={"tags": DEEP},
    )
    assert _refused_without_a_model(response, client)["detail"] == "tags is not valid JSON"


def test_a_deeply_nested_json_paste_is_a_422_not_a_500(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models", content=DEEP, headers={"Content-Type": "application/json"}
    )
    assert _refused_without_a_model(response, client)["detail"] == (
        "the request body is not valid JSON"
    )


# ── every change is a revision ────────────────────────────────────────────────


@pytest.mark.requires_git
def test_every_details_change_is_one_revision_in_the_models_history(client: TestClient) -> None:
    created = _create(client)

    steps: list[tuple[httpx.Response, str]] = [
        (_put_thumbnail(client, PNG_BYTES), f"Set {SLUG} thumbnail"),
        (_put_readme(client, "# Widget\n"), f"Set {SLUG} README"),
        (
            client.patch(f"/api/v1/models/{SLUG}", json={"name": "Widget", "tags": ["x"]}),
            f"Update {SLUG} metadata",
        ),
        (client.delete(f"/api/v1/models/{SLUG}/thumbnail"), f"Remove {SLUG} thumbnail"),
        (client.delete(f"/api/v1/models/{SLUG}/readme"), f"Remove {SLUG} README"),
    ]

    listed = client.get(f"/api/v1/models/{SLUG}/versions").json()
    assert [entry["message"] for entry in listed] == [message for _, message in reversed(steps)] + [
        f"Add {SLUG}"
    ]

    version = created["version"]
    for (response, message), entry in zip(steps, reversed(listed[:-1]), strict=True):
        assert response.status_code == 200, message
        # Each answer carries the revision it made.
        assert response.json()["version"] == entry["commit"] != version
        version = entry["commit"]
    # Paths are relative to the model's directory; a removal is a "D".
    by_message = {entry["message"]: entry for entry in listed}
    assert [change["path"] for change in by_message[f"Set {SLUG} thumbnail"]["files"]] == [
        "thumbnail.png"
    ]
    assert [
        (change["path"], change["status"])
        for change in by_message[f"Remove {SLUG} README"]["files"]
    ] == [("README.md", "D")]


def test_the_size_in_a_message_is_derived_from_the_limit() -> None:
    assert (_mib(2 * 1024 * 1024), _mib(3 * 1024 * 1024 // 2), _mib(512 * 1024)) == (
        "2 MiB",
        "1.5 MiB",
        "0.5 MiB",
    )


# ── a model.json on disk, read as permissively as an uploaded one ─────────────


def _place(paths: DataPaths, slug: str, meta: str) -> None:
    """A model directory put on the volume by hand, as the user guide allows."""
    paths.model_dir(slug).mkdir(parents=True)
    paths.model_source(slug).write_text(SOURCE, encoding="utf-8")
    paths.model_meta(slug).write_text(meta, encoding="utf-8")


def test_nulls_in_a_model_json_on_disk_are_the_defaults(
    client: TestClient, paths: DataPaths
) -> None:
    _place(
        paths,
        "placed",
        json.dumps({"name": None, "description": None, "tags": None, "libraries": None}),
    )

    listed = _listed(client)["placed"]
    assert (listed["name"], listed["description"], listed["tags"], listed["libraries"]) == (
        "placed",
        "",
        [],
        [],
    )
    assert client.get("/api/v1/models/placed").json()["name"] == "placed"


@pytest.mark.parametrize(
    ("meta", "named"), [('{"name": "Bad", "tags": 5}', "tags"), ("{not json", "not JSON")]
)
def test_an_invalid_model_json_on_disk_costs_only_its_own_model(
    client: TestClient, paths: DataPaths, meta: str, named: str
) -> None:
    _create(client)
    _place(paths, "broken", meta)

    listing = client.get("/api/v1/models")
    assert listing.status_code == 200
    slugs = {model["slug"] for model in listing.json()}
    assert SLUG in slugs
    assert "broken" not in slugs

    response = client.get("/api/v1/models/broken")
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["title"] == "Invalid Model Metadata"
    assert "model.json of 'broken' is not valid" in body["detail"]
    assert named in body["detail"]


# ── the model.json cap ────────────────────────────────────────────────────────


def _meta_of_size(size: int) -> bytes:
    """A valid model.json of exactly ``size`` bytes: JSON allows trailing whitespace."""
    body = json.dumps({"name": "Widget"}).encode()
    return body + b" " * (size - len(body))


def _upload_meta(client: TestClient, meta: bytes) -> httpx.Response:
    response: httpx.Response = client.post(
        "/api/v1/models",
        files={
            "file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream"),
            "meta": ("model.json", meta, "application/json"),
        },
    )
    return response


def test_the_model_json_cap_is_sized_for_a_real_one() -> None:
    assert models_api.MAX_META_BYTES == 64 * 1024
    assert models_api.MAX_META_SIZE == "64 KiB"


def test_a_model_json_at_the_cap_is_accepted(client: TestClient) -> None:
    response = _upload_meta(client, _meta_of_size(models_api.MAX_META_BYTES))
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "Widget"


def test_a_model_json_one_byte_over_the_cap_is_a_422_naming_it(client: TestClient) -> None:
    size = models_api.MAX_META_BYTES + 1
    response = _upload_meta(client, _meta_of_size(size))
    assert _refused_without_a_model(response, client)["detail"] == (
        f"the model.json is too large: {size} bytes, "
        "and a model.json is at most 65536 bytes (64 KiB)"
    )


def test_an_oversized_model_json_is_refused_before_it_is_parsed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    parsed: list[bytes] = []
    real = models_api._read_meta_file

    def recording(payload: bytes, slug: str) -> Any:
        parsed.append(payload)
        return real(payload, slug)

    monkeypatch.setattr(models_api, "_read_meta_file", recording)

    response = _upload_meta(client, b"{" * (models_api.MAX_META_BYTES + 1))

    _refused_without_a_model(response, client)
    assert parsed == []


def test_a_model_json_is_parsed_off_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[threading.Thread] = []
    real = models_api._read_meta_file

    def recording(payload: bytes, slug: str) -> Any:
        threads.append(threading.current_thread())
        return real(payload, slug)

    monkeypatch.setattr(models_api, "_read_meta_file", recording)

    response = _upload_meta(client, _meta_of_size(100))

    assert response.status_code == 201, response.text
    # The TestClient's loop runs in its portal thread; `to_thread` is a pool worker.
    assert len(threads) == 1
    assert threads[0].name.startswith("asyncio_")
