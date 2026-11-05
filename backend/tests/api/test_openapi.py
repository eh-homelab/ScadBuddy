from __future__ import annotations

import importlib
import json
import pkgutil
import re
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

import scadbuddy.api
from scadbuddy.main import API_PREFIX, ROOT_ROUTE_MODULES, _name_in_openapi
from scadbuddy.tools.export_openapi import export


def test_the_spec_publishes_every_http_route_and_nothing_else(tmp_path: Path) -> None:
    """Shape, not a list (#508): a new route module is mounted by discovery
    (`test_routes.py`), so a path list here was one more tail every feature appended to."""
    schema = json.loads(export(tmp_path / "openapi.json").read_text(encoding="utf-8"))
    paths = set(schema["paths"])
    assert {"/healthz", f"{API_PREFIX}/models"} <= paths
    assert all(path == "/healthz" or path.startswith(f"{API_PREFIX}/") for path in paths)
    routes = set()
    for info in pkgutil.iter_modules(scadbuddy.api.__path__):
        router = getattr(importlib.import_module(f"scadbuddy.api.{info.name}"), "router", None)
        if isinstance(router, APIRouter):
            prefix = "" if info.name in ROOT_ROUTE_MODULES else API_PREFIX
            routes |= {
                prefix + re.sub(r":[^}]+}", "}", route.path)
                for route in router.routes
                if isinstance(route, APIRoute) and route.include_in_schema
            }
    assert paths == routes
    operation_ids = [op["operationId"] for item in schema["paths"].values() for op in item.values()]
    assert len(operation_ids) == len(set(operation_ids))


def test_the_export_is_deterministic(tmp_path: Path) -> None:
    """The spec is generated, not committed (#492), so the frontend and agent clients and
    CI's API diff all depend on two exports of one tree being byte-identical."""
    first = export(tmp_path / "a.json").read_bytes()
    assert export(tmp_path / "b.json").read_bytes() == first


def test_the_pasted_source_body_is_a_named_schema(tmp_path: Path) -> None:
    """Named, so the frontend's `PastedSource` is generated rather than hand-written."""
    schema = json.loads(export(tmp_path / "openapi.json").read_text(encoding="utf-8"))
    body = schema["paths"]["/api/v1/models"]["post"]["requestBody"]["content"]
    assert body["application/json"]["schema"] == {"$ref": "#/components/schemas/PastedSource"}
    assert schema["components"]["schemas"]["PastedSource"]["required"] == ["name", "source"]


def test_the_new_model_file_routes_document_only_what_they_answer(tmp_path: Path) -> None:
    """#179: a response class with a media type of its own adds it to the documented
    200 beside the declared one, so a route could claim a type it never sends."""
    paths = json.loads(export(tmp_path / "openapi.json").read_text(encoding="utf-8"))["paths"]

    def success_types(path: str, method: str) -> set[str]:
        return set(paths[path][method]["responses"]["200"]["content"])

    assert success_types("/api/v1/models/{slug}/readme", "get") == {"text/markdown"}
    # The cover is whatever image the template's media holds first (#274).
    assert success_types("/api/v1/models/{slug}/thumbnail", "get") == {
        "image/png",
        "image/jpeg",
        "image/webp",
    }
    # Usually WebP, but a file it cannot shrink is served as it is (#1427).
    assert success_types("/api/v1/models/{slug}/media/{item_id}/thumbnail", "get") == {"image/*"}
    assert success_types("/api/v1/models/{slug}/source", "get") == {"text/plain"}
    for path in ("/api/v1/models/{slug}/readme", "/api/v1/models/{slug}/thumbnail"):
        for method in ("put", "delete"):
            assert success_types(path, method) == {"application/json"}, (method, path)


def test_the_merge_route_documents_each_409(tmp_path: Path) -> None:
    """#371: a conflict, no update, and a merge that kept racing are all 409s."""
    paths = json.loads(export(tmp_path / "openapi.json").read_text(encoding="utf-8"))["paths"]
    merge = paths["/api/v1/models/{slug}/upstream/merge"]["post"]
    conflict = merge["responses"]["409"]["description"]
    for case in ("merge_base", "`current`", "`gone`", "kept changing", "retry"):
        assert case in conflict, case
        assert case in merge["description"], case


def test_the_download_documents_only_a_3mf(tmp_path: Path) -> None:
    """#769: it answers a FileResponse or a rewritten one, and either is a 3MF."""
    paths = json.loads(export(tmp_path / "openapi.json").read_text(encoding="utf-8"))["paths"]
    download = paths["/api/v1/outputs/{output_id}/model.3mf"]["get"]
    assert set(download["responses"]["200"]["content"]) == {"model/3mf"}


def test_the_view_routes_document_a_png(tmp_path: Path) -> None:
    paths = json.loads(export(tmp_path / "openapi.json").read_text(encoding="utf-8"))["paths"]

    for path in (
        "/api/v1/jobs/{job_id}/views/{view}.png",
        "/api/v1/outputs/{output_id}/views/{view}.png",
    ):
        assert set(paths[path]["get"]["responses"]["200"]["content"]) == {"image/png"}, path


def test_the_view_routes_document_the_camera(tmp_path: Path) -> None:
    """#830: an agent works a camera out from a feature's coordinates, so the
    conventions are in the spec, not only in the code."""
    paths = json.loads(export(tmp_path / "openapi.json").read_text(encoding="utf-8"))["paths"]

    for path in (
        "/api/v1/jobs/{job_id}/views/{view}.png",
        "/api/v1/outputs/{output_id}/views/{view}.png",
        "/api/v1/jobs/{job_id}/colours.png",
    ):
        params = {p["name"]: p for p in paths[path]["get"]["parameters"]}
        for name in ("azimuth", "elevation", "zoom", "target_x", "target_y", "target_z"):
            assert params[name]["in"] == "query", (path, name)
            assert params[name]["description"], (path, name)
        assert "+Z" in params["azimuth"]["description"], path
        assert "front" in params["azimuth"]["description"], path
        assert '"maximum": 90' in json.dumps(params["elevation"]["schema"]), path


def test_each_attachment_upload_body_has_its_own_name(tmp_path: Path) -> None:
    """#5317: the print and the order upload bodies were both `AttachmentUpload`, so the
    spec kept one shape for two routes and the order route lost its `role` part."""
    schema = json.loads(export(tmp_path / "openapi.json").read_text(encoding="utf-8"))
    named = schema["components"]["schemas"]
    assert "AttachmentUpload" not in named

    def body(path: str) -> str:
        content = schema["paths"][path]["post"]["requestBody"]["content"]
        ref: str = content["multipart/form-data"]["schema"]["$ref"]
        return ref.removeprefix("#/components/schemas/")

    printed = body("/api/v1/prints/{archive_id}/attachments")
    ordered = body("/api/v1/orders/{order_id}/attachments")
    assert (printed, ordered) == ("PrintAttachmentUpload", "OrderAttachmentUpload")
    assert set(named[printed]["properties"]) == {"file", "caption"}
    assert set(named[ordered]["properties"]) == {"file", "caption", "role"}
    assert "fulfilment_id" in named["RevisionAttachmentUpload"]["properties"]


def test_naming_two_models_with_one_name_is_refused() -> None:
    """The guard behind #5317: a name already in the spec is never overwritten."""

    class Upload(BaseModel):
        file: bytes

    first = Upload

    class Upload(BaseModel):  # type: ignore[no-redef]
        file: bytes
        role: str

    app = FastAPI()
    _name_in_openapi(app, first, Upload)
    with pytest.raises(ValueError, match="Upload"):
        app.openapi()
