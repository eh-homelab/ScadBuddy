"""A model's media writes as ``library`` operations (#1054, spec 2026-10-01 §4.3)."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.operations.claims import ClaimStore, Held
from tests.api.conftest import PNG_BYTES
from tests.api.test_media import WEBM
from tests.api.test_model_operations import _commits, _history_bytes, _state, _workflow_ids

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]

PNG = PNG_BYTES + b"\x00"


def _upload(client: TestClient, slug: str, payload: bytes, key: str | None = None) -> Any:
    return client.post(
        f"/api/v1/models/{slug}/media",
        files={"file": ("f.png", payload, "image/png")},
        data={"caption": "c"},
        headers={"Idempotency-Key": key} if key else None,
    )


def _leftovers(app: FastAPI) -> list[str]:
    cache = _state(app).paths.cache
    return [path.name for path in cache.glob("media-upload-*")]


def test_a_repeated_media_upload_makes_one_item(
    client: TestClient, app: FastAPI, model: str
) -> None:
    """Review focus 1: a re-send with the same key streams the body again, and leaves
    nothing of it behind."""
    key = uuid.uuid4().hex
    first = _upload(client, model, PNG, key)
    assert first.status_code == 200, first.text
    commits = _commits(app)
    again = _upload(client, model, PNG, key)
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert len(again.json()["media"]) == 1
    assert _commits(app) == commits
    assert len(_workflow_ids(app, "model_media_upload")) == 1
    assert _leftovers(app) == []


def test_media_edits_are_operations(client: TestClient, app: FastAPI, model: str) -> None:
    first = _upload(client, model, PNG).json()["media"][0]["id"]
    second = _upload(client, model, PNG_BYTES + b"\x01").json()["media"][1]["id"]
    captioned = client.patch(f"/api/v1/models/{model}/media/{first}", json={"caption": "x"})
    assert captioned.status_code == 200, captioned.text
    assert captioned.json()["media"][0]["caption"] == "x"
    ordered = client.put(f"/api/v1/models/{model}/media/order", json={"ids": [second, first]})
    assert ordered.status_code == 200, ordered.text
    assert [item["id"] for item in ordered.json()["media"]] == [second, first]
    covered = client.put(f"/api/v1/models/{model}/media/cover", json={"id": first})
    assert covered.status_code == 200, covered.text
    assert covered.json()["media"][0]["id"] == first
    deleted = client.delete(f"/api/v1/models/{model}/media/{second}")
    assert deleted.status_code == 200, deleted.text
    assert [item["id"] for item in deleted.json()["media"]] == [first]
    for kind in (
        "model_media_patch",
        "model_media_order",
        "model_media_cover",
        "model_media_delete",
    ):
        assert _workflow_ids(app, kind), kind


def test_a_media_upload_goes_by_claim(client: TestClient, app: FastAPI, model: str) -> None:
    image = PNG + b"\x00" * 2_000_000
    response = _upload(client, model, image)
    assert response.status_code == 200, response.text
    item = response.json()["media"][0]
    assert client.get(f"/api/v1/models/{model}/media/{item['id']}").content == image
    (workflow_id,) = _workflow_ids(app, "model_media_upload")
    assert _history_bytes(app, workflow_id) < 100_000
    assert _leftovers(app) == []


def test_too_many_items_is_still_409(
    client: TestClient, app: FastAPI, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scadbuddy.library.media_operations.MAX_MEDIA_ITEMS", 1)
    assert _upload(client, model, PNG).status_code == 200
    refused = _upload(client, model, PNG_BYTES + b"\x01")
    assert refused.status_code == 409, refused.text
    assert "at most" in refused.json()["detail"]
    # Refused by the check: nothing recorded, nothing left on the volume.
    assert len(_workflow_ids(app, "model_media_upload")) == 1
    assert _leftovers(app) == []


def test_a_keyed_media_edit_resent_after_the_model_went_answers_as_first(
    client: TestClient, model: str
) -> None:
    """Review 3e final M1: the route makes no refusal of its own, so a re-send gets its
    recorded answer, not a 404 made since."""
    item = _upload(client, model, PNG).json()["media"][0]["id"]
    headers = {"Idempotency-Key": uuid.uuid4().hex}
    url = f"/api/v1/models/{model}/media/{item}"
    first = client.patch(url, json={"caption": "x"}, headers=headers)
    assert first.status_code == 200, first.text
    assert client.delete(f"/api/v1/models/{model}").status_code in (200, 204)
    again = client.patch(url, json={"caption": "x"}, headers=headers)
    assert again.status_code == 200, again.text
    assert again.json() == first.json()


def test_a_poster_that_cannot_be_held_releases_the_files_claim(
    client: TestClient, app: FastAPI, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 3e final M6: a failed second hold leaves no claim of the first."""
    hold_file = ClaimStore.hold_file
    holds: list[str] = []

    def failing(store: ClaimStore, path: Path, digest: str) -> Held:
        holds.append(digest)
        if len(holds) == 2:
            path.unlink()
            raise OSError("disk full")
        return hold_file(store, path, digest)

    monkeypatch.setattr(ClaimStore, "hold_file", failing)
    with pytest.raises(OSError, match="disk full"):
        client.post(
            f"/api/v1/models/{model}/media",
            files={"file": ("v.webm", WEBM, "video/webm"), "poster": ("p.png", PNG, "image/png")},
        )
    assert holds[0] == hashlib.sha256(WEBM).hexdigest()
    assert not (_state(app).paths.claims / holds[0]).exists()
