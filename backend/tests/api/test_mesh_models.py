"""Mesh models through the API (#2146, spec 2026-10-11 §4-§6): create from a library
file, an upload or an output; read the scene; edit and eject, each one revision."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
import trimesh
from fastapi.testclient import TestClient

from scadbuddy.bambuddy import print_source
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.presets import ParamPresetCreate
from scadbuddy.meshmodel import ingest
from scadbuddy.meshmodel import operations as mesh_operations
from scadbuddy.meshmodel.scene import load_scene, text_sha256
from scadbuddy.render.bambu3mf import stl_3mf
from scadbuddy.render.read_budget import ReadBudget
from tests.api.test_print_library import library_file
from tests.api.test_send import configure
from tests.support.operations import press

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]


def stl(mesh: trimesh.Trimesh) -> bytes:
    data = mesh.export(file_type="stl")
    assert isinstance(data, bytes)
    return data


def carrot() -> trimesh.Trimesh:
    from tests.meshmodel.test_section import neck

    return neck()


def upload(
    client: TestClient,
    name: str = "Carrot",
    data: bytes | None = None,
    filename: str = "carrot.stl",
    key: dict[str, str] | None = None,
) -> httpx.Response:
    response: httpx.Response = client.post(
        "/api/v1/models/from-mesh/upload",
        files={
            "file": (
                filename,
                data if data is not None else stl(carrot()),
                "application/octet-stream",
            )
        },
        data={"name": name},
        headers=key or press(),
    )
    return response


def edit(
    client: TestClient,
    slug: str,
    *edits: dict[str, Any],
    drop: bool = False,
    key: dict[str, str] | None = None,
) -> httpx.Response:
    response: httpx.Response = client.post(
        f"/api/v1/models/{slug}/scene/edits",
        json={"edits": list(edits), "drop_from_presets": drop},
        headers=key or press(),
    )
    return response


def save_tall(client: TestClient, slug: str) -> None:
    """A saved preset setting ``height_scale``, written to the store: the route checks
    values against the schema openscad exports, and the fake one exports only ``width``
    and ``label`` (``tests/conftest.py`` ``FAKE_OPENSCAD``)."""
    presets = client.app.state.scadbuddy.presets  # type: ignore[attr-defined]
    presets.create(slug, ParamPresetCreate(name="Tall", params={"height_scale": 2}))


def test_an_uploaded_stl_opens_as_a_scene_model(client: TestClient, paths: DataPaths) -> None:
    created = upload(client)
    assert created.status_code == 201, created.text
    slug = created.json()["slug"]
    directory = paths.model_dir(slug)
    scene = load_scene((directory / "scene.json").read_text())
    assert [o.id for o in scene.objects] == ["o1"]
    assert (directory / "meshes" / "o1-p1.3mf").is_file()
    scad = (directory / "model.scad").read_text()
    assert scene.generated_sha256 == text_sha256(scad)
    assert client.get(f"/api/v1/models/{slug}").json()["version"]


def test_a_library_file_opens_as_a_scene_model_and_bambuddy_is_not_written(
    client: TestClient,
) -> None:
    configure(client)
    with respx.mock:
        library_file(688, content=stl_3mf(stl(carrot()), model_name="carrot"), file_type="3mf")
        created = client.post(
            "/api/v1/models/from-mesh",
            json={"name": "Carrot Garden", "library_file_id": 688},
            headers=press(),
        )
        writes = [c for c in respx.calls if c.request.method != "GET"]
    assert created.status_code == 201, created.text
    assert writes == []
    scene = client.get(f"/api/v1/models/{created.json()['slug']}/scene").json()
    assert scene["scene"]["source"]["kind"] == "bambuddy_library"
    assert scene["scene"]["source"]["file_id"] == 688


def test_a_sliced_library_file_is_refused(client: TestClient) -> None:
    configure(client)
    with respx.mock:
        respx.get(url__regex=r".*/library/files/77$").mock(
            return_value=httpx.Response(
                200, json={"id": 77, "filename": "x.gcode.3mf", "file_type": "gcode.3mf"}
            )
        )
        refused = client.post(
            "/api/v1/models/from-mesh", json={"name": "X", "library_file_id": 77}, headers=press()
        )
    assert refused.status_code == 422, refused.text


def test_a_create_names_exactly_one_source(client: TestClient) -> None:
    both = client.post(
        "/api/v1/models/from-mesh",
        json={"name": "X", "library_file_id": 1, "output_id": "abc"},
        headers=press(),
    )
    assert both.status_code == 422


def test_an_unreadable_upload_is_refused(client: TestClient) -> None:
    refused = upload(client, data=b"not a mesh", filename="x.3mf")
    assert refused.status_code == 422, refused.text


def test_an_unreadable_stl_upload_is_refused(client: TestClient) -> None:
    refused = upload(client, data=b"not a mesh", filename="x.stl")
    assert refused.status_code == 422, refused.text


def test_an_obj_upload_is_refused(client: TestClient) -> None:
    refused = upload(client, data=b"v 0 0 0\n", filename="x.obj")
    assert refused.status_code == 415, refused.text


def test_a_create_writes_only_scene_files(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file the scene would write outside ``meshes/``, ``scene.json`` and
    ``model.scad`` is refused as a 422, and nothing is created."""
    real = ingest.scene_from_objects

    def stray(*args: Any, **kwargs: Any) -> Any:
        scene, files = real(*args, **kwargs)
        return scene, {**files, "model.json": b"{}"}

    monkeypatch.setattr(mesh_operations, "scene_from_objects", stray)
    refused = upload(client, name="Stray")
    assert refused.status_code == 422, refused.text
    assert "model.json" in refused.json()["detail"]
    assert not paths.model_dir("stray").exists()


def test_split_with_a_joint_is_one_revision(client: TestClient, paths: DataPaths) -> None:
    slug = upload(client).json()["slug"]
    before = len(client.get(f"/api/v1/models/{slug}/versions").json())
    done = edit(
        client,
        slug,
        {
            "edit": "split_at_plane",
            "object": "o1",
            "point": [0, 0, 32],
            "normal": [0, 0, 1],
            "joint": {"depth": 5},
        },
    )
    assert done.status_code == 200, done.text
    assert len(client.get(f"/api/v1/models/{slug}/versions").json()) == before + 1
    scene = load_scene((paths.model_dir(slug) / "scene.json").read_text())
    assert [o.id for o in scene.objects] == ["o1", "o2"]
    assert scene.generated_sha256 == text_sha256((paths.model_dir(slug) / "model.scad").read_text())


def test_a_repeated_edit_is_one_revision(client: TestClient, paths: DataPaths) -> None:
    """A split run twice would split o2 off again; the replay answers the first run."""
    slug = upload(client).json()["slug"]
    before = len(client.get(f"/api/v1/models/{slug}/versions").json())
    key = press()
    split = {
        "edit": "split_at_plane",
        "object": "o1",
        "point": [0, 0, 32],
        "normal": [0, 0, 1],
        "joint": {"depth": 5},
    }
    first = edit(client, slug, split, key=key)
    again = edit(client, slug, split, key=key)
    assert first.status_code == again.status_code == 200, again.text
    assert again.json() == first.json()
    assert len(client.get(f"/api/v1/models/{slug}/versions").json()) == before + 1
    scene = load_scene((paths.model_dir(slug) / "scene.json").read_text())
    assert [o.id for o in scene.objects] == ["o1", "o2"]


def test_a_repeated_upload_is_one_model(client: TestClient, paths: DataPaths) -> None:
    key = press()
    first = upload(client, key=key)
    again = upload(client, key=key)
    assert first.status_code == again.status_code == 201, again.text
    assert again.json()["slug"] == first.json()["slug"] == "carrot"
    assert [m["slug"] for m in client.get("/api/v1/models").json() if m["origin"] == "mine"] == [
        "carrot"
    ]
    assert not (paths.model_dir("carrot-2")).exists()


def tight_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A triangle budget the carrot's STL is past."""
    monkeypatch.setattr(mesh_operations, "ReadBudget", lambda: ReadBudget(max_triangles=10))


def test_an_stl_upload_past_the_triangle_budget_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tight_budget(monkeypatch)
    refused = upload(client)
    assert refused.status_code == 422, refused.text
    assert "triangles" in refused.json()["detail"]


def test_a_library_stl_past_the_triangle_budget_is_refused_before_it_loads(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(client)
    tight_budget(monkeypatch)

    def never(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("an STL past the budget is never loaded")

    monkeypatch.setattr(mesh_operations, "stl_3mf", never)
    monkeypatch.setattr(print_source, "stl_3mf", never)
    with respx.mock:
        library_file(689, content=stl(carrot()), file_type="stl")
        refused = client.post(
            "/api/v1/models/from-mesh",
            json={"name": "Big Carrot", "library_file_id": 689},
            headers=press(),
        )
    assert refused.status_code == 422, refused.text
    assert "triangles" in refused.json()["detail"]


def test_a_refused_edit_is_a_422_naming_why(client: TestClient) -> None:
    slug = upload(client).json()["slug"]
    refused = edit(
        client,
        slug,
        {"edit": "split_at_plane", "object": "o1", "point": [0, 0, 500], "normal": [0, 0, 1]},
    )
    assert refused.status_code == 422
    assert "does not cut o1" in refused.json()["detail"]


def test_a_hand_edited_model_refuses_scene_edits(client: TestClient) -> None:
    slug = upload(client).json()["slug"]
    put = client.put(
        f"/api/v1/models/{slug}/source",
        json={"source": "cube(1);\n", "force": True},
        headers=press(),
    )
    assert put.status_code == 200, put.text
    refused = edit(client, slug, {"edit": "rename", "object": "o1", "name": "x"})
    assert refused.status_code == 409
    assert "eject" in refused.json()["detail"]
    assert client.get(f"/api/v1/models/{slug}/scene").json()["hand_edited"] is True


def test_removing_a_parameter_a_saved_preset_names_is_refused_unless_dropped(
    client: TestClient,
) -> None:
    slug = upload(client).json()["slug"]
    exposed = edit(
        client,
        slug,
        {"edit": "expose", "object": "o1", "op": None, "field": "scale.2", "name": "height_scale"},
    )
    assert exposed.status_code == 200, exposed.text
    save_tall(client, slug)
    unexpose = {"edit": "unexpose", "object": "o1", "op": None, "field": "scale.2"}
    refused = edit(client, slug, unexpose)
    assert refused.status_code == 409 and "Tall" in refused.json()["detail"]
    assert edit(client, slug, unexpose, drop=True).status_code == 200
    presets = client.get(f"/api/v1/models/{slug}/presets").json()
    assert "height_scale" not in next(p for p in presets if p["name"] == "Tall")["params"]


def test_a_preset_left_naming_a_removed_parameter_is_trimmed_by_the_next_edit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The revision commits before the presets are trimmed; if that trim fails, the next
    edit (which removes nothing) still mends the preset (review of #2388)."""
    slug = upload(client).json()["slug"]
    exposed = edit(
        client,
        slug,
        {"edit": "expose", "object": "o1", "op": None, "field": "scale.2", "name": "height_scale"},
    )
    assert exposed.status_code == 200, exposed.text
    save_tall(client, slug)
    presets = client.app.state.scadbuddy.presets  # type: ignore[attr-defined]
    real_update = presets.update

    def fails(*args: object, **kwargs: object) -> object:
        raise RuntimeError("Postgres went away")

    monkeypatch.setattr(presets, "update", fails)
    unexpose = {"edit": "unexpose", "object": "o1", "op": None, "field": "scale.2"}
    assert edit(client, slug, unexpose, drop=True).status_code >= 500
    # The revision committed: the scale is a plain number again.
    scene = client.get(f"/api/v1/models/{slug}/scene").json()["scene"]
    assert scene["objects"][0]["transform"]["scale"][2] == 1
    monkeypatch.setattr(presets, "update", real_update)
    assert (
        edit(client, slug, {"edit": "rename", "object": "o1", "name": "Again"}).status_code == 200
    )
    tall = next(
        p for p in client.get(f"/api/v1/models/{slug}/presets").json() if p["name"] == "Tall"
    )
    assert "height_scale" not in tall["params"]


def test_eject_leaves_a_plain_template_that_keeps_its_provenance(
    client: TestClient, paths: DataPaths
) -> None:
    slug = upload(client).json()["slug"]
    ejected = client.post(f"/api/v1/models/{slug}/scene/eject", headers=press())
    assert ejected.status_code == 200, ejected.text
    assert not (paths.model_dir(slug) / "scene.json").exists()
    assert (paths.model_dir(slug) / "meshes" / "o1-p1.3mf").is_file()
    assert client.get(f"/api/v1/models/{slug}/scene").status_code == 404
    assert client.get(f"/api/v1/models/{slug}").json()["mesh_source"]["kind"] == "upload"


def test_a_details_patch_keeps_the_mesh_source(client: TestClient, paths: DataPaths) -> None:
    slug = upload(client).json()["slug"]
    patched = client.patch(
        f"/api/v1/models/{slug}",
        json={"name": "Carrot Two", "description": "orange", "tags": ["veg"]},
        headers=press(),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["mesh_source"]["kind"] == "upload"
    raw = json.loads((paths.model_dir(slug) / "model.json").read_text())
    assert raw["mesh_source"]["kind"] == "upload"
    assert raw["description"] == "orange"


def test_an_output_opens_as_a_scene_model(client: TestClient, paths: DataPaths) -> None:
    output_id = "ab" * 16
    directory = paths.outputs / "somewhere" / output_id
    directory.mkdir(parents=True)
    # Only the 3MF is read; the record just has to be there for the output to be found.
    (directory / "meta.json").write_text("{}", encoding="utf-8")
    (directory / "model.3mf").write_bytes(stl_3mf(stl(carrot()), model_name="carrot"))
    created = client.post(
        "/api/v1/models/from-mesh",
        json={"name": "From Output", "output_id": output_id},
        headers=press(),
    )
    assert created.status_code == 201, created.text
    source = client.get(f"/api/v1/models/{created.json()['slug']}/scene").json()["scene"]["source"]
    assert source["kind"] == "output"
    assert source["output_id"] == output_id


def test_an_output_with_no_3mf_is_a_404(client: TestClient) -> None:
    missing = client.post(
        "/api/v1/models/from-mesh",
        json={"name": "Nothing", "output_id": "cd" * 16},
        headers=press(),
    )
    assert missing.status_code == 404, missing.text
