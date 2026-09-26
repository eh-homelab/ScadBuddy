"""#179 -- a model's thumbnail, README and metadata, set after it was created."""

from __future__ import annotations

import itertools
import json
import shutil
import zipfile
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

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
    assert served.headers["content-type"].startswith("text/markdown")

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
