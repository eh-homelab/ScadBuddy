from __future__ import annotations

import json
from pathlib import Path

from scadbuddy.tools.export_openapi import export

EXPECTED_PATHS = {
    "/healthz",
    "/api/v1/models",
    "/api/v1/models/check",
    "/api/v1/models/import",
    "/api/v1/models/{slug}",
    "/api/v1/models/{slug}/duplicate",
    "/api/v1/models/{slug}/upstream",
    "/api/v1/models/{slug}/upstream/merge",
    "/api/v1/models/{slug}/upstream/dismiss",
    "/api/v1/models/{slug}/upstream/detach",
    "/api/v1/models/{slug}/source",
    "/api/v1/lsp/diagnostics",
    "/api/v1/models/{slug}/schema",
    "/api/v1/models/{slug}/thumbnail",
    "/api/v1/models/{slug}/media",
    "/api/v1/models/{slug}/media/order",
    "/api/v1/models/{slug}/media/cover",
    "/api/v1/models/{slug}/media/{item_id}",
    "/api/v1/models/{slug}/media/{item_id}/poster",
    "/api/v1/models/{slug}/readme",
    "/api/v1/models/{slug}/presets",
    "/api/v1/models/{slug}/presets/{preset_id}",
    "/api/v1/models/{slug}/presets/{preset_id}/duplicate",
    "/api/v1/models/{slug}/render",
    "/api/v1/models/{slug}/versions",
    "/api/v1/models/{slug}/versions/{commit}/source",
    "/api/v1/models/{slug}/versions/{commit}/schema",
    "/api/v1/models/{slug}/versions/{commit}/diff",
    "/api/v1/models/{slug}/versions/{commit}/restore",
    "/api/v1/models/{slug}/outputs",
    "/api/v1/jobs/{job_id}",
    "/api/v1/jobs/{job_id}/preview.glb",
    "/api/v1/outputs/{output_id}",
    "/api/v1/outputs/{output_id}/edit",
    "/api/v1/outputs/{output_id}/geometry",
    "/api/v1/outputs/{output_id}/model.3mf",
    "/api/v1/outputs/{output_id}/thumbnail",
    "/api/v1/outputs/{output_id}/plates",
    "/api/v1/outputs/{output_id}/plates/{index}/thumbnail",
    "/api/v1/outputs/{output_id}/send",
    "/api/v1/outputs/{output_id}/project-file",
    "/api/v1/print/projects",
    "/api/v1/print/projects/last",
    "/api/v1/print/outputs/{output_id}/project",
    "/api/v1/print/models/{slug}/choices",
    "/api/v1/print/printers/{printer_id}/bed-type",
    "/api/v1/print/outputs/{output_id}/filaments",
    "/api/v1/print/outputs/{output_id}/choices",
    "/api/v1/print/outputs/{output_id}/progress",
    "/api/v1/print/outputs/{output_id}/run",
    "/api/v1/print/outputs/{output_id}/check",
    "/api/v1/print/runs/{run_id}",
    "/api/v1/analyzers",
    "/api/v1/analyzers/run",
    "/api/v1/analyzers/fixes/preview",
    "/api/v1/analyzers/fixes/apply",
    "/api/v1/analyzers/decisions",
    "/api/v1/analyzers/decisions/{decision_id}",
    "/api/v1/settings",
    "/api/v1/settings/print-options",
    "/api/v1/settings/test",
    "/api/v1/settings/targets",
    "/api/v1/settings/register-sidebar",
    "/api/v1/settings/bambuddy",
    "/api/v1/settings/remembered",
    "/api/v1/fonts",
    "/api/v1/fonts/catalogue",
    "/api/v1/fonts/install",
    "/api/v1/plate",
    "/api/v1/plate/fit",
    "/api/v1/plates",
    "/api/v1/libraries",
    "/api/v1/assets/usage",
    "/api/v1/models/{slug}/assets",
    "/api/v1/models/{slug}/assets/{asset_id}",
    "/api/v1/models/{slug}/assets/{asset_id}/content",
    "/api/v1/models/{slug}/samples/{name}",
    "/api/v1/models/{slug}/libraries/{name}",
    "/api/v1/models/{slug}/libraries/{name}/files/{path}",
    "/api/v1/models/{slug}/files/{path}",
    "/api/v1/libraries/installed",
    "/api/v1/libraries/{name}",
    "/api/v1/libraries/{name}/users",
    "/api/v1/models/{slug}/libraries/{name}/check",
    "/api/v1/models/{slug}/dependencies",
    "/api/v1/models/{slug}/diagnostics",
    "/api/v1/jobs/{job_id}/views/{view}.png",
    "/api/v1/jobs/{job_id}/colours.png",
    "/api/v1/outputs/{output_id}/views/{view}.png",
    "/api/v1/prints/{archive_id}/timelapse",
    "/api/v1/prints/{archive_id}/photos/{filename}",
    "/api/v1/prints/{archive_id}/thumbnail",
    "/api/v1/prints/{archive_id}/plates/{index}/thumbnail",
    "/api/v1/prints/{archive_id}/files/sliced",
    "/api/v1/prints/{archive_id}/files/source",
    "/api/v1/print/library",
    "/api/v1/print/library/{file_id}/plates",
    "/api/v1/print/library/{file_id}/thumbnail",
    "/api/v1/print/library/{file_id}/plates/{index}/thumbnail",
    "/api/v1/print/library/{file_id}/choices",
    "/api/v1/print/library/{file_id}/filaments",
    "/api/v1/print/library/{file_id}/run",
    "/api/v1/print/library/{file_id}/check",
}


def test_every_route_in_the_spec_is_published(tmp_path: Path) -> None:
    schema = json.loads(export(tmp_path / "openapi.json").read_text(encoding="utf-8"))
    assert set(schema["paths"]) == EXPECTED_PATHS


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
