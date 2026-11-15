"""A native library file's associations (#6395): the routes, both directions, the
implied ones, and what a trash, a restore, a purge and a model's delete do to them."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.print_library.associations import RELATIONS
from scadbuddy.print_library.store import NativeFile
from scadbuddy.render.glb import BoundingBox
from tests.api.test_library_native import _native, _three_mf, _upload, native_client
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

__all__ = ["native_client"]


@pytest.fixture
def files(native_client: TestClient, tmp_path: Path) -> tuple[NativeFile, NativeFile]:
    return (
        _upload(native_client, _three_mf(tmp_path / "fixed.3mf")),
        _upload(native_client, _three_mf(tmp_path / "petg.3mf")),
    )


def _associate(client: TestClient, file_id: int, **body: Any) -> Any:
    return client.post(f"/api/v1/print/library/{file_id}/associations", json=body)


def _listed(client: TestClient, path: str) -> list[dict[str, Any]]:
    response = client.get(path)
    assert response.status_code == 200, response.text
    found: list[dict[str, Any]] = response.json()["associations"]
    return found


def test_relations_are_one_list() -> None:
    assert RELATIONS == ("output_of", "variant_of", "source_of", "related_to")


def test_a_file_is_associated_with_a_model(
    native_client: TestClient, files: tuple[NativeFile, NativeFile], model: str
) -> None:
    fixed, _ = files
    made = _associate(
        native_client, fixed.id, target=f"model:{model}", relation="output_of", note="scaled"
    )
    assert made.status_code == 201, made.text
    body = made.json()
    assert body | {"id": None, "created_at": None} == {
        "id": None,
        "library_file_id": fixed.id,
        "library_file_name": "fixed.3mf",
        "relation": "output_of",
        "target": f"model:{model}",
        "target_name": "Demo",
        "note": "scaled",
        "created_at": None,
        "created_by": {"kind": "browser", "principal": None, "session": None, "run_id": None},
        "implied_by": None,
    }

    assert [a["id"] for a in _listed(native_client, f"/api/v1/models/{model}/associations")] == [
        body["id"]
    ]
    assert [
        a["id"] for a in _listed(native_client, f"/api/v1/print/library/{fixed.id}/associations")
    ] == [body["id"]]


def test_the_same_association_again_keeps_its_id_and_takes_the_note(
    native_client: TestClient, files: tuple[NativeFile, NativeFile], model: str
) -> None:
    fixed, _ = files
    first = _associate(native_client, fixed.id, target=f"model:{model}", relation="related_to")
    again = _associate(
        native_client, fixed.id, target=f"model:{model}", relation="related_to", note="instructions"
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["note"] == "instructions"
    other = _associate(native_client, fixed.id, target=f"model:{model}", relation="variant_of")
    assert other.status_code == 201


def test_a_file_to_file_association_is_listed_both_ways(
    native_client: TestClient, files: tuple[NativeFile, NativeFile]
) -> None:
    fixed, petg = files
    made = _associate(native_client, petg.id, target=f"library:{fixed.id}", relation="variant_of")
    assert made.status_code == 201, made.text
    assert made.json()["target_name"] == "fixed.3mf"

    [mine] = _listed(native_client, f"/api/v1/print/library/{petg.id}/associations")
    [theirs] = _listed(native_client, f"/api/v1/print/library/{fixed.id}/associations")
    assert mine == theirs == made.json()


@pytest.mark.parametrize(
    ("target", "relation"),
    [
        ("model:no-such-model", "related_to"),
        ("library:999", "related_to"),
        ("library:1999999999", "related_to"),
        ("output:abc", "related_to"),
        ("nonsense", "related_to"),
        ("model:demo", "copy_of"),
    ],
)
def test_a_target_that_is_not_there_is_refused(
    native_client: TestClient,
    files: tuple[NativeFile, NativeFile],
    model: str,
    target: str,
    relation: str,
) -> None:
    fixed, _ = files
    response = _associate(native_client, fixed.id, target=target, relation=relation)
    assert response.status_code == 422, response.text


def test_a_file_id_is_written_one_way(
    native_client: TestClient, files: tuple[NativeFile, NativeFile]
) -> None:
    fixed, petg = files
    for spelling in (f"0{fixed.id}", "".join(chr(0x0660 + int(d)) for d in str(fixed.id))):
        response = _associate(
            native_client, petg.id, target=f"library:{spelling}", relation="variant_of"
        )
        assert response.status_code == 422, response.text


def test_a_file_is_not_associated_with_itself(
    native_client: TestClient, files: tuple[NativeFile, NativeFile]
) -> None:
    fixed, _ = files
    response = _associate(
        native_client, fixed.id, target=f"library:{fixed.id}", relation="related_to"
    )
    assert response.status_code == 422, response.text


def test_a_file_the_library_does_not_have_is_a_404(native_client: TestClient, model: str) -> None:
    response = _associate(
        native_client, 1_999_999_999, target=f"model:{model}", relation="related_to"
    )
    assert response.status_code == 404, response.text
    assert native_client.get("/api/v1/print/library/1999999999/associations").status_code == 404
    assert native_client.get("/api/v1/models/no-such-model/associations").status_code == 404


def test_remove(
    native_client: TestClient, files: tuple[NativeFile, NativeFile], model: str
) -> None:
    fixed, _ = files
    made = _associate(native_client, fixed.id, target=f"model:{model}", relation="related_to")
    path = f"/api/v1/print/library/associations/{made.json()['id']}"
    assert native_client.delete(path).status_code == 204
    assert native_client.delete(path).status_code == 404
    assert _listed(native_client, f"/api/v1/models/{model}/associations") == []


def test_a_trashed_file_hides_its_associations_and_a_restore_shows_them(
    native_client: TestClient, files: tuple[NativeFile, NativeFile], model: str
) -> None:
    fixed, petg = files
    to_model = _associate(native_client, fixed.id, target=f"model:{model}", relation="output_of")
    _associate(native_client, petg.id, target=f"library:{fixed.id}", relation="variant_of")

    trashed = native_client.post(
        "/api/v1/print/library/delete", json={"file_ids": [fixed.id]}, headers=press()
    )
    assert trashed.status_code == 200, trashed.text
    assert _listed(native_client, f"/api/v1/models/{model}/associations") == []
    assert _listed(native_client, f"/api/v1/print/library/{petg.id}/associations") == []
    assert native_client.get(f"/api/v1/print/library/{fixed.id}/associations").status_code == 404
    gone = native_client.delete(f"/api/v1/print/library/associations/{to_model.json()['id']}")
    assert gone.status_code == 404
    refused = _associate(
        native_client, petg.id, target=f"library:{fixed.id}", relation="related_to"
    )
    assert refused.status_code == 422, refused.text

    restored = native_client.post(
        "/api/v1/print/library/restore", json={"file_ids": [fixed.id]}, headers=press()
    )
    assert restored.status_code == 200, restored.text
    assert len(_listed(native_client, f"/api/v1/print/library/{fixed.id}/associations")) == 2
    assert len(_listed(native_client, f"/api/v1/models/{model}/associations")) == 1


def test_a_purged_file_drops_its_associations(
    native_client: TestClient, files: tuple[NativeFile, NativeFile]
) -> None:
    fixed, petg = files
    _associate(native_client, petg.id, target=f"library:{fixed.id}", relation="variant_of")
    native = _native(native_client)
    native.rows.trash([fixed.id])
    assert native.rows.purge_trashed(fixed.id, datetime.now(UTC) + timedelta(days=1)) is not None
    native.rows.restore([petg.id])
    with native.rows._pool.connection() as conn:
        left = conn.execute("SELECT count(*) AS n FROM library_file_associations").fetchone()
    assert left is not None and left["n"] == 0


def test_a_deleted_model_drops_its_associations(
    native_client: TestClient, files: tuple[NativeFile, NativeFile], model: str, paths: DataPaths
) -> None:
    fixed, _ = files
    _associate(native_client, fixed.id, target=f"model:{model}", relation="output_of")
    deleted = native_client.delete(f"/api/v1/models/{model}", headers=press())
    assert deleted.status_code == 204, deleted.text
    assert _listed(native_client, f"/api/v1/print/library/{fixed.id}/associations") == []
    # A model made again under the slug starts with none.
    paths.model_dir(model).mkdir(parents=True, exist_ok=True)
    paths.model_source(model).write_text("cube(1);\n", encoding="utf-8")
    assert _listed(native_client, f"/api/v1/models/{model}/associations") == []


def test_a_model_made_from_a_file_lists_it_as_its_source(
    native_client: TestClient, files: tuple[NativeFile, NativeFile], model: str, paths: DataPaths
) -> None:
    fixed, _ = files
    meta = paths.model_meta(model)
    raw = json.loads(meta.read_text(encoding="utf-8"))
    raw["mesh_source"] = {
        "kind": "bambuddy_library",
        "name": "fixed.3mf",
        "sha256": "0" * 64,
        "file_id": fixed.id,
    }
    meta.write_text(json.dumps(raw), encoding="utf-8")
    implied = {
        "id": None,
        "library_file_id": fixed.id,
        "library_file_name": "fixed.3mf",
        "relation": "source_of",
        "target": f"model:{model}",
        "target_name": "Demo",
        "note": "",
        "created_at": None,
        "created_by": None,
        "implied_by": "mesh_source",
    }
    assert _listed(native_client, f"/api/v1/models/{model}/associations") == [implied]
    assert _listed(native_client, f"/api/v1/print/library/{fixed.id}/associations") == [implied]

    # Said again as a row, it is listed once, as the row.
    made = _associate(native_client, fixed.id, target=f"model:{model}", relation="source_of")
    assert _listed(native_client, f"/api/v1/models/{model}/associations") == [made.json()]


def test_an_outputs_copy_is_an_output_of_its_model(
    native_client: TestClient, model: str, paths: DataPaths, tmp_path: Path
) -> None:
    output_id = "0123456789abcdef0123456789abcdef"
    directory = paths.outputs / model / output_id
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text(
        OutputMeta(
            id=output_id,
            slug=model,
            job_id="j",
            created_at=datetime.now(UTC),
            bbox_mm=BoundingBox(min=(0, 0, 0), max=(10, 10, 4), size=(10, 10, 4)),
        ).model_dump_json(),
        encoding="utf-8",
    )
    copy = _upload(native_client, _three_mf(tmp_path / "bracket.3mf"), output_id=output_id)
    [on_model] = _listed(native_client, f"/api/v1/models/{model}/associations")
    [on_file] = _listed(native_client, f"/api/v1/print/library/{copy.id}/associations")
    assert on_model == on_file
    assert on_model["relation"] == "output_of"
    assert on_model["implied_by"] == "output"
    assert on_model["library_file_id"] == copy.id


def test_on_bambuddys_library_reads_list_none_and_writes_are_refused(
    client: TestClient, model: str
) -> None:
    assert _listed(client, f"/api/v1/models/{model}/associations") == []
    response = _associate(client, 5, target=f"model:{model}", relation="related_to")
    assert response.status_code == 409, response.text
